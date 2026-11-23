"""Deterministic Chinese dual-track indexer, section FTS, incremental embeddings & kb rebuild."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import jieba

from kb_engine.config import KBConfig
from kb_engine.db import db_session, init_database, upsert_raw_document
from kb_engine.providers import EmbeddingProvider, pack_vector
from kb_engine.utils import (
    atomic_write_text,
    classify_and_resolve_link,
    compute_effective_char_count,
    extract_markdown_links,
    local_today_str,
    normalize_newlines,
    normalize_nfc,
    normalize_rel_path,
    parse_frontmatter,
    sha256_file,
    sha256_text,
    utc_now_iso,
    write_lock,
)

# PRD Iron Law #8: silence jieba stdout/stderr noise completely
jieba.setLogLevel(logging.ERROR)

_TYPE_SECTIONS: list[tuple[str, str]] = [
    ("concept", "核心概念（Concepts）"),
    ("case", "实战案例（Cases）"),
    ("evidence", "证据与金句（Evidences）"),
    ("insight", "原创洞察（Insights）"),
    ("source", "来源精读（Sources）"),
]

_TOKEN_CLEAN_RE = re.compile(r"[^\w\u4e00-\u9fff\-./+]+")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")


def segment_text(text: str) -> str:
    """Segment Chinese/English text using jieba search mode for FTS5 indexing."""
    if not text:
        return ""
    norm = normalize_nfc(text)
    tokens: list[str] = []
    for tok in jieba.cut_for_search(norm):
        cleaned = _TOKEN_CLEAN_RE.sub("", tok).strip()
        if cleaned:
            tokens.append(cleaned)
    return " ".join(tokens)


def split_markdown_sections(body: str) -> list[dict[str, Any]]:
    """Split Markdown body by H1-H3 headings into ordered section dicts."""
    if not body or not body.strip():
        return []
    sections: list[dict[str, Any]] = []
    cur_heading = "导语"
    cur_lines: list[str] = []
    order = 0

    for line in body.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            content = "\n".join(cur_lines).strip()
            if content:
                sections.append(
                    {
                        "order": order,
                        "heading": cur_heading,
                        "content": content,
                    }
                )
                order += 1
            cur_heading = m.group(2).strip()
            cur_lines = []
        else:
            cur_lines.append(line)

    tail = "\n".join(cur_lines).strip()
    if tail:
        sections.append(
            {
                "order": order,
                "heading": cur_heading,
                "content": tail,
            }
        )
    return sections


def iter_wiki_content_files(root_dir: Path) -> list[Path]:
    """Return sorted list of wiki content Markdown files (excluding index.md and log.md)."""
    wiki_dir = root_dir / "wiki"
    if not wiki_dir.exists():
        return []
    files: list[Path] = []
    for p in sorted(wiki_dir.rglob("*.md")):
        if not p.is_file():
            continue
        rel = normalize_rel_path(p.relative_to(wiki_dir))
        if rel in ("index.md", "log.md"):
            continue
        files.append(p)
    return files


def generate_wiki_index(root_dir: Path, pages: list[dict[str, Any]], *, project_name: str = "个人知识库总目录") -> Path:
    """Deterministically regenerate wiki/index.md from parsed wiki page metadata."""
    today = local_today_str()
    grouped: dict[str, list[dict[str, Any]]] = {t: [] for t, _ in _TYPE_SECTIONS}
    for page in pages:
        ptype = page.get("type", "")
        if ptype in grouped:
            grouped[ptype].append(page)

    lines: list[str] = [
        "---",
        'okf_version: "0.3"',
        f'title: "{project_name}"',
        'language: "zh-CN"',
        f'updated_at: "{today}"',
        "---",
        "",
        f"# {project_name}",
        "",
        "> 本文件由 `kb` 引擎根据页面元数据自动维护，请勿手动编辑。",
        "",
    ]

    for ptype, heading in _TYPE_SECTIONS:
        items = sorted(
            grouped[ptype],
            key=lambda x: (x.get("updated_at", ""), x.get("title", "")),
            reverse=True,
        )
        lines.append(f"## {heading}")
        if not items:
            lines.append("- （暂无条目）")
        else:
            for item in items:
                rel = normalize_rel_path(item["rel_path"])
                title = item.get("title") or Path(rel).stem
                summary = (item.get("summary") or "").strip()
                status_tag = " `[已废弃]`" if item.get("status") == "deprecated" else ""
                if summary:
                    lines.append(f"- [{title}](./{rel}){status_tag} — {summary}")
                else:
                    lines.append(f"- [{title}](./{rel}){status_tag}")
        lines.append("")

    index_path = root_dir / "wiki" / "index.md"
    atomic_write_text(index_path, "\n".join(lines))
    return index_path


def append_wiki_log(
    root_dir: Path,
    *,
    note: str,
    session_id: str,
    changed_pages: list[str],
    failed_items: list[dict[str, Any]] | None = None,
) -> Path:
    """Deterministically prepend an entry to wiki/log.md in reverse chronological order."""
    log_path = root_dir / "wiki" / "log.md"
    today = local_today_str()
    header = (
        "# 知识库更新日志\n\n"
        "> 本文件由 `kb compile-commit` 确定性自动追加，请勿手动修改历史条目。\n"
    )

    entry_lines = [f"## {today} · {note} (`{session_id}`)"]
    if changed_pages:
        entry_lines.append("- 变更页面：" + "、".join(f"`{normalize_rel_path(p)}`" for p in sorted(changed_pages)))
    else:
        entry_lines.append("- 变更页面：无正文改动")
    if failed_items:
        failed_desc = "、".join(
            f"`{item['raw_rel_path']}`（{item.get('error_message') or '编译失败'}）"
            for item in failed_items
        )
        entry_lines.append(f"- 跳过失败文件（毒丸隔离）：{failed_desc}")
    entry_block = "\n".join(entry_lines) + "\n"

    if log_path.exists():
        existing = normalize_newlines(log_path.read_text(encoding="utf-8"))
        marker_idx = existing.find("\n## ")
        if marker_idx != -1:
            prefix = existing[:marker_idx].rstrip() + "\n\n"
            rest = existing[marker_idx + 1 :]
            new_content = prefix + entry_block + "\n" + rest
        else:
            new_content = existing.rstrip() + "\n\n" + entry_block
    else:
        new_content = header + "\n" + entry_block

    atomic_write_text(log_path, new_content)
    return log_path


def _extract_source_refs(meta: dict[str, Any], page_file: Path, root_dir: Path) -> list[str]:
    """Resolve YAML sources[].ref entries into root-relative raw/ paths."""
    sources = meta.get("sources")
    if not isinstance(sources, list):
        return []
    resolved_refs: list[str] = []
    root_resolved = root_dir.resolve()
    for item in sources:
        ref = item.get("ref") if isinstance(item, dict) else (item if isinstance(item, str) else None)
        if not ref or not isinstance(ref, str):
            continue
        link = classify_and_resolve_link(page_file, ref, False, root_dir)
        if link.target_abs_path is not None:
            try:
                rel_root = normalize_rel_path(link.target_abs_path.relative_to(root_resolved))
                resolved_refs.append(rel_root)
            except ValueError:
                continue
    return resolved_refs


def _sync_incremental_embeddings(
    conn: Any,
    sections_to_sync: list[dict[str, Any]],
    embedding_provider: EmbeddingProvider,
) -> tuple[int, str | None]:
    """Incrementally embed only newly added or modified sections; reuse unchanged cache."""
    ready, reason = embedding_provider.is_ready()
    if not ready:
        return 0, reason

    cfg = embedding_provider.config
    current_ids = {s["section_id"] for s in sections_to_sync}

    # Load existing cached hashes for current model
    existing: dict[str, str] = {}
    for row in conn.execute(
        "SELECT section_id, content_hash FROM wiki_section_embeddings WHERE model = ?",
        (cfg.model,),
    ).fetchall():
        existing[row["section_id"]] = row["content_hash"]

    # Prune embeddings for deleted sections
    stale_ids = [sid for sid in existing if sid not in current_ids]
    if stale_ids:
        placeholders = ",".join("?" for _ in stale_ids)
        conn.execute(
            f"DELETE FROM wiki_section_embeddings WHERE section_id IN ({placeholders})",
            stale_ids,
        )

    pending = [
        s for s in sections_to_sync if existing.get(s["section_id"]) != s["content_hash"]
    ]
    if not pending:
        return 0, None

    texts = [s["embed_text"] for s in pending]
    vectors = embedding_provider.embed_texts(texts, is_query=False)
    if vectors is None or len(vectors) != len(pending):
        return 0, embedding_provider.last_error or "embedding_mismatch"

    now = utc_now_iso()
    for sec, vec in zip(pending, vectors):
        conn.execute(
            """
            INSERT OR REPLACE INTO wiki_section_embeddings (
                section_id, rel_path, content_hash, provider, model, dim, vector_blob, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sec["section_id"],
                sec["rel_path"],
                sec["content_hash"],
                cfg.provider,
                cfg.model,
                len(vec),
                pack_vector(vec),
                now,
            ),
        )
    conn.execute(
        "INSERT OR REPLACE INTO kb_meta (key, value) VALUES ('embedding_model', ?)",
        (cfg.model,),
    )
    return len(pending), None


def reindex_wiki_in_conn(
    root_dir: Path,
    conn: Any,
    *,
    regenerate_index: bool = True,
    config: KBConfig | None = None,
    embedding_provider: Any = None,
) -> dict[str, Any]:
    """Rebuild wiki_pages, wiki_fts, wiki_sections, wiki_sections_fts, links & optional embeddings."""
    wiki_dir = root_dir / "wiki"
    content_files = iter_wiki_content_files(root_dir)

    # Map raw_rel_path -> content_hash for provenance_map resolution
    raw_hash_by_rel: dict[str, str] = {}
    for row in conn.execute("SELECT content_hash, raw_rel_path, asset_rel_path FROM raw_documents").fetchall():
        raw_hash_by_rel[normalize_rel_path(row["raw_rel_path"])] = row["content_hash"]
        if row["asset_rel_path"]:
            raw_hash_by_rel[normalize_rel_path(row["asset_rel_path"])] = row["content_hash"]

    conn.execute("DELETE FROM wiki_pages")
    conn.execute("DELETE FROM wiki_fts")
    conn.execute("DELETE FROM wiki_sections")
    conn.execute("DELETE FROM wiki_sections_fts")
    conn.execute("DELETE FROM wiki_links")
    conn.execute("DELETE FROM provenance_map")

    pages_for_index: list[dict[str, Any]] = []
    sections_for_embed: list[dict[str, Any]] = []
    links_count = 0
    broken_links_count = 0
    provenance_count = 0

    for file_path in content_files:
        rel_path = normalize_rel_path(file_path.relative_to(wiki_dir))
        raw_text = normalize_newlines(file_path.read_text(encoding="utf-8"))
        meta, body, _err = parse_frontmatter(raw_text)
        meta = meta or {}

        ptype = str(meta.get("type") or _infer_type_from_rel_path(rel_path))
        title = normalize_nfc(str(meta.get("title") or file_path.stem))
        summary = normalize_nfc(str(meta.get("summary") or ""))
        status = str(meta.get("status") or "stable")
        domain = str(meta.get("domain") or "")
        visibility = str(meta.get("visibility") or "private")

        raw_tags = meta.get("tags") or []
        tags = [normalize_nfc(str(t)) for t in raw_tags] if isinstance(raw_tags, list) else [normalize_nfc(str(raw_tags))]

        raw_aliases = meta.get("aliases") or []
        aliases = [normalize_nfc(str(a)) for a in raw_aliases] if isinstance(raw_aliases, list) else [normalize_nfc(str(raw_aliases))]

        reading_status = str(meta.get("reading_status") or "").strip().lower()
        experience_type = str(meta.get("experience_type") or "").strip().lower()
        origin = str(meta.get("origin") or "").strip().lower()

        raw_layers = meta.get("layers") or []
        if isinstance(raw_layers, list):
            layers = [normalize_nfc(str(x)).strip().upper() for x in raw_layers if str(x).strip()]
        else:
            layers = [normalize_nfc(str(raw_layers)).strip().upper()] if str(raw_layers).strip() else []

        raw_mechanisms = meta.get("mechanisms") or []
        if isinstance(raw_mechanisms, list):
            mechanisms = [normalize_nfc(str(m)).strip() for m in raw_mechanisms if str(m).strip()]
        else:
            mechanisms = [normalize_nfc(str(raw_mechanisms)).strip()] if str(raw_mechanisms).strip() else []

        verified_val = meta.get("verified")
        is_verified = 1 if (isinstance(verified_val, list) and len(verified_val) > 0) or (isinstance(verified_val, str) and verified_val.strip()) or (verified_val is True) else 0

        stale_after = str(meta["stale_after"]) if meta.get("stale_after") else None
        superseded_by = str(meta["superseded_by"]).strip() if meta.get("superseded_by") else None
        char_count = compute_effective_char_count(body)
        content_hash = sha256_text(raw_text)

        generated = meta.get("generated")
        updated_at = ""
        if isinstance(generated, dict) and generated.get("at"):
            updated_at = str(generated["at"])
        if not updated_at:
            updated_at = utc_now_iso()

        conn.execute(
            """
            INSERT INTO wiki_pages (
                rel_path, type, title, summary, status, domain, visibility,
                tags_json, aliases_json, reading_status, experience_type, origin,
                layers_json, mechanisms_json, is_verified, stale_after, superseded_by,
                char_count, content_hash, body, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rel_path,
                ptype,
                title,
                summary,
                status,
                domain,
                visibility,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(aliases, ensure_ascii=False),
                reading_status,
                experience_type,
                origin,
                json.dumps(layers, ensure_ascii=False),
                json.dumps(mechanisms, ensure_ascii=False),
                is_verified,
                stale_after,
                superseded_by,
                char_count,
                content_hash,
                body,
                updated_at,
            ),
        )

        conn.execute(
            """
            INSERT INTO wiki_fts (
                rel_path, title_seg, aliases_seg, tags_seg, mechanisms_seg, summary_seg, body_seg
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rel_path,
                segment_text(title),
                segment_text(" ".join(aliases)),
                segment_text(" ".join(tags)),
                segment_text(" ".join(mechanisms)),
                segment_text(summary),
                segment_text(body),
            ),
        )

        # Index section-level rows & section FTS5 (context-enriched with page title & summary)
        for sec in split_markdown_sections(body):
            sec_id = f"{rel_path}#s{sec['order']}"
            heading = normalize_nfc(sec["heading"])
            sec_content = normalize_nfc(sec["content"])
            embed_text = f"[{title} | {summary}] {heading}\n{sec_content}"
            sec_hash = sha256_text(embed_text)

            conn.execute(
                """
                INSERT INTO wiki_sections (
                    section_id, rel_path, section_order, heading, content, content_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sec_id, rel_path, sec["order"], heading, sec_content, sec_hash, updated_at),
            )
            conn.execute(
                """
                INSERT INTO wiki_sections_fts (
                    section_id, rel_path, heading_seg, content_seg
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    sec_id,
                    rel_path,
                    segment_text(f"{title} {heading}"),
                    segment_text(sec_content),
                ),
            )
            sections_for_embed.append(
                {
                    "section_id": sec_id,
                    "rel_path": rel_path,
                    "content_hash": sec_hash,
                    "embed_text": embed_text,
                }
            )

        pages_for_index.append(
            {
                "rel_path": rel_path,
                "type": ptype,
                "title": title,
                "summary": summary,
                "status": status,
                "updated_at": updated_at,
            }
        )

        # Extract and classify Markdown links -> only wiki/ internal links go into wiki_links
        seen_links: set[tuple[str, str]] = set()
        for is_image, _label, raw_target in extract_markdown_links(body):
            resolved = classify_and_resolve_link(file_path, raw_target, is_image, root_dir)
            if resolved.kind != "wiki" or not resolved.target_rel_path:
                continue
            key = (resolved.target_rel_path, raw_target)
            if key in seen_links:
                continue
            seen_links.add(key)
            is_broken = 0 if (resolved.target_abs_path and resolved.target_abs_path.exists()) else 1
            conn.execute(
                """
                INSERT OR IGNORE INTO wiki_links (
                    source_path, target_path, raw_link, is_broken,
                    link_source, relation_type, relation_status, relation_layer, relation_reason
                ) VALUES (?, ?, ?, ?, 'body', 'mentions', 'confirmed', '', '')
                """,
                (rel_path, resolved.target_rel_path, raw_target, is_broken),
            )
            links_count += 1
            if is_broken:
                broken_links_count += 1

        # Index lightweight typed relations from YAML Frontmatter (`relations:`)
        raw_relations = meta.get("relations")
        if isinstance(raw_relations, list):
            for rel_item in raw_relations:
                if not isinstance(rel_item, dict):
                    continue
                raw_target = str(rel_item.get("target") or "").strip()
                if not raw_target:
                    continue
                resolved = classify_and_resolve_link(file_path, raw_target, False, root_dir)
                if resolved.kind != "wiki" or not resolved.target_rel_path:
                    continue
                is_broken = 0 if (resolved.target_abs_path and resolved.target_abs_path.exists()) else 1
                rel_type = normalize_nfc(str(rel_item.get("type") or "related")).strip() or "related"
                rel_status = str(rel_item.get("status") or "confirmed").strip().lower() or "confirmed"
                rel_layer = normalize_nfc(str(rel_item.get("layer") or "")).strip().upper()
                rel_reason = normalize_nfc(str(rel_item.get("reason") or "")).strip()
                conn.execute(
                    """
                    INSERT OR REPLACE INTO wiki_links (
                        source_path, target_path, raw_link, is_broken,
                        link_source, relation_type, relation_status, relation_layer, relation_reason
                    ) VALUES (?, ?, ?, ?, 'frontmatter', ?, ?, ?, ?)
                    """,
                    (
                        rel_path,
                        resolved.target_rel_path,
                        raw_target,
                        is_broken,
                        rel_type,
                        rel_status,
                        rel_layer,
                        rel_reason,
                    ),
                )
                if (resolved.target_rel_path, raw_target) not in seen_links:
                    seen_links.add((resolved.target_rel_path, raw_target))
                    links_count += 1
                    if is_broken:
                        broken_links_count += 1

        # Maintain provenance_map from YAML sources[].ref
        for raw_ref in _extract_source_refs(meta, file_path, root_dir):
            raw_hash = raw_hash_by_rel.get(raw_ref)
            if raw_hash:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO provenance_map (raw_content_hash, wiki_rel_path)
                    VALUES (?, ?)
                    """,
                    (raw_hash, rel_path),
                )
                provenance_count += 1

    # Incremental section embeddings (only runs when enabled + API key configured)
    embeddings_computed = 0
    embedding_note: str | None = None
    provider = embedding_provider
    if provider is None and config is not None and config.retrieval.embedding.enabled:
        provider = EmbeddingProvider(config.retrieval.embedding)
    if provider is not None:
        embeddings_computed, embedding_note = _sync_incremental_embeddings(
            conn, sections_for_embed, provider
        )

    if regenerate_index:
        generate_wiki_index(root_dir, pages_for_index)

    return {
        "pages_indexed": len(pages_for_index),
        "sections_indexed": len(sections_for_embed),
        "embeddings_computed": embeddings_computed,
        "embedding_note": embedding_note,
        "links_indexed": links_count,
        "broken_links": broken_links_count,
        "provenance_links": provenance_count,
    }


def _infer_type_from_rel_path(rel_path: str) -> str:
    prefix = rel_path.split("/", 1)[0]
    mapping = {
        "sources": "source",
        "concepts": "concept",
        "cases": "case",
        "evidences": "evidence",
        "insights": "insight",
    }
    return mapping.get(prefix, "concept")


def reindex_wiki(
    root_dir: Path,
    config: KBConfig,
    *,
    regenerate_index: bool = True,
    embedding_provider: Any = None,
) -> dict[str, Any]:
    """Acquire write lock and rebuild SQLite wiki tables + wiki/index.md."""
    with write_lock(root_dir, config.safety.write_lock_timeout_seconds):
        with db_session(root_dir) as conn:
            stats = reindex_wiki_in_conn(
                root_dir,
                conn,
                regenerate_index=regenerate_index,
                config=config,
                embedding_provider=embedding_provider,
            )
    return {"ok": True, **stats}


def rebuild_database(root_dir: Path, config: KBConfig) -> dict[str, Any]:
    """Disaster recovery: rebuild .kb/kb.db losslessly from raw/ (including image stubs) & wiki/."""
    with write_lock(root_dir, config.safety.write_lock_timeout_seconds):
        init_database(root_dir, reset=True)
        raw_dir = root_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        rebuilt_raw = 0
        missing_assets: list[str] = []

        with db_session(root_dir) as conn:
            for raw_file in sorted(raw_dir.glob("*.md")):
                if not raw_file.is_file():
                    continue
                raw_text = normalize_newlines(normalize_nfc(raw_file.read_text(encoding="utf-8")))
                meta, _body, _err = parse_frontmatter(raw_text)
                meta = meta or {}
                raw_rel_path = normalize_rel_path(raw_file.relative_to(root_dir))

                if meta.get("raw_type") == "image_stub":
                    asset_rel = normalize_rel_path(str(meta.get("asset_rel_path") or ""))
                    asset_sha = str(meta.get("asset_sha256") or "").strip()
                    asset_abs = root_dir / asset_rel if asset_rel else None
                    if asset_abs and asset_abs.exists():
                        if not asset_sha:
                            asset_sha = sha256_file(asset_abs)
                    else:
                        missing_assets.append(asset_rel or raw_rel_path)
                        if not asset_sha:
                            asset_sha = sha256_text(raw_text)
                    upsert_raw_document(
                        conn,
                        content_hash=asset_sha,
                        source_origin="rebuilt",
                        origin_path=None,
                        origin_mtime=None,
                        raw_rel_path=raw_rel_path,
                        asset_rel_path=asset_rel or None,
                        media_type="image",
                        needs_vision=True,
                        state="PENDING_COMPILE",
                    )
                else:
                    content_hash = sha256_text(raw_text)
                    upsert_raw_document(
                        conn,
                        content_hash=content_hash,
                        source_origin="rebuilt",
                        origin_path=None,
                        origin_mtime=None,
                        raw_rel_path=raw_rel_path,
                        asset_rel_path=None,
                        media_type="text",
                        needs_vision=False,
                        state="PENDING_COMPILE",
                    )
                rebuilt_raw += 1

            # Rebuild wiki_pages, wiki_fts, wiki_sections, links, provenance_map & optional embeddings
            index_stats = reindex_wiki_in_conn(
                root_dir,
                conn,
                regenerate_index=True,
                config=config,
            )

            # Mark any raw_documents referenced in provenance_map as COMPILED
            now = utc_now_iso()
            cur = conn.execute(
                """
                UPDATE raw_documents
                SET state = 'COMPILED', updated_at = ?
                WHERE content_hash IN (SELECT DISTINCT raw_content_hash FROM provenance_map)
                """,
                (now,),
            )
            compiled_count = cur.rowcount

            pending_count = conn.execute(
                "SELECT COUNT(*) FROM raw_documents WHERE state = 'PENDING_COMPILE'"
            ).fetchone()[0]

    return {
        "ok": True,
        "rebuilt_raw_count": rebuilt_raw,
        "compiled_raw_count": compiled_count,
        "pending_raw_count": pending_count,
        "missing_assets": missing_assets,
        **index_stats,
    }
