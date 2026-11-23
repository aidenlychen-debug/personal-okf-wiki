"""Tests for adaptive, evidence-gated self-evolution (telemetry, ceiling pause, backoff & volume/pain wake-up)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from kb_engine.cli import cmd_init, cmd_status
from kb_engine.config import load_config
from kb_engine.db import db_session, upsert_raw_document
from kb_engine.evaluator import run_eval
from kb_engine.evolution import build_evolve_report, record_evolve_checkpoint
from kb_engine.indexer import reindex_wiki
from kb_engine.retriever import query_wiki


def _insert_compiled_raws(tmp_path: Path, count: int, start_idx: int = 1) -> None:
    with db_session(tmp_path) as conn:
        for i in range(start_idx, start_idx + count):
            upsert_raw_document(
                conn,
                content_hash=f"hash_{i:04d}",
                source_origin="inbox",
                origin_path=f"inbox/doc_{i}.md",
                origin_mtime=1000.0 + i,
                raw_rel_path=f"raw/2026-11-22-doc-{i}.md",
                asset_rel_path=None,
                media_type="text",
                needs_vision=False,
                state="COMPILED",
            )


def test_query_telemetry_records_real_queries_but_not_eval(tmp_path: Path) -> None:
    cmd_init(tmp_path)
    config = load_config(tmp_path)

    (tmp_path / "wiki" / "concepts" / "复利效应.md").write_text(
        "---\n"
        "type: concept\n"
        'title: "复利效应"\n'
        'summary: "长期指数级积累效应。"\n'
        "status: stable\n"
        "---\n\n"
        "# 复利效应\n\n"
        "复利是世界第八大奇迹。\n",
        encoding="utf-8",
    )
    reindex_wiki(tmp_path, config, regenerate_index=True)

    # Run eval first: must NOT write to query_logs
    cases_file = tmp_path / "cases.yaml"
    cases_file.write_text(
        "cases:\n"
        "  - id: c1\n"
        '    query: "复利效应"\n'
        "    expected_paths: [concepts/复利效应.md]\n",
        encoding="utf-8",
    )
    run_eval(tmp_path, config, cases_file)
    report0 = build_evolve_report(tmp_path, config)
    assert report0["query_diagnostics"]["total_queries"] == 0

    # Real user queries: 1 hit + 1 zero-hit
    query_wiki(tmp_path, config, "复利效应")
    query_wiki(tmp_path, config, "完全不存在的冷门术语xyz")

    report1 = build_evolve_report(tmp_path, config)
    qd = report1["query_diagnostics"]
    assert qd["total_queries"] == 2
    assert qd["zero_hit_count"] == 1
    assert "完全不存在的冷门术语xyz" in qd["zero_hit_queries"]


def test_adaptive_backoff_model_ceiling_pause_and_volume_wakeup(tmp_path: Path) -> None:
    cmd_init(tmp_path)
    config = load_config(tmp_path)

    # 1. Below initial_interval (5): not recommended yet
    _insert_compiled_raws(tmp_path, 4, start_idx=1)
    st = cmd_status(tmp_path, config)
    assert st["evolution"]["evolve_recommended"] is False
    assert st["evolution"]["since_last_evolve"] == 4
    assert st["evolution"]["current_interval"] == 5

    # 2. Reach 5 compiled docs -> triggers volume_milestone
    _insert_compiled_raws(tmp_path, 1, start_idx=5)
    rep = build_evolve_report(tmp_path, config)
    assert rep["evolution_status"]["evolve_recommended"] is True
    assert rep["evolution_status"]["wake_reason"] == "volume_milestone"

    # 3. Record genuine gain ('evolved') -> stays responsive at interval 5
    cp1 = record_evolve_checkpoint(tmp_path, config, outcome="evolved", summary="优化了别名与机制词提取规则")
    assert cp1["state"] == "active"
    assert cp1["next_interval"] == 5

    # 4. Next 5 docs (total 10) -> triggers again; this time hits model_ceiling -> enters ceiling_paused & backs off to 10
    _insert_compiled_raws(tmp_path, 5, start_idx=6)
    rep2 = build_evolve_report(tmp_path, config)
    assert rep2["evolution_status"]["evolve_recommended"] is True
    assert rep2["delta_vs_last_checkpoint"]["raw_added"] == 5

    cp2 = record_evolve_checkpoint(
        tmp_path, config, outcome="model_ceiling", summary="已触及当前词法/模型能力上限，暂停迭代"
    )
    assert cp2["state"] == "ceiling_paused"
    assert cp2["next_interval"] == 10

    # 5. Add 5 more docs (total 15): still paused because backoff interval is now 10!
    _insert_compiled_raws(tmp_path, 5, start_idx=11)
    rep3 = build_evolve_report(tmp_path, config)
    assert rep3["evolution_status"]["state"] == "ceiling_paused"
    assert rep3["evolution_status"]["evolve_recommended"] is False

    # 6. Add 5 more docs (total 20, since_last == 10): wakes up cleanly on volume_milestone!
    _insert_compiled_raws(tmp_path, 5, start_idx=16)
    rep4 = build_evolve_report(tmp_path, config)
    assert rep4["evolution_status"]["evolve_recommended"] is True
    assert rep4["evolution_status"]["wake_reason"] == "volume_milestone"

    # 7. If still no_gain -> backs off further to 20
    cp3 = record_evolve_checkpoint(tmp_path, config, outcome="no_gain", summary="数据量增至20篇仍无显著新痛点")
    assert cp3["next_interval"] == 20


def test_pain_signal_spike_wakes_up_paused_evolution_and_cli_works(tmp_path: Path) -> None:
    cmd_init(tmp_path)
    config = load_config(tmp_path)

    # Put evolution into ceiling_paused with interval 10
    record_evolve_checkpoint(tmp_path, config, outcome="model_ceiling", summary="暂达能力上限")
    rep_quiet = build_evolve_report(tmp_path, config)
    assert rep_quiet["evolution_status"]["state"] == "ceiling_paused"
    assert rep_quiet["evolution_status"]["evolve_recommended"] is False

    # Simulate 5 real queries with 4 zero-hits (80% >= 30% threshold) -> wakes up on pain_signal_spike
    for idx in range(5):
        query_wiki(tmp_path, config, f"未覆盖主题词_{idx}")

    rep_pain = build_evolve_report(tmp_path, config)
    assert rep_pain["evolution_status"]["evolve_recommended"] is True
    assert rep_pain["evolution_status"]["wake_reason"] == "pain_signal_spike"

    # Verify CLI evolve-report & evolve-mark --json
    p1 = subprocess.run(
        [sys.executable, "-m", "kb_engine.cli", "--root", str(tmp_path), "evolve-report", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert p1.returncode == 0
    assert json.loads(p1.stdout)["ok"] is True

    p2 = subprocess.run(
        [
            sys.executable,
            "-m",
            "kb_engine.cli",
            "--root",
            str(tmp_path),
            "evolve-mark",
            "--outcome",
            "evolved",
            "--summary",
            "针对未覆盖查询补充口语别名规范",
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert p2.returncode == 0
    marked = json.loads(p2.stdout)
    assert marked["ok"] is True
    assert marked["outcome"] == "evolved"
