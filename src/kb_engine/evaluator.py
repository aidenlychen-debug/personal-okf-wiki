"""Golden test suite evaluator for recall, precision, negative queries & warm-cache latency."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import jieba
import yaml

from kb_engine.config import KBConfig
from kb_engine.errors import ErrorCode, KBError
from kb_engine.retriever import query_wiki
from kb_engine.utils import normalize_rel_path

jieba.setLogLevel(logging.ERROR)


def run_eval(
    root_dir: Path,
    config: KBConfig,
    cases_path: Path,
) -> dict[str, Any]:
    """Run lock-free evaluation against a YAML golden test cases file."""
    if not cases_path.exists():
        raise KBError(
            ErrorCode.FILE_NOT_FOUND,
            f"评测用例文件不存在: {cases_path}",
            details={"cases_path": str(cases_path)},
        )

    raw = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    cases = raw.get("cases", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    if not isinstance(cases, list):
        raise KBError(ErrorCode.INVALID_ARGUMENT, "评测用例文件格式无效，应包含 cases 列表。")

    # Pre-warm jieba dictionary before measuring hot-cache pure query latency
    jieba.initialize()

    positive_total_expected = 0
    positive_total_matched = 0
    precision_scores: list[float] = []
    negative_total = 0
    negative_false_positives = 0
    latencies: list[float] = []
    case_results: list[dict[str, Any]] = []

    for idx, case in enumerate(cases):
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id") or f"case_{idx + 1}")
        query = str(case.get("query") or "")
        is_negative = bool(case.get("negative", False))
        expected = [normalize_rel_path(p) for p in (case.get("expected_paths") or [])]
        include_neighbors = bool(case.get("include_neighbors", True))

        res = query_wiki(root_dir, config, query)
        latency_ms = float(res["latency_ms"])
        latencies.append(latency_ms)

        direct_paths = [item["rel_path"] for item in res["l1_inventory"]]
        neighbor_paths = [item["rel_path"] for item in res["graph_neighbors"]]
        candidate_paths = set(direct_paths + (neighbor_paths if include_neighbors else []))

        if is_negative:
            negative_total += 1
            fp = len(direct_paths) > 0
            if fp:
                negative_false_positives += 1
            case_results.append(
                {
                    "id": case_id,
                    "query": query,
                    "negative": True,
                    "passed": not fp,
                    "returned_count": len(direct_paths),
                    "latency_ms": latency_ms,
                }
            )
        else:
            expected_set = set(expected)
            matched = expected_set & candidate_paths
            positive_total_expected += len(expected_set)
            positive_total_matched += len(matched)

            case_recall = len(matched) / len(expected_set) if expected_set else 1.0
            if candidate_paths:
                case_precision = len(matched) / len(candidate_paths)
            else:
                case_precision = 0.0 if expected_set else 1.0
            precision_scores.append(case_precision)

            case_results.append(
                {
                    "id": case_id,
                    "query": query,
                    "negative": False,
                    "passed": len(matched) == len(expected_set),
                    "recall": round(case_recall, 4),
                    "precision": round(case_precision, 4),
                    "expected_paths": sorted(expected_set),
                    "matched_paths": sorted(matched),
                    "missing_paths": sorted(expected_set - matched),
                    "latency_ms": latency_ms,
                }
            )

    overall_recall = (
        positive_total_matched / positive_total_expected if positive_total_expected > 0 else 1.0
    )
    overall_precision = (
        sum(precision_scores) / len(precision_scores) if precision_scores else 1.0
    )
    neg_fp_rate = (
        negative_false_positives / negative_total if negative_total > 0 else 0.0
    )
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    max_latency = max(latencies) if latencies else 0.0

    return {
        "ok": True,
        "cases_path": str(cases_path),
        "total_cases": len(case_results),
        "metrics": {
            "recall": round(overall_recall, 4),
            "precision": round(overall_precision, 4),
            "negative_cases": negative_total,
            "negative_false_positives": negative_false_positives,
            "negative_false_positive_rate": round(neg_fp_rate, 4),
            "avg_latency_ms": round(avg_latency, 2),
            "max_latency_ms": round(max_latency, 2),
        },
        "results": case_results,
    }
