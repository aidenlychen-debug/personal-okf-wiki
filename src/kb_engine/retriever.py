"""Dual-track Chinese retriever (BM25 + LIKE + 1-hop graph) with L1/L2 budget control."""

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
from kb_engine.utils import normalize_nfc, parse_frontmatter

jieba.setLogLevel(logging.ERROR)

_TOKEN_CLEAN_RE = re.compile(r"[^\w\u4e00-\u9fff\-./+]+")


def build_safe_fts_query(query: str) -> str:
    """Segment query with jieba and double-quote escape every token for FTS5 safety."""
    norm = normalize_nfc(query).strip()
    if not norm:
        return ""
    tokens: list[str] = []
    seen: set[str] = set()

    # Preserve compound hyphenated/alphanumeric terms verbatim as well
    for raw_part in re.split(r"\s+", norm):
        part_clean = _TOKEN_CLEAN_RE.sub("", raw_part).strip()
        if part_clean and part_clean.lower() not in seen:
            seen.add(part_clean.lower())
            tokens.append(part_clean)

    for tok in jieba.cut_for_search(norm):
        cleaned = _TOKEN_CLEAN_RE.sub("", tok).strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            tokens.append(cleaned)

    if not tokens:
        return ""
    escaped = ['"' + t.replace('"', '""') + '"' for t in tokens]
    return " OR ".join(escaped)


def _make_snippet(body: str, query: str, max_len: int = 120) -> str:
    compact = re.sub(r"\s+", " ", body).strip()
    if not compact:
        return ""
    lower_body = compact.lower()
    lower_q = query.strip().lower()
    idx = lower_body.find(lower_q) if lower_q else -1
    if idx == -1:
        # Try first jieba token
        for tok in jieba.cut_for_search(query):
            t = tok.strip().lower()
            if len(t) >= 2:
                idx = lower_body.find(t)
                if idx != -1:
                    break
    if idx == -1:
        return compact[:max_len] + ("..." if len(compact) > max_len else "")
    start = max(0, idx - 35)
    end = min(len(compact), idx + max_len - 35)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(compact) else ""
    return f"{prefix}{compact[start:end]}{suffix}"


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


def query_wiki(
    root_dir: Path,
    config: KBConfig,
    query: str,
    *,
    page_type: str | None = None,
    l1_limit: int | None = None,
    offset: int = 0,
    top_k: int | None = None,
) -> dict[str, Any]:
    """Execute lock-free read-only dual-track + 1-hop graph retrieval."""
    t0 = time.perf_counter()
    norm_query = normalize_nfc(query).strip()
    eff_l1_limit = l1_limit if l1_limit is not None else config.retrieval.default_l1_limit
    eff_top_k = top_k if top_k is not None else config.retrieval.default_l2_top_k
    max_evidences = config.retrieval.max_expanded_evidences
    w = config.retrieval.fts_weights

    conn = connect_db(root_dir, ensure_schema=True)
    try:
        hits: dict[str, dict[str, Any]] = {}

        if norm_query:
            # Track A: FTS5 BM25 weighted search
            fts_expr = build_safe_fts_query(norm_query)
            if fts_expr:
                sql = """
                    SELECT
                        p.rel_path, p.type, p.title, p.summary, p.body,
                        p.tags_json, p.aliases_json,
                        bm25(wiki_fts, 0.0, ?, ?, ?, ?, ?) AS raw_bm25
                    FROM wiki_fts
                    JOIN wiki_pages p ON p.rel_path = wiki_fts.rel_path
                    WHERE wiki_fts MATCH ?
                """
                params: list[Any] = [w.title, w.aliases, w.tags, w.summary, w.body, fts_expr]
                if page_type:
                    sql += " AND p.type = ?"
                    params.append(page_type)
                rows = conn.execute(sql, params).fetchall()
                for row in rows:
                    bm25_score = max(0.1, -float(row["raw_bm25"]))
                    hits[row["rel_path"]] = {
                        "rel_path": row["rel_path"],
                        "type": row["type"],
                        "title": row["title"],
                        "summary": row["summary"],
                        "body": row["body"],
                        "score": bm25_score,
                        "hit_fts": True,
                        "hit_like": False,
                    }

            # Track B: Case-insensitive exact substring fallback (guarantees 2-char CJK & acronyms)
            like_sql = "SELECT rel_path, type, title, summary, body, tags_json, aliases_json FROM wiki_pages"
            like_params: list[Any] = []
            if page_type:
                like_sql += " WHERE type = ?"
                like_params.append(page_type)
            q_lower = norm_query.lower()
            for row in conn.execute(like_sql, like_params).fetchall():
                bonus = 0.0
                if q_lower in row["title"].lower():
                    bonus += w.title * 1.5
                if q_lower in row["aliases_json"].lower():
                    bonus += w.aliases * 1.5
                if q_lower in row["tags_json"].lower():
                    bonus += w.tags * 1.2
                if q_lower in row["summary"].lower():
                    bonus += w.summary * 1.2
                if q_lower in row["body"].lower():
                    bonus += w.body * 3.0
                if bonus > 0.0:
                    rp = row["rel_path"]
                    if rp in hits:
                        hits[rp]["score"] += bonus
                        hits[rp]["hit_like"] = True
                    else:
                        hits[rp] = {
                            "rel_path": rp,
                            "type": row["type"],
                            "title": row["title"],
                            "summary": row["summary"],
                            "body": row["body"],
                            "score": bonus,
                            "hit_fts": False,
                            "hit_like": True,
                        }

        # Sort direct hits by score desc, then rel_path asc for determinism
        ranked_direct = sorted(
            hits.values(),
            key=lambda x: (-x["score"], x["rel_path"]),
        )
        direct_paths = {item["rel_path"] for item in ranked_direct}

        # Track C: Bidirectional 1-hop graph neighbors
        graph_neighbors: list[dict[str, Any]] = []
        neighbor_seen: set[str] = set()
        if direct_paths:
            placeholders = ",".join("?" for _ in direct_paths)
            direct_list = sorted(direct_paths)

            # Outbound links: direct -> neighbor
            out_rows = conn.execute(
                f"""
                SELECT l.source_path, l.target_path, p.type, p.title, p.summary, p.body
                FROM wiki_links l
                JOIN wiki_pages p ON p.rel_path = l.target_path
                WHERE l.source_path IN ({placeholders}) AND l.is_broken = 0
                """,
                direct_list,
            ).fetchall()
            for r in out_rows:
                tp = r["target_path"]
                if tp in direct_paths or tp in neighbor_seen:
                    continue
                neighbor_seen.add(tp)
                graph_neighbors.append(
                    {
                        "rel_path": tp,
                        "type": r["type"],
                        "title": r["title"],
                        "summary": r["summary"],
                        "body": r["body"],
                        "linked_via": r["source_path"],
                        "direction": "outbound",
                    }
                )

            # Inbound links: neighbor -> direct
            in_rows = conn.execute(
                f"""
                SELECT l.source_path, l.target_path, p.type, p.title, p.summary, p.body
                FROM wiki_links l
                JOIN wiki_pages p ON p.rel_path = l.source_path
                WHERE l.target_path IN ({placeholders}) AND l.is_broken = 0
                """,
                direct_list,
            ).fetchall()
            for r in in_rows:
                sp = r["source_path"]
                if sp in direct_paths or sp in neighbor_seen:
                    continue
                neighbor_seen.add(sp)
                graph_neighbors.append(
                    {
                        "rel_path": sp,
                        "type": r["type"],
                        "title": r["title"],
                        "summary": r["summary"],
                        "body": r["body"],
                        "linked_via": r["target_path"],
                        "direction": "inbound",
                    }
                )

        # Build L1 inventory with pagination
        total_direct = len(ranked_direct)
        total_neighbors = len(graph_neighbors)
        total_hits = total_direct + total_neighbors

        l1_slice = ranked_direct[offset : offset + eff_l1_limit]
        truncated = (offset + eff_l1_limit) < total_direct

        l1_inventory: list[dict[str, Any]] = []
        for item in l1_slice:
            if item["hit_fts"] and item["hit_like"]:
                src = "fts+like"
            elif item["hit_fts"]:
                src = "fts"
            else:
                src = "like"
            l1_inventory.append(
                {
                    "rel_path": item["rel_path"],
                    "type": item["type"],
                    "title": item["title"],
                    "summary": item["summary"],
                    "hit_source": src,
                    "score": round(item["score"], 3),
                    "snippet": _make_snippet(item["body"], norm_query),
                }
            )

        # Build L2 full docs (top_k highest-scoring direct hits)
        l2_full_docs: list[dict[str, Any]] = []
        for item in ranked_direct[:eff_top_k]:
            l2_full_docs.append(
                {
                    "rel_path": item["rel_path"],
                    "type": item["type"],
                    "title": item["title"],
                    "sources": _extract_page_sources(root_dir, item["rel_path"]),
                    "body": item["body"],
                }
            )

        # Expand matching evidences (from direct hits first, then 1-hop neighbors) up to max_evidences
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

        public_neighbors = [
            {
                "rel_path": n["rel_path"],
                "type": n["type"],
                "title": n["title"],
                "summary": n["summary"],
                "linked_via": n["linked_via"],
                "direction": n["direction"],
            }
            for n in graph_neighbors
        ]

        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return {
            "ok": True,
            "query": norm_query,
            "latency_ms": latency_ms,
            "stats": {
                "total_hits": total_hits,
                "direct_hits": total_direct,
                "graph_neighbors": total_neighbors,
                "l1_returned": len(l1_inventory),
                "l2_expanded": len(l2_full_docs),
                "evidences_expanded": len(evidences_with_sources),
                "truncated": truncated,
            },
            "l1_inventory": l1_inventory,
            "l2_full_docs": l2_full_docs,
            "evidences_with_sources": evidences_with_sources,
            "graph_neighbors": public_neighbors,
        }
    finally:
        conn.close()
