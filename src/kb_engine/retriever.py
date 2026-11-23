"""Dual-track + section-level + pluggable hybrid (Vector RRF & Rerank) + lifecycle/mode retriever."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import jieba

from kb_engine.config import KBConfig
from kb_engine.db import connect_db
from kb_engine.evolution import record_query_telemetry
from kb_engine.indexer import split_markdown_sections
from kb_engine.providers import (
    EmbeddingProvider,
    RerankerProvider,
    cosine_similarity,
    unpack_vector,
)
from kb_engine.utils import normalize_nfc, parse_frontmatter

jieba.setLogLevel(logging.ERROR)

_TOKEN_CLEAN_RE = re.compile(r"[^\w\u4e00-\u9fff\-./+]+")
_HAS_CONTENT_CHAR_RE = re.compile(r"[\w\u4e00-\u9fff]")
_NAV_HEADINGS = frozenset({"关联知识", "参考来源", "延伸阅读", "相关链接"})
_STOP_SINGLE_CHARS = frozenset(
    "的了在是和与及或被把将就也都而并从对向为以于到之其这那一个个中上下前后性化者"
)


def _escape_fts_token(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def _clean_token(token: str) -> str:
    cleaned = _TOKEN_CLEAN_RE.sub("", token).strip()
    if not cleaned or not _HAS_CONTENT_CHAR_RE.search(cleaned):
        return ""
    return cleaned


def _extract_query_terms(query: str) -> tuple[list[str], list[str]]:
    """Return (raw_parts, informative_tokens) for scoring snippets and sections."""
    norm = normalize_nfc(query).strip()
    if not norm:
        return [], []
    raw_parts: list[str] = []
    seen_parts: set[str] = set()
    for part in re.split(r"\s+", norm):
        c = _clean_token(part)
        if c and c.lower() not in seen_parts:
            seen_parts.add(c.lower())
            raw_parts.append(c)

    tokens: list[str] = []
    seen_toks: set[str] = set()
    for p in raw_parts:
        if p.lower() not in seen_toks:
            seen_toks.add(p.lower())
            tokens.append(p)
    for tok in jieba.cut_for_search(norm):
        c = _clean_token(tok)
        if not c or c.lower() in seen_toks:
            continue
        if len(c) == 1 and c in _STOP_SINGLE_CHARS:
            continue
        seen_toks.add(c.lower())
        tokens.append(c)
    return raw_parts, tokens


def build_strict_fts_query(query: str) -> str:
    """Build an AND-priority FTS5 query that requires all query chunks/semantic components."""
    norm = normalize_nfc(query).strip()
    if not norm:
        return ""

    chunk_clauses: list[str] = []
    seen_chunks: set[str] = set()
    for raw_part in re.split(r"\s+", norm):
        part_clean = _clean_token(raw_part)
        if not part_clean or part_clean.lower() in seen_chunks:
            continue
        seen_chunks.add(part_clean.lower())

        sub_tokens: list[str] = []
        seen_sub: set[str] = set()
        for tok in jieba.cut(part_clean, cut_all=False):
            c = _clean_token(tok)
            if not c or c.lower() in seen_sub:
                continue
            if len(c) == 1 and c in _STOP_SINGLE_CHARS:
                continue
            seen_sub.add(c.lower())
            sub_tokens.append(c)

        escaped_part = _escape_fts_token(part_clean)
        if len(sub_tokens) > 1:
            and_sub = " AND ".join(_escape_fts_token(t) for t in sub_tokens)
            chunk_clauses.append(f"({escaped_part} OR ({and_sub}))")
        else:
            chunk_clauses.append(escaped_part)

    if not chunk_clauses:
        return ""
    return " AND ".join(chunk_clauses)


def build_safe_fts_query(query: str) -> str:
    """Build an OR-fallback FTS5 query with double-quote escaping and stop-char filtering."""
    norm = normalize_nfc(query).strip()
    if not norm:
        return ""
    tokens: list[str] = []
    seen: set[str] = set()

    for raw_part in re.split(r"\s+", norm):
        part_clean = _clean_token(raw_part)
        if part_clean and part_clean.lower() not in seen:
            seen.add(part_clean.lower())
            tokens.append(part_clean)

    for tok in jieba.cut_for_search(norm):
        cleaned = _clean_token(tok)
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            tokens.append(cleaned)

    multi_char = [t for t in tokens if len(t) >= 2 or t not in _STOP_SINGLE_CHARS]
    final_tokens = multi_char if multi_char else tokens
    if not final_tokens:
        return ""
    return " OR ".join(_escape_fts_token(t) for t in final_tokens)


def _make_snippet(body: str, query: str, max_len: int = 140) -> str:
    compact = re.sub(r"\s+", " ", body).strip()
    if not compact:
        return ""
    lower_body = compact.lower()
    raw_parts, tokens = _extract_query_terms(query)
    lower_q = normalize_nfc(query).strip().lower()

    idx = lower_body.find(lower_q) if lower_q else -1
    if idx == -1:
        for term in [*raw_parts, *tokens]:
            t = term.lower()
            if len(t) >= 2:
                idx = lower_body.find(t)
                if idx != -1:
                    break
    if idx == -1:
        return compact[:max_len] + ("..." if len(compact) > max_len else "")
    start = max(0, idx - 40)
    end = min(len(compact), idx + max_len - 40)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(compact) else ""
    return f"{prefix}{compact[start:end]}{suffix}"


def _extract_matched_sections(body: str, query: str, max_sections: int = 3) -> list[dict[str, Any]]:
    """Slice Markdown body into heading sections and return top query-relevant passages."""
    parsed = split_markdown_sections(body)
    if not parsed:
        return []

    raw_parts, tokens = _extract_query_terms(query)
    lower_q = normalize_nfc(query).strip().lower()
    search_tokens = [t.lower() for t in tokens if len(t) >= 2] or [t.lower() for t in tokens]

    scored: list[tuple[float, int, str, str]] = []
    for sec in parsed:
        idx = sec["order"]
        heading = sec["heading"]
        content = sec["content"]
        haystack = f"{heading}\n{content}".lower()
        score = 0.0
        if lower_q and lower_q in haystack:
            score += 6.0
        distinct_hits = 0
        for part in raw_parts:
            pl = part.lower()
            if pl in heading.lower():
                score += 4.0
            if pl in haystack:
                score += 2.5
                distinct_hits += 1
        for tok in search_tokens:
            if tok in haystack:
                score += 1.0 + min(3, haystack.count(tok)) * 0.25
                distinct_hits += 1
        if score > 0.0:
            total = score + distinct_hits * 0.5
            if heading.strip() in _NAV_HEADINGS:
                total *= 0.35
            scored.append((total, idx, heading, content))

    if not scored:
        first = parsed[0]
        return [{"heading": first["heading"], "score": 0.0, "content": first["content"]}]

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [
        {"heading": heading, "score": round(score, 2), "content": content}
        for score, _idx, heading, content in scored[:max_sections]
    ]


def _extract_page_sources(root_dir: Path, rel_path: str) -> list[str]:
    file_path = root_dir / "wiki" / rel_path
    if not file_path.exists():
        return []
    meta, _body, _err = parse_frontmatter(file_path.read_text(encoding="utf-8"))
    if not meta or not isinstance(meta.get("sources"), list):
        return []
    refs: list[str] = []
    for item in meta["sources"]:
        if isinstance(item, dict) and item.get("ref"):
            refs.append(str(item["ref"]))
        elif isinstance(item, str):
            refs.append(item)
    return refs


def _parse_json_list(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        parsed = json.loads(str(raw))
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _build_page_filter_clauses(
    *,
    alias: str,
    page_type: str | None,
    mode: str,
    visibility: str | None,
    include_deprecated: bool,
    layer: str | None = None,
    experience_type: str | None = None,
    reading_status: str | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if page_type:
        clauses.append(f"{alias}.type = ?")
        params.append(page_type)

    if layer:
        clauses.append(f"{alias}.layers_json LIKE ?")
        params.append(f'%"{layer.strip().upper()}"%')
    if experience_type:
        clauses.append(f"{alias}.experience_type = ?")
        params.append(experience_type.strip().lower())
    if reading_status:
        clauses.append(f"{alias}.reading_status = ?")
        params.append(reading_status.strip().lower())

    if mode == "student":
        clauses.append(f"{alias}.visibility = 'public'")
        clauses.append(f"{alias}.status = 'stable'")
    else:
        if visibility:
            clauses.append(f"{alias}.visibility = ?")
            params.append(visibility)
        if not include_deprecated:
            clauses.append(f"{alias}.status != 'deprecated'")

    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


def _run_page_fts_query(
    conn: Any,
    fts_expr: str,
    w: Any,
    filter_sql: str,
    filter_params: list[Any],
) -> list[Any]:
    if not fts_expr:
        return []
    sql = f"""
        SELECT
            p.rel_path, p.type, p.title, p.summary, p.body,
            p.status, p.visibility, p.is_verified, p.superseded_by,
            p.reading_status, p.experience_type, p.origin,
            p.layers_json, p.mechanisms_json,
            p.tags_json, p.aliases_json,
            bm25(wiki_fts, 0.0, ?, ?, ?, ?, ?, ?) AS raw_bm25
        FROM wiki_fts
        JOIN wiki_pages p ON p.rel_path = wiki_fts.rel_path
        WHERE wiki_fts MATCH ? {filter_sql}
    """
    params: list[Any] = [w.title, w.aliases, w.tags, w.mechanisms, w.summary, w.body, fts_expr, *filter_params]
    return conn.execute(sql, params).fetchall()


def _run_section_fts_query(
    conn: Any,
    fts_expr: str,
    filter_sql: str,
    filter_params: list[Any],
) -> list[Any]:
    if not fts_expr:
        return []
    sql = f"""
        SELECT
            s.section_id, s.rel_path, s.heading, s.content,
            p.type, p.title, p.summary, p.body,
            p.status, p.visibility, p.is_verified, p.superseded_by,
            p.reading_status, p.experience_type, p.origin,
            p.layers_json, p.mechanisms_json,
            bm25(wiki_sections_fts, 0.0, 0.0, 3.0, 1.0) AS raw_bm25
        FROM wiki_sections_fts
        JOIN wiki_sections s ON s.section_id = wiki_sections_fts.section_id
        JOIN wiki_pages p ON p.rel_path = s.rel_path
        WHERE wiki_sections_fts MATCH ? {filter_sql}
    """
    return conn.execute(sql, [fts_expr, *filter_params]).fetchall()


def _make_hit_entry(
    row: Any,
    *,
    score: float,
    hit_fts: bool,
    hit_strict: bool,
    hit_like: bool,
    hit_section: bool,
    hit_vector: bool,
    best_section_text: str | None,
) -> dict[str, Any]:
    return {
        "rel_path": row["rel_path"],
        "type": row["type"],
        "title": row["title"],
        "summary": row["summary"],
        "body": row["body"],
        "status": row["status"],
        "visibility": row["visibility"],
        "is_verified": bool(row["is_verified"]),
        "superseded_by": row["superseded_by"],
        "reading_status": row["reading_status"] or "",
        "experience_type": row["experience_type"] or "",
        "origin": row["origin"] or "",
        "layers": _parse_json_list(row["layers_json"]),
        "mechanisms": _parse_json_list(row["mechanisms_json"]),
        "score": score,
        "hit_fts": hit_fts,
        "hit_strict": hit_strict,
        "hit_like": hit_like,
        "hit_section": hit_section,
        "hit_vector": hit_vector,
        "best_section_text": best_section_text,
    }


def query_wiki(
    root_dir: Path,
    config: KBConfig,
    query: str,
    *,
    page_type: str | None = None,
    mode: str = "creator",
    visibility: str | None = None,
    include_deprecated: bool = False,
    layer: str | None = None,
    experience_type: str | None = None,
    reading_status: str | None = None,
    l1_limit: int | None = None,
    offset: int = 0,
    top_k: int | None = None,
    embedding_provider: Any = None,
    reranker_provider: Any = None,
    record_telemetry: bool = True,
) -> dict[str, Any]:
    """Execute lock-free section+page dual-track, pluggable hybrid (Vector RRF + Rerank) & graph retrieval."""
    t0 = time.perf_counter()
    norm_query = normalize_nfc(query).strip()
    eff_mode = mode if mode in ("creator", "student") else "creator"
    eff_l1_limit = l1_limit if l1_limit is not None else config.retrieval.default_l1_limit
    eff_top_k = top_k if top_k is not None else config.retrieval.default_l2_top_k
    max_evidences = config.retrieval.max_expanded_evidences
    min_strict_hits = config.retrieval.min_strict_hits
    or_min_ratio = config.retrieval.or_fallback_min_score_ratio
    graph_seed_top_k = config.retrieval.graph_seed_top_k
    max_matched_sections = config.retrieval.max_matched_sections
    section_boost = config.retrieval.section_boost_weight
    rrf_k = config.retrieval.rrf_k
    verified_mult = config.retrieval.verified_boost_multiplier
    firsthand_mult = config.retrieval.firsthand_boost_multiplier
    w = config.retrieval.fts_weights

    filter_sql, filter_params = _build_page_filter_clauses(
        alias="p",
        page_type=page_type,
        mode=eff_mode,
        visibility=visibility,
        include_deprecated=include_deprecated,
        layer=layer,
        experience_type=experience_type,
        reading_status=reading_status,
    )

    conn = connect_db(root_dir, ensure_schema=True)
    try:
        hits: dict[str, dict[str, Any]] = {}
        fallback_used = False
        pruned_tail_hits = 0
        vector_used = False
        reranker_used = False
        degraded_reasons: list[str] = []
        top_rerank_score: float | None = None

        if norm_query:
            strict_expr = build_strict_fts_query(norm_query)

            # Track A1: Strict AND-priority page-level FTS5
            for row in _run_page_fts_query(conn, strict_expr, w, filter_sql, filter_params):
                bm25_score = max(0.1, -float(row["raw_bm25"]))
                hits[row["rel_path"]] = _make_hit_entry(
                    row,
                    score=bm25_score,
                    hit_fts=True,
                    hit_strict=True,
                    hit_like=False,
                    hit_section=False,
                    hit_vector=False,
                    best_section_text=None,
                )

            # Track A1-Sec: Section-level strict FTS5 (boosts dense single-section matches)
            best_sec_by_page: dict[str, tuple[float, str]] = {}
            for row in _run_section_fts_query(conn, strict_expr, filter_sql, filter_params):
                sec_score = max(0.1, -float(row["raw_bm25"]))
                if row["heading"].strip() in _NAV_HEADINGS:
                    sec_score *= 0.35
                rp = row["rel_path"]
                prev = best_sec_by_page.get(rp)
                if prev is None or sec_score > prev[0]:
                    best_sec_by_page[rp] = (sec_score, f"{row['heading']}\n{row['content']}")
                if rp not in hits:
                    hits[rp] = _make_hit_entry(
                        row,
                        score=sec_score * section_boost,
                        hit_fts=True,
                        hit_strict=True,
                        hit_like=False,
                        hit_section=True,
                        hit_vector=False,
                        best_section_text=f"{row['heading']}\n{row['content']}",
                    )
            for rp, (sec_score, sec_text) in best_sec_by_page.items():
                if rp in hits and not hits[rp]["hit_section"]:
                    hits[rp]["score"] += sec_score * section_boost
                    hits[rp]["hit_section"] = True
                    hits[rp]["best_section_text"] = sec_text

            # Track B: Exact substring match (full phrase + multi-word all-present, including mechanisms)
            like_sql = (
                "SELECT p.rel_path, p.type, p.title, p.summary, p.body, "
                "p.status, p.visibility, p.is_verified, p.superseded_by, "
                "p.reading_status, p.experience_type, p.origin, "
                "p.layers_json, p.mechanisms_json, "
                "p.tags_json, p.aliases_json FROM wiki_pages p WHERE 1=1"
                + filter_sql
            )
            q_lower = norm_query.lower()
            raw_parts, _ = _extract_query_terms(norm_query)
            part_lowers = [p.lower() for p in raw_parts]

            for row in conn.execute(like_sql, filter_params).fetchall():
                title_l = row["title"].lower()
                aliases_l = row["aliases_json"].lower()
                tags_l = row["tags_json"].lower()
                mech_l = (row["mechanisms_json"] or "").lower()
                summary_l = row["summary"].lower()
                body_l = row["body"].lower()

                bonus = 0.0
                if q_lower in title_l:
                    bonus += w.title * 1.5
                if q_lower in aliases_l:
                    bonus += w.aliases * 1.5
                if q_lower in tags_l:
                    bonus += w.tags * 1.2
                if q_lower in mech_l:
                    bonus += w.mechanisms * 1.3
                if q_lower in summary_l:
                    bonus += w.summary * 1.2
                if q_lower in body_l:
                    bonus += w.body * 3.0

                if bonus == 0.0 and len(part_lowers) > 1:
                    combined = f"{title_l}\n{aliases_l}\n{tags_l}\n{mech_l}\n{summary_l}\n{body_l}"
                    if all(p in combined for p in part_lowers):
                        for p in part_lowers:
                            if p in title_l:
                                bonus += w.title * 0.8
                            if p in aliases_l:
                                bonus += w.aliases * 0.8
                            if p in tags_l:
                                bonus += w.tags * 0.6
                            if p in mech_l:
                                bonus += w.mechanisms * 0.7
                            if p in summary_l:
                                bonus += w.summary * 0.6
                            if p in body_l:
                                bonus += w.body * 1.5

                if bonus > 0.0:
                    rp = row["rel_path"]
                    if rp in hits:
                        hits[rp]["score"] += bonus
                        hits[rp]["hit_like"] = True
                    else:
                        hits[rp] = _make_hit_entry(
                            row,
                            score=bonus,
                            hit_fts=False,
                            hit_strict=False,
                            hit_like=True,
                            hit_section=False,
                            hit_vector=False,
                            best_section_text=None,
                        )

            # Track A2: OR fallback ONLY when strict+exact hits are below threshold
            if len(hits) < min_strict_hits:
                fallback_expr = build_safe_fts_query(norm_query)
                if fallback_expr and fallback_expr != strict_expr:
                    fallback_used = True
                    # For long multi-concept queries (>=3 distinct core tokens), require at least 2
                    # distinct core tokens to match so a single generic word (e.g. "方案") never causes
                    # false-positive hits on completely unrelated queries.
                    core_tokens: list[str] = []
                    seen_core: set[str] = set()
                    for tok in jieba.cut(norm_query, cut_all=False):
                        c = _clean_token(tok).lower()
                        if not c or c in seen_core or (len(c) == 1 and c in _STOP_SINGLE_CHARS):
                            continue
                        seen_core.add(c)
                        core_tokens.append(c)
                    min_core_matches = 2 if len(core_tokens) >= 4 else 1

                    for row in _run_page_fts_query(conn, fallback_expr, w, filter_sql, filter_params):
                        rp = row["rel_path"]
                        if rp in hits:
                            continue
                        if min_core_matches > 1:
                            hay = (
                                f"{row['title']} {row['aliases_json']} {row['tags_json']} "
                                f"{row['mechanisms_json']} {row['summary']} {row['body']}"
                            ).lower()
                            matched_cores = sum(1 for ct in core_tokens if ct in hay)
                            if matched_cores < min_core_matches:
                                pruned_tail_hits += 1
                                continue
                        bm25_score = max(0.1, -float(row["raw_bm25"])) * 0.45
                        hits[rp] = _make_hit_entry(
                            row,
                            score=bm25_score,
                            hit_fts=True,
                            hit_strict=False,
                            hit_like=False,
                            hit_section=False,
                            hit_vector=False,
                            best_section_text=None,
                        )

            # Prune weak OR-only tail noise relative to top_score
            if hits:
                top_score = max(item["score"] for item in hits.values())
                cutoff = top_score * or_min_ratio
                filtered_hits: dict[str, dict[str, Any]] = {}
                for rp, item in hits.items():
                    if not item["hit_strict"] and not item["hit_like"] and item["score"] < cutoff:
                        pruned_tail_hits += 1
                        continue
                    filtered_hits[rp] = item
                hits = filtered_hits

            # Track V (Pluggable Semantic Vector Retrieval + RRF Fusion)
            emb_provider = embedding_provider
            if emb_provider is None and config.retrieval.embedding.enabled:
                emb_provider = EmbeddingProvider(config.retrieval.embedding)
            if emb_provider is not None:
                emb_ready, emb_reason = emb_provider.is_ready()
                if not emb_ready:
                    if emb_reason and emb_reason != "embedding_disabled":
                        degraded_reasons.append(emb_reason)
                else:
                    q_vecs = emb_provider.embed_texts([norm_query], is_query=True)
                    if not q_vecs:
                        degraded_reasons.append(emb_provider.last_error or "embedding_query_failed")
                    else:
                        q_vec = q_vecs[0]
                        emb_model = emb_provider.config.model
                        vec_rows = conn.execute(
                            f"""
                            SELECT
                                e.section_id, e.rel_path, e.vector_blob,
                                s.heading, s.content,
                                p.type, p.title, p.summary, p.body,
                                p.status, p.visibility, p.is_verified, p.superseded_by,
                                p.reading_status, p.experience_type, p.origin,
                                p.layers_json, p.mechanisms_json
                            FROM wiki_section_embeddings e
                            JOIN wiki_sections s ON s.section_id = e.section_id
                            JOIN wiki_pages p ON p.rel_path = e.rel_path
                            WHERE e.model = ? {filter_sql}
                            """,
                            [emb_model, *filter_params],
                        ).fetchall()
                        if vec_rows:
                            vector_used = True
                            best_vec_by_page: dict[str, tuple[float, Any]] = {}
                            for vr in vec_rows:
                                sim = cosine_similarity(q_vec, unpack_vector(vr["vector_blob"]))
                                if sim <= 0.15:
                                    continue
                                rp = vr["rel_path"]
                                prev = best_vec_by_page.get(rp)
                                if prev is None or sim > prev[0]:
                                    best_vec_by_page[rp] = (sim, vr)

                            vec_ranked = sorted(
                                best_vec_by_page.items(), key=lambda x: (-x[1][0], x[0])
                            )[:30]

                            # RRF merge: combine lexical ranks + semantic vector ranks
                            lex_ranked = sorted(
                                hits.values(), key=lambda x: (-x["score"], x["rel_path"])
                            )
                            lex_rank_map = {item["rel_path"]: idx + 1 for idx, item in enumerate(lex_ranked)}
                            vec_rank_map = {rp: idx + 1 for idx, (rp, _val) in enumerate(vec_ranked)}

                            for rp, (_sim, vr) in vec_ranked:
                                if rp not in hits:
                                    hits[rp] = _make_hit_entry(
                                        vr,
                                        score=0.0,
                                        hit_fts=False,
                                        hit_strict=False,
                                        hit_like=False,
                                        hit_section=True,
                                        hit_vector=True,
                                        best_section_text=f"{vr['heading']}\n{vr['content']}",
                                    )
                                else:
                                    hits[rp]["hit_vector"] = True

                            for rp, item in hits.items():
                                rrf_score = 0.0
                                if rp in lex_rank_map:
                                    rrf_score += 1.0 / (rrf_k + lex_rank_map[rp])
                                if rp in vec_rank_map:
                                    rrf_score += 1.0 / (rrf_k + vec_rank_map[rp])
                                item["score"] = rrf_score * 100.0
                        else:
                            degraded_reasons.append("no_cached_embeddings_run_reindex")

            # Mode-specific gentle boosting:
            # - student: boost human-verified pages
            # - creator: gently boost firsthand non-evidence pages for personal writing priority
            #            without inflating factual evidence weight
            if eff_mode == "student":
                for item in hits.values():
                    if item["is_verified"]:
                        item["score"] *= verified_mult
            else:
                for item in hits.values():
                    if item["experience_type"] == "firsthand" and item["type"] != "evidence":
                        item["score"] *= firsthand_mult

        ranked_direct = sorted(
            hits.values(),
            key=lambda x: (-x["score"], x["rel_path"]),
        )

        # Track R (Pluggable Cloud Reranker on Top-N candidates)
        rr_provider = reranker_provider
        if rr_provider is None and config.retrieval.reranker.enabled:
            rr_provider = RerankerProvider(config.retrieval.reranker)
        if rr_provider is not None and norm_query and ranked_direct:
            rr_ready, rr_reason = rr_provider.is_ready()
            if not rr_ready:
                if rr_reason and rr_reason != "reranker_disabled":
                    degraded_reasons.append(rr_reason)
            else:
                top_n = min(len(ranked_direct), rr_provider.config.top_n)
                candidates = ranked_direct[:top_n]
                docs_for_rerank = [
                    f"{c['title']}\n{c['summary']}\n{(c.get('best_section_text') or c['body'][:800])}"
                    for c in candidates
                ]
                rr_results = rr_provider.rerank(norm_query, docs_for_rerank, top_n=top_n)
                if rr_results is None:
                    degraded_reasons.append(rr_provider.last_error or "rerank_failed")
                elif rr_results:
                    reranker_used = True
                    reordered: list[dict[str, Any]] = []
                    used_indices: set[int] = set()
                    for r in rr_results:
                        idx = r["index"]
                        if 0 <= idx < len(candidates) and idx not in used_indices:
                            used_indices.add(idx)
                            cand = candidates[idx]
                            rel_score = float(r["relevance_score"])
                            if top_rerank_score is None or rel_score > top_rerank_score:
                                top_rerank_score = rel_score
                            cand["score"] = rel_score * (
                                verified_mult if (eff_mode == "student" and cand["is_verified"]) else 1.0
                            )
                            reordered.append(cand)
                    remainder = [c for i, c in enumerate(candidates) if i not in used_indices]
                    ranked_direct = reordered + remainder + ranked_direct[top_n:]

        direct_paths = {item["rel_path"] for item in ranked_direct}
        seed_paths = [item["rel_path"] for item in ranked_direct[:graph_seed_top_k]]

        # Track C: Bidirectional 1-hop graph neighbors seeded ONLY from top-K direct hits
        # Separated into confirmed relations (graph_neighbors / confirmed_relations) vs candidate_relations
        # while strictly excluding rejected relations.
        graph_neighbors: list[dict[str, Any]] = []
        candidate_neighbors: list[dict[str, Any]] = []
        confirmed_seen: set[str] = set()
        candidate_seen: set[str] = set()
        if seed_paths:
            placeholders = ",".join("?" for _ in seed_paths)
            seed_list = sorted(seed_paths)

            out_rows = conn.execute(
                f"""
                SELECT l.source_path, l.target_path, l.relation_type, l.relation_status,
                       l.relation_layer, l.relation_reason,
                       p.type, p.title, p.summary, p.body
                FROM wiki_links l
                JOIN wiki_pages p ON p.rel_path = l.target_path
                WHERE l.source_path IN ({placeholders})
                  AND l.is_broken = 0
                  AND l.relation_status != 'rejected' {filter_sql}
                """,
                [*seed_list, *filter_params],
            ).fetchall()
            for r in out_rows:
                tp = r["target_path"]
                rel_status = (r["relation_status"] or "confirmed").lower()
                entry = {
                    "rel_path": tp,
                    "type": r["type"],
                    "title": r["title"],
                    "summary": r["summary"],
                    "body": r["body"],
                    "linked_via": r["source_path"],
                    "direction": "outbound",
                    "relation_type": r["relation_type"] or "mentions",
                    "relation_status": rel_status,
                    "relation_layer": r["relation_layer"] or "",
                    "relation_reason": r["relation_reason"] or "",
                }
                if rel_status == "candidate":
                    if tp not in direct_paths and tp not in candidate_seen:
                        candidate_seen.add(tp)
                        candidate_neighbors.append(entry)
                else:
                    if tp not in direct_paths and tp not in confirmed_seen:
                        confirmed_seen.add(tp)
                        graph_neighbors.append(entry)

            in_rows = conn.execute(
                f"""
                SELECT l.source_path, l.target_path, l.relation_type, l.relation_status,
                       l.relation_layer, l.relation_reason,
                       p.type, p.title, p.summary, p.body
                FROM wiki_links l
                JOIN wiki_pages p ON p.rel_path = l.source_path
                WHERE l.target_path IN ({placeholders})
                  AND l.is_broken = 0
                  AND l.relation_status != 'rejected' {filter_sql}
                """,
                [*seed_list, *filter_params],
            ).fetchall()
            for r in in_rows:
                sp = r["source_path"]
                rel_status = (r["relation_status"] or "confirmed").lower()
                entry = {
                    "rel_path": sp,
                    "type": r["type"],
                    "title": r["title"],
                    "summary": r["summary"],
                    "body": r["body"],
                    "linked_via": r["target_path"],
                    "direction": "inbound",
                    "relation_type": r["relation_type"] or "mentions",
                    "relation_status": rel_status,
                    "relation_layer": r["relation_layer"] or "",
                    "relation_reason": r["relation_reason"] or "",
                }
                if rel_status == "candidate":
                    if sp not in direct_paths and sp not in candidate_seen:
                        candidate_seen.add(sp)
                        candidate_neighbors.append(entry)
                else:
                    if sp not in direct_paths and sp not in confirmed_seen:
                        confirmed_seen.add(sp)
                        graph_neighbors.append(entry)

        # Student mode confidence check
        confidence_warning: str | None = None
        if eff_mode == "student":
            if not ranked_direct:
                confidence_warning = "LOW_CONFIDENCE"
            elif reranker_used and top_rerank_score is not None:
                if top_rerank_score < config.retrieval.reranker.min_score_student:
                    confidence_warning = "LOW_CONFIDENCE"

        total_direct = len(ranked_direct)
        total_neighbors = len(graph_neighbors)
        total_hits = total_direct + total_neighbors
        strict_hits_count = sum(1 for item in ranked_direct if item["hit_strict"] or item["hit_like"])

        l1_slice = ranked_direct[offset : offset + eff_l1_limit]
        truncated = (offset + eff_l1_limit) < total_direct

        l1_inventory: list[dict[str, Any]] = []
        for item in l1_slice:
            src_parts = []
            if item["hit_fts"]:
                src_parts.append("fts")
            if item["hit_like"]:
                src_parts.append("like")
            if item["hit_vector"]:
                src_parts.append("vector")
            src = "+".join(src_parts) or "fts"

            if item["hit_vector"] and (item["hit_strict"] or item["hit_like"]):
                tier = "hybrid+strict"
            elif item["hit_like"] and item["hit_strict"]:
                tier = "exact+strict"
            elif item["hit_strict"]:
                tier = "strict"
            elif item["hit_like"]:
                tier = "exact"
            elif item["hit_vector"]:
                tier = "semantic_vector"
            else:
                tier = "fallback_or"

            l1_inventory.append(
                {
                    "rel_path": item["rel_path"],
                    "type": item["type"],
                    "title": item["title"],
                    "summary": item["summary"],
                    "status": item["status"],
                    "visibility": item["visibility"],
                    "is_verified": item["is_verified"],
                    "reading_status": item["reading_status"],
                    "experience_type": item["experience_type"],
                    "origin": item["origin"],
                    "layers": item["layers"],
                    "mechanisms": item["mechanisms"],
                    "hit_source": src,
                    "match_tier": tier,
                    "score": round(item["score"], 3),
                    "snippet": _make_snippet(item["body"], norm_query),
                }
            )

        l2_full_docs: list[dict[str, Any]] = []
        for item in ranked_direct[:eff_top_k]:
            l2_full_docs.append(
                {
                    "rel_path": item["rel_path"],
                    "type": item["type"],
                    "title": item["title"],
                    "status": item["status"],
                    "visibility": item["visibility"],
                    "is_verified": item["is_verified"],
                    "superseded_by": item["superseded_by"],
                    "reading_status": item["reading_status"],
                    "experience_type": item["experience_type"],
                    "origin": item["origin"],
                    "layers": item["layers"],
                    "mechanisms": item["mechanisms"],
                    "sources": _extract_page_sources(root_dir, item["rel_path"]),
                    "matched_sections": _extract_matched_sections(
                        item["body"], norm_query, max_sections=max_matched_sections
                    ),
                    "body": item["body"],
                }
            )

        evidences_with_sources: list[dict[str, Any]] = []
        ev_seen: set[str] = set()
        for item in [*ranked_direct, *graph_neighbors]:
            if len(evidences_with_sources) >= max_evidences:
                break
            if item["type"] != "evidence" or item["rel_path"] in ev_seen:
                continue
            ev_seen.add(item["rel_path"])
            evidences_with_sources.append(
                {
                    "rel_path": item["rel_path"],
                    "title": item["title"],
                    "summary": item["summary"],
                    "sources": _extract_page_sources(root_dir, item["rel_path"]),
                    "body": item["body"],
                }
            )

        def _format_neighbor(n: dict[str, Any]) -> dict[str, Any]:
            return {
                "rel_path": n["rel_path"],
                "type": n["type"],
                "title": n["title"],
                "summary": n["summary"],
                "linked_via": n["linked_via"],
                "direction": n["direction"],
                "relation_type": n["relation_type"],
                "relation_status": n["relation_status"],
                "relation_layer": n["relation_layer"],
                "relation_reason": n["relation_reason"],
            }

        public_neighbors = [_format_neighbor(n) for n in graph_neighbors]
        public_candidates = [_format_neighbor(n) for n in candidate_neighbors]

        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        if record_telemetry and norm_query:
            record_query_telemetry(
                conn,
                query_text=norm_query,
                mode=eff_mode,
                direct_hits=total_direct,
                strict_hits=strict_hits_count,
                fallback_used=fallback_used,
                confidence_warning=confidence_warning,
                top_score=ranked_direct[0]["score"] if ranked_direct else 0.0,
                latency_ms=latency_ms,
            )
        return {
            "ok": True,
            "query": norm_query,
            "mode": eff_mode,
            "confidence_warning": confidence_warning,
            "latency_ms": latency_ms,
            "stats": {
                "total_hits": total_hits,
                "direct_hits": total_direct,
                "strict_hits": strict_hits_count,
                "graph_neighbors": total_neighbors,
                "candidate_relations": len(public_candidates),
                "l1_returned": len(l1_inventory),
                "l2_expanded": len(l2_full_docs),
                "evidences_expanded": len(evidences_with_sources),
                "hybrid_mode": "hybrid" if vector_used else "lexical",
                "vector_used": vector_used,
                "reranker_used": reranker_used,
                "degraded_reasons": degraded_reasons,
                "fallback_used": fallback_used,
                "pruned_tail_hits": pruned_tail_hits,
                "truncated": truncated,
            },
            "l1_inventory": l1_inventory,
            "l2_full_docs": l2_full_docs,
            "evidences_with_sources": evidences_with_sources,
            "graph_neighbors": public_neighbors,
            "confirmed_relations": public_neighbors,
            "candidate_relations": public_candidates,
        }
    finally:
        conn.close()
