"""Adaptive, evidence-gated self-evolution engine (telemetry, saturation detection & volume wake-up)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from kb_engine.config import KBConfig
from kb_engine.db import connect_db, db_session
from kb_engine.errors import ErrorCode, KBError
from kb_engine.utils import utc_now_iso, write_lock
from kb_engine.validator import lint_wiki

VALID_EVOLVE_OUTCOMES = {"evolved", "no_gain", "model_ceiling", "skipped"}


def record_query_telemetry(
    conn: sqlite3.Connection,
    *,
    query_text: str,
    mode: str,
    direct_hits: int,
    strict_hits: int,
    fallback_used: bool,
    confidence_warning: str | None,
    top_score: float,
    latency_ms: float,
) -> None:
    """Best-effort, non-blocking query telemetry insert into .kb/kb.db."""
    if not query_text or not query_text.strip():
        return
    try:
        conn.execute(
            """
            INSERT INTO query_logs (
                query_text, mode, direct_hits, strict_hits, fallback_used,
                confidence_warning, top_score, latency_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                query_text.strip(),
                mode,
                direct_hits,
                strict_hits,
                1 if fallback_used else 0,
                confidence_warning,
                round(float(top_score), 4),
                round(float(latency_ms), 2),
                utc_now_iso(),
            ),
        )
        conn.commit()
    except sqlite3.Error:
        # Never block or fail a read-only query due to telemetry write contention
        pass


def _compute_next_interval(
    current_interval: int,
    outcome: str,
    backoff_intervals: list[int],
    explicit_next: int | None = None,
) -> int:
    if explicit_next is not None and explicit_next >= 1:
        return explicit_next
    tiers = sorted({max(1, int(x)) for x in backoff_intervals}) or [5, 10, 20, 40]
    if outcome == "evolved":
        # Genuine measurable gain: stay responsive (cap at second tier e.g. 10)
        cap = tiers[1] if len(tiers) > 1 else tiers[0]
        return min(max(current_interval, tiers[0]), cap)
    # no_gain / model_ceiling / skipped -> back off to the next larger tier
    for t in tiers:
        if t > current_interval:
            return t
    return tiers[-1]


def _query_window_stats(
    conn: sqlite3.Connection,
    since_iso: str | None,
    since_id: int | None = None,
) -> dict[str, Any]:
    if since_id is not None:
        rows = conn.execute(
            "SELECT * FROM query_logs WHERE id > ? ORDER BY id DESC",
            (since_id,),
        ).fetchall()
    elif since_iso:
        rows = conn.execute(
            "SELECT * FROM query_logs WHERE created_at > ? ORDER BY id DESC",
            (since_iso,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM query_logs ORDER BY id DESC").fetchall()

    total = len(rows)
    if total == 0:
        return {
            "total_queries": 0,
            "zero_hit_count": 0,
            "zero_hit_rate": 0.0,
            "fallback_count": 0,
            "fallback_rate": 0.0,
            "low_confidence_count": 0,
            "avg_latency_ms": 0.0,
            "zero_hit_queries": [],
            "fallback_queries": [],
            "low_confidence_queries": [],
        }

    zero_hits = [r for r in rows if r["direct_hits"] == 0]
    fallbacks = [r for r in rows if r["fallback_used"] == 1]
    low_conf = [r for r in rows if r["confidence_warning"] == "LOW_CONFIDENCE"]
    avg_lat = sum(float(r["latency_ms"]) for r in rows) / total

    def _top_unique(items: list[Any], limit: int = 10) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for r in items:
            q = str(r["query_text"])
            if q not in seen:
                seen.add(q)
                out.append(q)
            if len(out) >= limit:
                break
        return out

    return {
        "total_queries": total,
        "zero_hit_count": len(zero_hits),
        "zero_hit_rate": round(len(zero_hits) / total, 4),
        "fallback_count": len(fallbacks),
        "fallback_rate": round(len(fallbacks) / total, 4),
        "low_confidence_count": len(low_conf),
        "avg_latency_ms": round(avg_lat, 2),
        "zero_hit_queries": _top_unique(zero_hits),
        "fallback_queries": _top_unique(fallbacks),
        "low_confidence_queries": _top_unique(low_conf),
    }


def get_evolution_status(conn: sqlite3.Connection, config: KBConfig) -> dict[str, Any]:
    """Compute current adaptive evolution state, backoff interval, and wake-up signal."""
    compiled_total = int(
        conn.execute("SELECT COUNT(*) FROM raw_documents WHERE state = 'COMPILED'").fetchone()[0]
    )
    last_row = conn.execute(
        "SELECT * FROM evolution_checkpoints ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if last_row:
        last_cp: dict[str, Any] | None = {
            "id": last_row["id"],
            "compiled_raw_total": last_row["compiled_raw_total"],
            "wiki_pages_total": last_row["wiki_pages_total"],
            "outcome": last_row["outcome"],
            "next_interval": last_row["next_interval"],
            "summary": last_row["summary"],
            "metrics": json.loads(last_row["metrics_json"] or "{}"),
            "created_at": last_row["created_at"],
        }
        baseline_compiled = int(last_row["compiled_raw_total"])
        current_interval = max(1, int(last_row["next_interval"]))
        state = "ceiling_paused" if last_row["outcome"] == "model_ceiling" else "active"
        since_iso: str | None = last_row["created_at"]
        since_id: int | None = (
            int(last_cp["metrics"]["last_query_log_id"])
            if isinstance(last_cp.get("metrics"), dict) and "last_query_log_id" in last_cp["metrics"]
            else None
        )
    else:
        last_cp = None
        baseline_compiled = 0
        current_interval = max(1, config.evolution.initial_interval)
        state = "active"
        since_iso = None
        since_id = None

    since_last = max(0, compiled_total - baseline_compiled)
    q_stats = _query_window_stats(conn, since_iso, since_id=since_id)

    wake_reason: str | None = None
    evolve_recommended = False
    if config.evolution.enabled:
        if since_last >= current_interval:
            evolve_recommended = True
            wake_reason = "volume_milestone"
        elif (
            q_stats["total_queries"] >= config.evolution.min_pain_queries
            and q_stats["zero_hit_rate"] >= config.evolution.pain_zero_hit_ratio
        ):
            evolve_recommended = True
            wake_reason = "pain_signal_spike"

    return {
        "enabled": config.evolution.enabled,
        "state": state,
        "compiled_raw_total": compiled_total,
        "since_last_evolve": since_last,
        "current_interval": current_interval,
        "evolve_recommended": evolve_recommended,
        "wake_reason": wake_reason,
        "last_checkpoint": last_cp,
    }


def _collect_page_diagnostics(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT rel_path, type, title, status, char_count,
               aliases_json, layers_json, mechanisms_json,
               experience_type, reading_status, updated_at
        FROM wiki_pages
        WHERE status != 'deprecated'
        ORDER BY updated_at DESC, rel_path ASC
        """
    ).fetchall()
    total = len(rows)
    if total == 0:
        return {
            "active_pages": 0,
            "avg_char_count": 0,
            "aliases_coverage": 0.0,
            "mechanisms_coverage": 0.0,
            "layers_coverage": 0.0,
            "experience_type_coverage": 0.0,
            "recent_pages": [],
        }

    def _non_empty_json_list(raw: Any) -> bool:
        try:
            v = json.loads(raw or "[]")
            return isinstance(v, list) and len(v) > 0
        except (ValueError, TypeError):
            return False

    avg_chars = round(sum(int(r["char_count"]) for r in rows) / total)
    has_aliases = sum(1 for r in rows if _non_empty_json_list(r["aliases_json"]))
    has_mech = sum(1 for r in rows if _non_empty_json_list(r["mechanisms_json"]))
    has_layers = sum(1 for r in rows if _non_empty_json_list(r["layers_json"]))
    has_exp = sum(1 for r in rows if bool((r["experience_type"] or "").strip()))

    recent_pages = [
        {
            "rel_path": r["rel_path"],
            "type": r["type"],
            "title": r["title"],
            "char_count": r["char_count"],
            "updated_at": r["updated_at"],
        }
        for r in rows[:15]
    ]
    return {
        "active_pages": total,
        "avg_char_count": avg_chars,
        "aliases_coverage": round(has_aliases / total, 4),
        "mechanisms_coverage": round(has_mech / total, 4),
        "layers_coverage": round(has_layers / total, 4),
        "experience_type_coverage": round(has_exp / total, 4),
        "recent_pages": recent_pages,
    }


def build_evolve_report(root_dir: Path, config: KBConfig) -> dict[str, Any]:
    """Generate objective evidence-gated diagnostics for self-evolution & ceiling detection."""
    lint_res = lint_wiki(root_dir, config)
    conn = connect_db(root_dir, ensure_schema=True)
    try:
        evo_status = get_evolution_status(conn, config)
        last_cp = evo_status["last_checkpoint"]
        since_iso = last_cp["created_at"] if last_cp else None
        since_id = (
            int(last_cp["metrics"]["last_query_log_id"])
            if last_cp and isinstance(last_cp.get("metrics"), dict) and "last_query_log_id" in last_cp["metrics"]
            else None
        )

        q_stats = _query_window_stats(conn, since_iso, since_id=since_id)
        p_stats = _collect_page_diagnostics(conn)
        max_q_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM query_logs").fetchone()[0])
        orphan_count = sum(1 for w in lint_res["warnings"] if w.get("code") == "ORPHAN_PAGE")
        broken_links = int(
            conn.execute("SELECT COUNT(*) FROM wiki_links WHERE is_broken = 1").fetchone()[0]
        )
        candidate_relations = int(
            conn.execute(
                "SELECT COUNT(*) FROM wiki_links WHERE relation_status = 'candidate' AND is_broken = 0"
            ).fetchone()[0]
        )
    finally:
        conn.close()

    current_metrics = {
        "active_pages": p_stats["active_pages"],
        "orphan_count": orphan_count,
        "broken_links": broken_links,
        "candidate_relations": candidate_relations,
        "zero_hit_rate": q_stats["zero_hit_rate"],
        "fallback_rate": q_stats["fallback_rate"],
        "aliases_coverage": p_stats["aliases_coverage"],
        "mechanisms_coverage": p_stats["mechanisms_coverage"],
        "last_query_log_id": max_q_id,
    }

    delta: dict[str, Any] | None = None
    if last_cp and isinstance(last_cp.get("metrics"), dict):
        prev = last_cp["metrics"]
        delta = {
            "raw_added": evo_status["since_last_evolve"],
            "pages_added": p_stats["active_pages"] - int(prev.get("active_pages", 0)),
            "zero_hit_rate_delta": round(
                q_stats["zero_hit_rate"] - float(prev.get("zero_hit_rate", 0.0)), 4
            ),
            "fallback_rate_delta": round(
                q_stats["fallback_rate"] - float(prev.get("fallback_rate", 0.0)), 4
            ),
            "orphan_delta": orphan_count - int(prev.get("orphan_count", 0)),
        }

    return {
        "ok": True,
        "evolution_status": evo_status,
        "ingest_diagnostics": p_stats,
        "query_diagnostics": q_stats,
        "health_snapshot": {
            "lint_ok": lint_res["ok"],
            "error_count": lint_res["error_count"],
            "warning_count": lint_res["warning_count"],
            "orphan_count": orphan_count,
            "broken_links": broken_links,
            "candidate_relations": candidate_relations,
        },
        "current_metrics": current_metrics,
        "delta_vs_last_checkpoint": delta,
    }


def record_evolve_checkpoint(
    root_dir: Path,
    config: KBConfig,
    *,
    outcome: str,
    summary: str,
    next_interval: int | None = None,
) -> dict[str, Any]:
    """Record an evolution decision checkpoint and compute adaptive next backoff interval."""
    norm_outcome = outcome.strip().lower()
    if norm_outcome not in VALID_EVOLVE_OUTCOMES:
        raise KBError(
            ErrorCode.INVALID_ARGUMENT,
            f"无效的 outcome: '{outcome}'（允许值: {sorted(VALID_EVOLVE_OUTCOMES)}）",
        )

    report = build_evolve_report(root_dir, config)
    evo_status = report["evolution_status"]
    computed_next = _compute_next_interval(
        evo_status["current_interval"],
        norm_outcome,
        config.evolution.backoff_intervals,
        explicit_next=next_interval,
    )
    now = utc_now_iso()

    with write_lock(root_dir, config.safety.write_lock_timeout_seconds):
        with db_session(root_dir) as conn:
            cur = conn.execute(
                """
                INSERT INTO evolution_checkpoints (
                    compiled_raw_total, wiki_pages_total, outcome,
                    next_interval, summary, metrics_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evo_status["compiled_raw_total"],
                    report["ingest_diagnostics"]["active_pages"],
                    norm_outcome,
                    computed_next,
                    summary.strip(),
                    json.dumps(report["current_metrics"], ensure_ascii=False),
                    now,
                ),
            )
            cp_id = cur.lastrowid

    return {
        "ok": True,
        "checkpoint_id": cp_id,
        "outcome": norm_outcome,
        "state": "ceiling_paused" if norm_outcome == "model_ceiling" else "active",
        "compiled_raw_total": evo_status["compiled_raw_total"],
        "next_interval": computed_next,
        "summary": summary.strip(),
        "created_at": now,
    }
