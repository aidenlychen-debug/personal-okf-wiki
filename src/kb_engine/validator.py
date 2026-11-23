"""OKF v0.3 schema, link safety, human-block, verified append-only & deletion/shrinkage validator."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from kb_engine.config import KBConfig
from kb_engine.errors import ErrorCode
from kb_engine.indexer import iter_wiki_content_files
from kb_engine.utils import (
    classify_and_resolve_link,
    compute_effective_char_count,
    extract_human_blocks,
    extract_markdown_links,
    find_wikilinks,
    git_list_tracked_files,
    git_show_file,
    normalize_newlines,
    normalize_rel_path,
    parse_frontmatter,
)

VALID_TYPES = {"source", "concept", "case", "evidence", "insight"}
VALID_STATUSES = {"draft", "stable", "deprecated"}
VALID_VISIBILITIES = {"private", "team", "public"}
VALID_READING_STATUSES = {"read", "partial", "unread"}
VALID_EXPERIENCE_TYPES = {"firsthand", "observation", "others"}
VALID_ORIGINS = {"human", "mixed", "model_candidate"}
VALID_LAYERS = {"L1", "L2", "L3", "L4", "L5", "L6"}
VALID_RELATION_STATUSES = {"confirmed", "candidate", "rejected"}


def _is_verified_non_empty(meta: dict[str, Any] | None) -> bool:
    if not meta:
        return False
    val = meta.get("verified")
    if isinstance(val, list):
        return len(val) > 0
    if isinstance(val, str):
        return bool(val.strip())
    return val is True


def _normalize_allow_set(items: list[str] | None) -> set[str]:
    if not items:
        return set()
    result: set[str] = set()
    for item in items:
        norm = normalize_rel_path(item)
        result.add(norm)
        if norm.startswith("wiki/"):
            result.add(norm[5:])
        else:
            result.add(f"wiki/{norm}")
    return result


def lint_wiki(
    root_dir: Path,
    config: KBConfig,
    *,
    base_commit: str | None = None,
    allow_shrinkage: list[str] | None = None,
    allow_delete: list[str] | None = None,
) -> dict[str, Any]:
    """Run full read-only governance validation across wiki/."""
    wiki_dir = root_dir / "wiki"
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    allow_shrink_set = _normalize_allow_set(allow_shrinkage)
    allow_delete_set = _normalize_allow_set(allow_delete)

    # 1. Check root wiki/index.md okf_version == "0.3"
    index_path = wiki_dir / "index.md"
    if not index_path.exists():
        errors.append(
            {
                "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                "path": "wiki/index.md",
                "message": "缺少根目录 wiki/index.md",
            }
        )
    else:
        idx_meta, _idx_body, idx_err = parse_frontmatter(index_path.read_text(encoding="utf-8"))
        if idx_err or not idx_meta or str(idx_meta.get("okf_version", "")) != "0.3":
            errors.append(
                {
                    "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                    "path": "wiki/index.md",
                    "message": '根目录 wiki/index.md 必须声明 okf_version: "0.3"',
                }
            )

    content_files = iter_wiki_content_files(root_dir)
    inbound_counts: dict[str, int] = {}
    page_types: dict[str, str] = {}
    today = date.today()

    for file_path in content_files:
        rel_wiki = normalize_rel_path(file_path.relative_to(wiki_dir))
        rel_root = f"wiki/{rel_wiki}"
        inbound_counts.setdefault(rel_wiki, 0)

        text = normalize_newlines(file_path.read_text(encoding="utf-8"))
        meta, body, parse_err = parse_frontmatter(text)

        # 2. Frontmatter schema validation
        if parse_err:
            errors.append(
                {
                    "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                    "path": rel_root,
                    "message": f"Frontmatter 解析错误: {parse_err}",
                }
            )
            continue
        if meta is None:
            errors.append(
                {
                    "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                    "path": rel_root,
                    "message": "页面缺少 YAML Frontmatter",
                }
            )
            continue

        if "okf_version" in meta:
            errors.append(
                {
                    "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                    "path": rel_root,
                    "message": "仅根目录 wiki/index.md 允许声明 okf_version，子页面禁止误写",
                }
            )

        ptype = str(meta.get("type") or "").strip()
        title = str(meta.get("title") or "").strip()
        summary = str(meta.get("summary") or "").strip()
        page_types[rel_wiki] = ptype

        if ptype not in VALID_TYPES:
            errors.append(
                {
                    "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                    "path": rel_root,
                    "message": f"无效或缺失的 type 字段: '{ptype}'（允许值: {sorted(VALID_TYPES)}）",
                }
            )
        if not title:
            errors.append(
                {
                    "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                    "path": rel_root,
                    "message": "缺失必填字段 title",
                }
            )
        if not summary:
            errors.append(
                {
                    "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                    "path": rel_root,
                    "message": "缺失必填字段 summary",
                }
            )

        status = meta.get("status")
        if status is not None and str(status) not in VALID_STATUSES:
            errors.append(
                {
                    "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                    "path": rel_root,
                    "message": f"无效的 status 字段: '{status}'",
                }
            )

        visibility = meta.get("visibility")
        if visibility is not None and str(visibility) not in VALID_VISIBILITIES:
            errors.append(
                {
                    "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                    "path": rel_root,
                    "message": f"无效的 visibility 字段: '{visibility}'",
                }
            )

        # Optional V3.0 metadata validation (strictly additive, backward-compatible)
        reading_status = meta.get("reading_status")
        if reading_status is not None and str(reading_status).strip() and str(reading_status).strip().lower() not in VALID_READING_STATUSES:
            errors.append(
                {
                    "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                    "path": rel_root,
                    "message": f"无效的 reading_status 字段: '{reading_status}'（允许值: {sorted(VALID_READING_STATUSES)}）",
                }
            )

        experience_type = meta.get("experience_type")
        if experience_type is not None and str(experience_type).strip() and str(experience_type).strip().lower() not in VALID_EXPERIENCE_TYPES:
            errors.append(
                {
                    "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                    "path": rel_root,
                    "message": f"无效的 experience_type 字段: '{experience_type}'（允许值: {sorted(VALID_EXPERIENCE_TYPES)}）",
                }
            )

        origin = meta.get("origin")
        if origin is not None and str(origin).strip() and str(origin).strip().lower() not in VALID_ORIGINS:
            errors.append(
                {
                    "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                    "path": rel_root,
                    "message": f"无效的 origin 字段: '{origin}'（允许值: {sorted(VALID_ORIGINS)}）",
                }
            )

        layers = meta.get("layers")
        if layers is not None:
            layer_list = layers if isinstance(layers, list) else [layers]
            for lv in layer_list:
                lv_norm = str(lv).strip().upper()
                if lv_norm and lv_norm not in VALID_LAYERS:
                    errors.append(
                        {
                            "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                            "path": rel_root,
                            "message": f"无效的 layers 层级值: '{lv}'（允许值: L1-L6）",
                        }
                    )

        # Stale warning
        stale_val = meta.get("stale_after")
        if stale_val:
            try:
                stale_date = (
                    stale_val if isinstance(stale_val, date) else date.fromisoformat(str(stale_val))
                )
                if stale_date < today:
                    warnings.append(
                        {
                            "code": "STALE_PAGE",
                            "path": rel_root,
                            "message": f"页面已超过复审日期 stale_after ({stale_date.isoformat()})",
                        }
                    )
            except ValueError:
                warnings.append(
                    {
                        "code": "INVALID_STALE_DATE",
                        "path": rel_root,
                        "message": f"stale_after 日期格式不规范: {stale_val}",
                    }
                )

        # 3. Forbidden [[wikilinks]] check
        wikilinks = find_wikilinks(body)
        if wikilinks:
            errors.append(
                {
                    "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                    "path": rel_root,
                    "message": f"禁止使用 Obsidian [[wikilinks]] 语法: {wikilinks[:3]}",
                }
            )

        # 4. Local relative link existence check (ignore external URLs & pure #anchors)
        for is_image, _label, raw_target in extract_markdown_links(body):
            resolved = classify_and_resolve_link(file_path, raw_target, is_image, root_dir)
            if resolved.kind in ("external", "anchor"):
                continue
            if not resolved.target_abs_path or not resolved.target_abs_path.exists():
                errors.append(
                    {
                        "code": ErrorCode.BROKEN_LINK.value,
                        "path": rel_root,
                        "link": raw_target,
                        "message": f"本地相对链接目标不存在: {raw_target}",
                    }
                )
            elif resolved.kind == "wiki" and resolved.target_rel_path:
                inbound_counts[resolved.target_rel_path] = inbound_counts.get(resolved.target_rel_path, 0) + 1

        # Also validate YAML superseded_by relative link if present
        superseded_by = meta.get("superseded_by")
        if superseded_by and isinstance(superseded_by, str):
            resolved_sup = classify_and_resolve_link(file_path, superseded_by, False, root_dir)
            if resolved_sup.kind not in ("external", "anchor"):
                if not resolved_sup.target_abs_path or not resolved_sup.target_abs_path.exists():
                    errors.append(
                        {
                            "code": ErrorCode.BROKEN_LINK.value,
                            "path": rel_root,
                            "link": superseded_by,
                            "message": f"Frontmatter superseded_by 目标不存在: {superseded_by}",
                        }
                    )

        # Also validate YAML sources[].ref
        sources = meta.get("sources")
        if isinstance(sources, list):
            for s in sources:
                ref = s.get("ref") if isinstance(s, dict) else None
                if ref and isinstance(ref, str):
                    resolved = classify_and_resolve_link(file_path, ref, False, root_dir)
                    if resolved.kind not in ("external", "anchor"):
                        if not resolved.target_abs_path or not resolved.target_abs_path.exists():
                            errors.append(
                                {
                                    "code": ErrorCode.BROKEN_LINK.value,
                                    "path": rel_root,
                                    "link": ref,
                                    "message": f"Frontmatter sources.ref 目标不存在: {ref}",
                                }
                            )

        # Validate optional YAML relations[] typed links
        relations = meta.get("relations")
        if isinstance(relations, list):
            for rel_item in relations:
                if not isinstance(rel_item, dict):
                    continue
                rel_status = str(rel_item.get("status") or "confirmed").strip().lower()
                if rel_status and rel_status not in VALID_RELATION_STATUSES:
                    errors.append(
                        {
                            "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                            "path": rel_root,
                            "message": f"无效的 relations.status: '{rel_status}'（允许值: {sorted(VALID_RELATION_STATUSES)}）",
                        }
                    )
                rel_layer = str(rel_item.get("layer") or "").strip().upper()
                if rel_layer and rel_layer not in VALID_LAYERS:
                    errors.append(
                        {
                            "code": ErrorCode.OKF_SCHEMA_ERROR.value,
                            "path": rel_root,
                            "message": f"无效的 relations.layer: '{rel_layer}'（允许值: L1-L6）",
                        }
                    )
                target = rel_item.get("target")
                if target and isinstance(target, str):
                    resolved = classify_and_resolve_link(file_path, target, False, root_dir)
                    if resolved.kind not in ("external", "anchor"):
                        if not resolved.target_abs_path or not resolved.target_abs_path.exists():
                            errors.append(
                                {
                                    "code": ErrorCode.BROKEN_LINK.value,
                                    "path": rel_root,
                                    "link": target,
                                    "message": f"Frontmatter relations.target 目标不存在: {target}",
                                }
                            )
                        elif resolved.kind == "wiki" and resolved.target_rel_path and rel_status != "rejected":
                            inbound_counts[resolved.target_rel_path] = inbound_counts.get(resolved.target_rel_path, 0) + 1

    # 5. Orphan warnings (non-source pages with 0 inbound links)
    for rel_wiki, count in sorted(inbound_counts.items()):
        if count == 0 and page_types.get(rel_wiki) != "source":
            warnings.append(
                {
                    "code": "ORPHAN_PAGE",
                    "path": f"wiki/{rel_wiki}",
                    "message": "孤儿页面：当前无其他 wiki 页面链入该页",
                }
            )

    # 6. Four hard safety guards relative to base_commit
    if base_commit:
        tracked_wiki = git_list_tracked_files(root_dir, base_commit, prefix="wiki")
        for tracked_rel in tracked_wiki:
            if not tracked_rel.endswith(".md"):
                continue
            if tracked_rel in ("wiki/index.md", "wiki/log.md"):
                continue

            disk_path = root_dir / tracked_rel
            old_text = git_show_file(root_dir, base_commit, tracked_rel)
            if old_text is None:
                continue

            # Guard A: Unexpected deletion
            if not disk_path.exists():
                if config.safety.block_unexpected_deletions and tracked_rel not in allow_delete_set:
                    errors.append(
                        {
                            "code": ErrorCode.UNEXPECTED_FILE_DELETED.value,
                            "path": tracked_rel,
                            "message": f"已有 wiki 页面被意外删除（如需删除请显式传入 --allow-delete）: {tracked_rel}",
                        }
                    )
                continue

            new_text = normalize_newlines(disk_path.read_text(encoding="utf-8"))
            old_norm = normalize_newlines(old_text)

            # Guard B: Human-owned block byte-for-byte integrity
            if config.safety.enforce_human_blocks:
                old_blocks = extract_human_blocks(old_norm)
                new_blocks = extract_human_blocks(new_text)
                if old_blocks != new_blocks:
                    errors.append(
                        {
                            "code": ErrorCode.HUMAN_BLOCK_MODIFIED.value,
                            "path": tracked_rel,
                            "message": f"检测到 <!-- human:start --> 人类禁改区块被修改: {tracked_rel}",
                        }
                    )

            old_meta, old_body, _ = parse_frontmatter(old_norm)
            _new_meta, new_body, _ = parse_frontmatter(new_text)

            # Guard C: Verified page non-strict prefix append-only check
            if config.safety.enforce_verified_append_only and _is_verified_non_empty(old_meta):
                if not new_body.startswith(old_body.rstrip()):
                    errors.append(
                        {
                            "code": ErrorCode.VERIFIED_CONTENT_MODIFIED.value,
                            "path": tracked_rel,
                            "message": f"已验证页面（verified 非空）仅允许保持不变或在文末追加，禁止删改既有正文: {tracked_rel}",
                        }
                    )

            # Guard D: Content shrinkage guard (> max_shrinkage_ratio)
            old_chars = compute_effective_char_count(old_body)
            new_chars = compute_effective_char_count(new_body)
            if old_chars > 0 and tracked_rel not in allow_shrink_set:
                shrink_ratio = (old_chars - new_chars) / old_chars
                if shrink_ratio > config.safety.max_shrinkage_ratio:
                    errors.append(
                        {
                            "code": ErrorCode.CONTENT_SHRINKAGE.value,
                            "path": tracked_rel,
                            "old_chars": old_chars,
                            "new_chars": new_chars,
                            "shrink_ratio": round(shrink_ratio, 4),
                            "message": (
                                f"页面正文有效字符数缩水 {shrink_ratio:.1%}（超过阈值 {config.safety.max_shrinkage_ratio:.0%}）: "
                                f"{tracked_rel}"
                            ),
                        }
                    )

    return {
        "ok": len(errors) == 0,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
    }
