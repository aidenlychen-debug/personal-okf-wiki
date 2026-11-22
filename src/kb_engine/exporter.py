"""Phase 3 commercial Skill bundle exporter stub (visibility & domain filtering)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kb_engine.db import connect_db


def export_skill_bundle(
    root_dir: Path,
    output_dir: Path,
    visibility: str = "public",
    domains: list[str] | None = None,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Filter eligible wiki pages by visibility and domain for future Agent Skill export."""
    allowed_visibilities = {
        "public": ("public",),
        "team": ("public", "team"),
        "private": ("public", "team", "private"),
    }.get(visibility, (visibility,))

    conn = connect_db(root_dir, ensure_schema=True)
    try:
        vis_placeholders = ",".join("?" for _ in allowed_visibilities)
        sql = f"""
            SELECT rel_path, type, title, summary, domain, visibility, is_verified
            FROM wiki_pages
            WHERE visibility IN ({vis_placeholders})
        """
        params: list[Any] = list(allowed_visibilities)
        if domains:
            dom_placeholders = ",".join("?" for _ in domains)
            sql += f" AND domain IN ({dom_placeholders})"
            params.extend(domains)
        sql += " ORDER BY type, rel_path"

        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()

    return {
        "ok": True,
        "dry_run": dry_run,
        "phase": "Phase 3 stub",
        "output_dir": str(output_dir),
        "filter": {
            "visibility": visibility,
            "domains": domains or [],
        },
        "matched_count": len(rows),
        "matched_pages": rows,
        "note": "Phase 3 导出打包尚未启用，当前返回通过 visibility/domain 过滤的候选页面清单。",
    }
