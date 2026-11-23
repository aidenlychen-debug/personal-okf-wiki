"""Hardening tests: cross-depth rename, real multi-process lock stress, 300-page eval & live watchdog."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from kb_engine.cli import cmd_init
from kb_engine.config import load_config
from kb_engine.db import connect_db
from kb_engine.errors import ErrorCode
from kb_engine.evaluator import run_eval
from kb_engine.indexer import reindex_wiki
from kb_engine.refactor import rename_wiki_page
from kb_engine.utils import write_lock
from kb_engine.validator import lint_wiki
from kb_engine.watcher import run_watcher
from tests.test_retrieval_safety_refactor import _seed_golden_wiki


def test_cross_depth_rename_rewrites_incoming_outgoing_and_sources_ref(tmp_path: Path) -> None:
    _seed_golden_wiki(tmp_path)
    config = load_config(tmp_path)

    # Move concepts/知识复利效应.md -> concepts/sub/知识复利飞轮.md (depth increases by 1!)
    res = rename_wiki_page(
        tmp_path,
        config,
        "concepts/知识复利效应.md",
        "concepts/sub/知识复利飞轮.md",
        new_title="知识复利飞轮",
    )
    assert res["ok"] is True
    assert res["rewritten_links_count"] == 3
    assert res["outgoing_links_rewritten_count"] >= 4  # 3 wiki links + 1 YAML sources[].ref

    moved_text = (tmp_path / "wiki" / "concepts" / "sub" / "知识复利飞轮.md").read_text(encoding="utf-8")
    # Outgoing YAML source ref adjusted from ../../raw/... to ../../../raw/...
    assert "../../../raw/2026-11-21-llm-wiki-architecture.md" in moved_text
    # Outgoing wiki links adjusted from ../cases/... to ../../cases/...
    assert "../../cases/某团队落地案例.md" in moved_text
    assert "../../evidences/检索效率对比数据.md" in moved_text
    assert "../../sources/LLM-Wiki-架构解析.md" in moved_text
    # External link & internal anchor untouched
    assert "https://example.com/okf" in moved_text
    assert "(#关联知识)" in moved_text

    # Full vault lint must report 0 errors and 0 broken links
    lint_res = lint_wiki(tmp_path, config)
    assert lint_res["ok"] is True
    assert lint_res["error_count"] == 0


def test_multiprocess_write_lock_contention_and_timeout(tmp_path: Path) -> None:
    cmd_init(tmp_path)
    for i in range(6):
        (tmp_path / "inbox" / f"mp_note_{i}.md").write_text(f"# 多进程测试 {i}\n正文 {i}\n", encoding="utf-8")

    # Launch 6 concurrent OS processes competing for .kb/write.lock
    cmds = [
        [sys.executable, "-m", "kb_engine.cli", "--root", str(tmp_path), "scan", "--json"],
        [sys.executable, "-m", "kb_engine.cli", "--root", str(tmp_path), "reindex", "--json"],
        [sys.executable, "-m", "kb_engine.cli", "--root", str(tmp_path), "add", "--text", "跨进程灵感A", "--title", "灵感A", "--json"],
        [sys.executable, "-m", "kb_engine.cli", "--root", str(tmp_path), "scan", "--json"],
        [sys.executable, "-m", "kb_engine.cli", "--root", str(tmp_path), "add", "--text", "跨进程灵感B", "--title", "灵感B", "--json"],
        [sys.executable, "-m", "kb_engine.cli", "--root", str(tmp_path), "reindex", "--json"],
    ]
    procs = [
        subprocess.Popen(c, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        for c in cmds
    ]
    for p in procs:
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, f"Process failed: stdout={out} stderr={err}"
        payload = json.loads(out)
        assert payload["ok"] is True

    conn = connect_db(tmp_path)
    try:
        # 6 inbox files + 2 direct_text adds = 8 raw_documents
        assert conn.execute("SELECT COUNT(*) FROM raw_documents").fetchone()[0] == 8
    finally:
        conn.close()

    # Verify WRITE_LOCK_TIMEOUT error contract from a separate OS process
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8").replace("write_lock_timeout_seconds: 10", "write_lock_timeout_seconds: 0.3"),
        encoding="utf-8",
    )
    with write_lock(tmp_path, timeout_seconds=5.0):
        timeout_proc = subprocess.run(
            [sys.executable, "-m", "kb_engine.cli", "--root", str(tmp_path), "scan", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert timeout_proc.returncode != 0
        err_json = json.loads(timeout_proc.stdout)
        assert err_json["ok"] is False
        assert err_json["error_code"] == ErrorCode.WRITE_LOCK_TIMEOUT.value


def test_300_page_scale_eval_latency_under_200ms(tmp_path: Path) -> None:
    _seed_golden_wiki(tmp_path)
    config = load_config(tmp_path)

    # Generate 300 realistic filler pages across all 5 OKF types
    type_dirs = [
        ("concept", "concepts"),
        ("case", "cases"),
        ("evidence", "evidences"),
        ("insight", "insights"),
        ("source", "sources"),
    ]
    for i in range(300):
        ptype, subdir = type_dirs[i % len(type_dirs)]
        p = tmp_path / "wiki" / subdir / f"规模压测条目_{i:03d}.md"
        p.write_text(
            "---\n"
            f"type: {ptype}\n"
            f'title: "规模压测条目 {i:03d}"\n'
            f'summary: "用于验证300页知识库规模下的检索性能与精度基线 {i:03d}。"\n'
            "status: stable\n"
            f"tags: [压测, 专题{i % 15}]\n"
            "---\n\n"
            f"# 规模压测条目 {i:03d}\n\n"
            f"这是一篇编号为 {i:03d} 的常规知识笔记，讨论分布式系统、内容运营、视频脚本结构与认知升级等主题。\n",
            encoding="utf-8",
        )

    idx_stats = reindex_wiki(tmp_path, config, regenerate_index=True)
    assert idx_stats["pages_indexed"] == 304

    cases_file = Path(__file__).parent / "eval_cases.yaml"
    eval_res = run_eval(tmp_path, config, cases_file)
    assert eval_res["ok"] is True
    assert eval_res["metrics"]["recall"] == 1.0
    assert eval_res["metrics"]["negative_false_positives"] == 0
    assert eval_res["metrics"]["max_latency_ms"] < 200.0


def test_live_watchdog_debounce_and_junk_filter(tmp_path: Path) -> None:
    cmd_init(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    cfg_text = (
        cfg_path.read_text(encoding="utf-8")
        .replace("debounce_seconds: 1.5", "debounce_seconds: 0.2")
        .replace("poll_interval_seconds: 30", "poll_interval_seconds: 5")
    )
    cfg_path.write_text(cfg_text, encoding="utf-8")
    config = load_config(tmp_path)

    stop_event = threading.Event()
    t = threading.Thread(target=run_watcher, args=(tmp_path, config), kwargs={"stop_event": stop_event}, daemon=True)
    t.start()
    try:
        # Wait briefly for observer to attach
        time.sleep(0.4)

        # Drop a junk .tmp file and a real markdown file
        junk = tmp_path / "inbox" / "downloading.tmp"
        junk.write_text("partial", encoding="utf-8")
        real_note = tmp_path / "inbox" / "实时投递笔记.md"
        real_note.write_text("# 实时投递笔记\n验证 watchdog 自动防抖归档。\n", encoding="utf-8")

        # Wait up to 4s for watcher to ingest real_note
        deadline = time.monotonic() + 4.0
        ingested = False
        while time.monotonic() < deadline:
            if not real_note.exists():
                conn = connect_db(tmp_path)
                try:
                    cnt = conn.execute("SELECT COUNT(*) FROM raw_documents").fetchone()[0]
                finally:
                    conn.close()
                if cnt == 1:
                    ingested = True
                    break
            time.sleep(0.15)

        assert ingested is True
        assert junk.exists()  # Junk file must be ignored, not archived
    finally:
        stop_event.set()
        t.join(timeout=5.0)


def test_human_readable_cli_output(tmp_path: Path) -> None:
    _seed_golden_wiki(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "kb_engine.cli", "--root", str(tmp_path), "status"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0
    assert "Wiki 页面:" in proc.stdout
    assert "守护进程:" in proc.stdout

    proc_q = subprocess.run(
        [sys.executable, "-m", "kb_engine.cli", "--root", str(tmp_path), "query", "复利"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc_q.returncode == 0
    assert "知识复利效应" in proc_q.stdout


def test_and_priority_blocks_weak_subtoken_noise_and_slices_sections(tmp_path: Path) -> None:
    from kb_engine.retriever import query_wiki

    cmd_init(tmp_path)
    config = load_config(tmp_path)

    # 3 pages genuinely about "无限游戏"
    for idx in range(1, 4):
        (tmp_path / "wiki" / "concepts" / f"无限游戏概念{idx}.md").write_text(
            "---\n"
            "type: concept\n"
            f'title: "无限游戏概念{idx}"\n'
            f'summary: "讨论无限游戏的长期边界与持续规则 {idx}。"\n'
            "status: stable\n"
            "tags: [无限游戏]\n"
            "---\n\n"
            f"# 无限游戏概念{idx}\n\n"
            "## 核心要义\n"
            "无限游戏的目标不是赢下某次比赛，而是让游戏持续进行下去。\n\n"
            "## 关联知识\n"
            "- 延伸阅读：无限游戏概念参考\n",
            encoding="utf-8",
        )

    # 1 unrelated physics page mentioning only "无限" ("无限可分")
    (tmp_path / "wiki" / "concepts" / "时间量子化.md").write_text(
        "---\n"
        "type: concept\n"
        'title: "时间量子化"\n'
        'summary: "普朗克尺度下时间不是无限可分的。"\n'
        "status: stable\n"
        "tags: [物理学]\n"
        "---\n\n"
        "# 时间量子化\n\n"
        "## 物理推导\n"
        "空间与时间在普朗克尺度下并非无限可分，存在最小离散单元。\n",
        encoding="utf-8",
    )

    reindex_wiki(tmp_path, config, regenerate_index=True)

    res = query_wiki(tmp_path, config, "无限游戏")
    paths = [x["rel_path"] for x in res["l1_inventory"]]
    assert res["stats"]["fallback_used"] is False
    assert res["stats"]["direct_hits"] == 3
    assert "concepts/时间量子化.md" not in paths
    assert all(x["match_tier"] == "exact+strict" for x in res["l1_inventory"])

    # Verify passage-level matched_sections prioritizes substantive section over 关联知识
    top_sections = res["l2_full_docs"][0]["matched_sections"]
    assert len(top_sections) >= 1
    assert top_sections[0]["heading"] == "核心要义"


def test_or_fallback_triggers_on_sparse_multi_term_query(tmp_path: Path) -> None:
    from kb_engine.retriever import query_wiki

    cmd_init(tmp_path)
    config = load_config(tmp_path)

    (tmp_path / "wiki" / "concepts" / "无许可杠杆.md").write_text(
        "---\n"
        "type: concept\n"
        'title: "无许可杠杆"\n'
        'summary: "代码与媒体是零边际成本的杠杆。"\n'
        "status: stable\n"
        "tags: [杠杆]\n"
        "---\n\n"
        "# 无许可杠杆\n\n"
        "## 核心机制\n"
        "媒体杠杆让创作者一次生产无限复制。\n",
        encoding="utf-8",
    )
    reindex_wiki(tmp_path, config, regenerate_index=True)

    # Query has 2 terms; no single page has both "杠杆" and "太空殖民", so OR fallback rescues "无许可杠杆"
    res = query_wiki(tmp_path, config, "杠杆 太空殖民")
    assert res["stats"]["fallback_used"] is True
    assert res["stats"]["direct_hits"] == 1
    assert res["l1_inventory"][0]["rel_path"] == "concepts/无许可杠杆.md"
    assert res["l1_inventory"][0]["match_tier"] == "fallback_or"

