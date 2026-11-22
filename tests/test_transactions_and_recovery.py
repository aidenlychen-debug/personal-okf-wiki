"""Gates 2, 3, 4 tests: Cross-system reconciliation, zero-loss recovery, poison pill, rebuild & EN->ZH+Image."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kb_engine.cli import (
    cmd_compile_begin,
    cmd_compile_commit,
    cmd_compile_fail,
    cmd_init,
    cmd_recover,
    cmd_retry,
)
from kb_engine.config import load_config
from kb_engine.db import connect_db, db_session
from kb_engine.indexer import rebuild_database
from kb_engine.utils import git_show_file, run_git
from kb_engine.watcher import scan_once
from tests.test_utils_and_watcher import make_test_png


def _write_valid_concept(root: Path, filename: str, title: str, raw_rel: str, extra_body: str = "") -> Path:
    p = root / "wiki" / "concepts" / filename
    content = (
        "---\n"
        "type: concept\n"
        f'title: "{title}"\n'
        'summary: "持续预编译与互联让知识越用越厚。"\n'
        "status: stable\n"
        "tags: [知识管理, 内容创作]\n"
        'aliases: ["Knowledge Compounding"]\n'
        'domain: "personal-growth"\n'
        "visibility: private\n"
        "generated:\n"
        '  by: "agent:kb-ingest"\n'
        '  at: "2026-11-21"\n'
        "verified: []\n"
        "sources:\n"
        "  - id: s1\n"
        f'    ref: "../../{raw_rel}"\n'
        "---\n\n"
        f"# {title}\n\n"
        "## 核心要义\n"
        f"这是一段关于{title}的中文精炼提炼，包含足够的上下文内容以便测试校验。{extra_body}\n\n"
        "<!-- human:start -->\n"
        "人类原始批注区。\n"
        "<!-- human:end -->\n"
    )
    p.write_text(content, encoding="utf-8")
    return p


def test_gate2_human_checkpoint_exact_staging_reconcile_and_zero_loss_recover(tmp_path: Path) -> None:
    cmd_init(tmp_path)
    config = load_config(tmp_path)

    # Scenario 1: Human leaves uncommitted wiki/ edits AND raw/ has new archived file
    human_page = tmp_path / "wiki" / "insights" / "人类随笔.md"
    human_page.write_text(
        "---\ntype: insight\ntitle: \"人类随笔\"\nsummary: \"人类手动写的灵感\"\n---\n\n# 人类随笔\n手动内容。\n",
        encoding="utf-8",
    )
    (tmp_path / "inbox" / "doc1.md").write_text("# Doc 1\nFirst raw document.\n", encoding="utf-8")
    scan_once(tmp_path, config)

    begin = cmd_compile_begin(tmp_path, config)
    assert begin["human_checkpoint_created"] is True
    assert len(begin["items"]) == 1

    # Verify the human checkpoint commit ONLY included wiki/, NOT raw/
    show_files = run_git(tmp_path, ["show", "--name-only", "--pretty=format:", "HEAD"]).stdout.splitlines()
    assert any(f.startswith("wiki/") for f in show_files)
    assert not any(f.startswith("raw/") for f in show_files)

    # Scenario 2: While compiling, a concurrent new file arrives in raw/
    (tmp_path / "inbox" / "concurrent_doc2.md").write_text("# Doc 2\nConcurrent arrival.\n", encoding="utf-8")
    scan_once(tmp_path, config)

    raw1_rel = begin["items"][0]["raw_rel_path"]
    _write_valid_concept(tmp_path, "知识复利效应.md", "知识复利效应", raw1_rel)
    commit_res = cmd_compile_commit(tmp_path, config, session_id=begin["session_id"], note="编译第一篇资料")
    assert commit_res["ok"] is True
    assert commit_res["commit_sha"] is not None

    committed_files = run_git(tmp_path, ["show", "--name-only", "--pretty=format:", commit_res["commit_sha"]]).stdout.splitlines()
    assert raw1_rel in committed_files
    # Concurrent doc2 in raw/ must NOT be staged in doc1's commit!
    assert not any("concurrent_doc2" in f for f in committed_files)

    # Scenario 3: Simulate crash in COMMITTING state AFTER Git commit already landed
    begin2 = cmd_compile_begin(tmp_path, config)
    sess2 = begin2["session_id"]
    raw2_rel = begin2["items"][0]["raw_rel_path"]
    _write_valid_concept(tmp_path, "并发概念.md", "并发概念", raw2_rel)

    # Manually simulate Step A (COMMITTING) + Step B (Git commit with trailer), then crash before Step C
    with db_session(tmp_path) as conn:
        conn.execute("UPDATE compile_sessions SET status = 'COMMITTING' WHERE session_id = ?", (sess2,))
    run_git(tmp_path, ["add", "-A", "--", "wiki"])
    run_git(tmp_path, ["add", "--", raw2_rel])
    run_git(tmp_path, ["commit", "-m", f"feat(wiki): simulated crash commit\n\n[kb-session:{sess2}]"])
    expected_sha = run_git(tmp_path, ["rev-parse", "HEAD"]).stdout.strip()

    rec_res = cmd_recover(tmp_path, config)
    assert rec_res["action"] == "reconciled"
    assert rec_res["reconciled_commit_sha"] == expected_sha
    assert rec_res["recovery_ref"] is None

    conn = connect_db(tmp_path)
    try:
        st = conn.execute("SELECT status FROM compile_sessions WHERE session_id = ?", (sess2,)).fetchone()[0]
        doc_st = conn.execute("SELECT state FROM raw_documents WHERE raw_rel_path = ?", (raw2_rel,)).fetchone()[0]
        assert st == "COMMITTED"
        assert doc_st == "COMPILED"
    finally:
        conn.close()

    # Scenario 4: Dirty wiki edit during ACTIVE session -> kb recover saves to refs/kb/recovery/<id> zero loss
    (tmp_path / "inbox" / "doc3.md").write_text("# Doc 3\nThird doc.\n", encoding="utf-8")
    scan_once(tmp_path, config)
    begin3 = cmd_compile_begin(tmp_path, config)
    sess3 = begin3["session_id"]

    # Human manually edits a wiki file while session is ACTIVE
    dirty_target = tmp_path / "wiki" / "concepts" / "知识复利效应.md"
    dirty_target.write_text(dirty_target.read_text(encoding="utf-8") + "\n人类在编译中途手写的新内容！\n", encoding="utf-8")

    rec3 = cmd_recover(tmp_path, config)
    assert rec3["action"] == "rolled_back"
    assert rec3["recovery_ref"] == f"refs/kb/recovery/{sess3}"
    assert "wiki/concepts/知识复利效应.md" in rec3["backed_up_files"]
    assert rec3["reset_to_pending_count"] == 1

    # Verify the backup ref contains the exact dirty edit (zero byte loss)
    backed_up_content = git_show_file(tmp_path, rec3["recovery_ref"], "wiki/concepts/知识复利效应.md")
    assert backed_up_content is not None
    assert "人类在编译中途手写的新内容！" in backed_up_content


def test_gate2_poison_pill_and_retry_loop(tmp_path: Path) -> None:
    cmd_init(tmp_path)
    config = load_config(tmp_path)

    (tmp_path / "inbox" / "good.md").write_text("# Good\nValid content.\n", encoding="utf-8")
    (tmp_path / "inbox" / "poison.md").write_text("# Poison\nCorrupted payload.\n", encoding="utf-8")
    scan_once(tmp_path, config)

    begin = cmd_compile_begin(tmp_path, config)
    sess = begin["session_id"]
    items_by_name = {Path(i["raw_rel_path"]).name: i for i in begin["items"]}
    poison_item = next(v for k, v in items_by_name.items() if "poison" in k)
    good_item = next(v for k, v in items_by_name.items() if "good" in k)

    # Isolate poison pill
    fail_res = cmd_compile_fail(
        tmp_path,
        config,
        session_id=sess,
        content_hash=poison_item["content_hash"],
        reason="文件编码损坏无法解析",
    )
    assert fail_res["state"] == "FAILED"
    assert fail_res["retry_count"] == 1

    # Compile good item and commit successfully
    _write_valid_concept(tmp_path, "正常概念.md", "正常概念", good_item["raw_rel_path"])
    commit_res = cmd_compile_commit(tmp_path, config, session_id=sess, note="编译正常文档并隔离毒丸")
    assert commit_res["compiled_count"] == 1
    assert commit_res["failed_count"] == 1
    assert "跳过失败文件" in (tmp_path / "wiki" / "log.md").read_text(encoding="utf-8")

    # Retry failed poison item
    retry_res = cmd_retry(tmp_path, config, content_hash=poison_item["content_hash"])
    assert retry_res["retried_count"] == 1
    assert retry_res["items"][0]["state"] == "PENDING_COMPILE"
    assert retry_res["items"][0]["last_error_message"] == "文件编码损坏无法解析"

    # Re-compile retried item
    begin2 = cmd_compile_begin(tmp_path, config)
    assert len(begin2["items"]) == 1
    assert begin2["items"][0]["content_hash"] == poison_item["content_hash"]


def test_gate3_and_gate4_en_to_zh_image_and_disaster_rebuild(tmp_path: Path) -> None:
    cmd_init(tmp_path)
    config = load_config(tmp_path)

    # Gate 4: Drop English article + PNG image into inbox/
    fixture_en = Path(__file__).parent / "fixtures" / "sample_en_article.md"
    en_bytes = fixture_en.read_bytes()
    png_bytes = make_test_png(64, 32)

    (tmp_path / "inbox" / "llm-wiki-architecture.md").write_bytes(en_bytes)
    (tmp_path / "inbox" / "核心架构图.png").write_bytes(png_bytes)

    scan_res = scan_once(tmp_path, config)
    assert scan_res["ingested_count"] == 2
    # Inbox cleaned up (moved to .processed/)
    assert not (tmp_path / "inbox" / "llm-wiki-architecture.md").exists()
    assert not (tmp_path / "inbox" / "核心架构图.png").exists()

    begin = cmd_compile_begin(tmp_path, config)
    assert len(begin["items"]) == 2
    img_item = next(i for i in begin["items"] if i["media_type"] == "image")
    txt_item = next(i for i in begin["items"] if i["media_type"] == "text")
    assert img_item["needs_vision"] is True
    assert img_item["asset_rel_path"] is not None
    assert (tmp_path / img_item["asset_rel_path"]).exists()

    # Compile EN -> ZH + embed image in wiki/
    _write_valid_concept(
        tmp_path,
        "知识复利效应.md",
        "知识复利效应",
        txt_item["raw_rel_path"],
        extra_body=f"\n\n![核心架构图](../../{img_item['asset_rel_path']})\n",
    )
    src_page = tmp_path / "wiki" / "sources" / "核心架构图精读.md"
    src_page.write_text(
        "---\n"
        "type: source\n"
        'title: "核心架构图精读"\n'
        'summary: "多模态读图提炼架构核心模块。"\n'
        "status: stable\n"
        "tags: [架构图]\n"
        'aliases: ["Core Architecture Diagram"]\n'
        "sources:\n"
        "  - id: s1\n"
        f'    ref: "../../{img_item["raw_rel_path"]}"\n'
        "---\n\n"
        "# 核心架构图精读\n\n"
        f"![核心架构图](../../{img_item['asset_rel_path']})\n",
        encoding="utf-8",
    )

    commit_res = cmd_compile_commit(tmp_path, config, session_id=begin["session_id"], note="英文文章与架构图全自动中文入库")
    assert commit_res["ok"] is True
    assert commit_res["compiled_count"] == 2
    index_text = (tmp_path / "wiki" / "index.md").read_text(encoding="utf-8")
    assert 'okf_version: "0.3"' in index_text
    assert "知识复利效应" in index_text

    # Gate 3: Physically delete entire .kb/ directory and run kb rebuild
    shutil.rmtree(tmp_path / ".kb")
    assert not (tmp_path / ".kb").exists()

    rebuild_res = rebuild_database(tmp_path, config)
    assert rebuild_res["ok"] is True
    assert rebuild_res["rebuilt_raw_count"] == 2
    assert rebuild_res["compiled_raw_count"] == 2
    assert rebuild_res["pages_indexed"] == 2
    assert rebuild_res["missing_assets"] == []

    # Re-drop the exact same text and exact same PNG into inbox/ -> 100% deduplicated!
    (tmp_path / "inbox" / "llm-wiki-architecture-copy.md").write_bytes(en_bytes)
    (tmp_path / "inbox" / "核心架构图-重复.png").write_bytes(png_bytes)
    rescan = scan_once(tmp_path, config)
    assert rescan["ingested_count"] == 0
    assert rescan["duplicate_count"] == 2
