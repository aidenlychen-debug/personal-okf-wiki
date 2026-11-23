"""Tests for zero-pollution cleaner, native PDF adapter registration, merge_wiki_pages & kb audit."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from kb_engine.adapters.cleaner import light_clean_text
from kb_engine.adapters.registry import build_default_registry
from kb_engine.cli import cmd_audit, cmd_init
from kb_engine.config import load_config
from kb_engine.indexer import reindex_wiki
from kb_engine.refactor import merge_wiki_pages
from kb_engine.validator import lint_wiki


def test_light_clean_text_strips_noise_and_merges_wraps_without_altering_meaning() -> None:
    dirty = (
        "---\n"
        'title: "测试文档"\n'
        "---\n\n"
        "# 测试章节\n\n"
        "0:00\n"
        "[00:01:23] 这是第一句被硬换行\n"
        "截断的中文段落 [Music]。\n"
        "- 12 -\n"
        "z-library.sk 1lib.sk z-lib.sk\n"
        "1分钟4秒钟\n"
        "第二段保留完整原意。\n\n"
        "```python\n"
        "0:00\n"
        "x = 12\n"
        "```\n"
    )
    cleaned = light_clean_text(dirty)
    assert "0:00\n这是" not in cleaned
    assert "[00:01:23]" not in cleaned
    assert "[Music]" not in cleaned
    assert "- 12 -" not in cleaned
    assert "z-library.sk" not in cleaned
    assert "1分钟4秒钟" not in cleaned
    # Broken wrap merged cleanly without extra spaces between CJK chars
    assert "这是第一句被硬换行截断的中文段落" in cleaned
    assert "第二段保留完整原意。" in cleaned
    # Frontmatter & code fence preserved verbatim
    assert 'title: "测试文档"' in cleaned
    assert "```python\n0:00\nx = 12\n```" in cleaned


def test_default_registry_includes_pdf_adapter() -> None:
    reg = build_default_registry(["text", "image", "pdf"])
    assert "pdf" in reg.enabled_names
    assert reg.get_for_file(Path("sample.pdf")) is not None


def test_merge_wiki_pages_rewrites_links_merges_metadata_and_retires_source(tmp_path: Path) -> None:
    cmd_init(tmp_path)
    config = load_config(tmp_path)

    (tmp_path / "wiki" / "concepts" / "主概念.md").write_text(
        "---\n"
        "type: concept\n"
        'title: "主概念"\n'
        'summary: "规范主概念页。"\n'
        "status: stable\n"
        "visibility: public\n"
        "tags: [核心]\n"
        'aliases: ["Main Concept"]\n'
        "verified:\n"
        '  - by: "human:owner"\n'
        '    at: "2026-11-22"\n'
        "---\n\n"
        "# 主概念\n\n"
        "## 核心要义\n"
        "主概念已有核心论述。\n",
        encoding="utf-8",
    )

    (tmp_path / "wiki" / "concepts" / "重复概念.md").write_text(
        "---\n"
        "type: concept\n"
        'title: "重复概念"\n'
        'summary: "待合并的重复概念页。"\n'
        "status: stable\n"
        "visibility: public\n"
        "tags: [核心, 补充标签]\n"
        'aliases: ["Duplicate Alias"]\n'
        "---\n\n"
        "# 重复概念\n\n"
        "## 补充观点\n"
        "这里有一条来自另一本书的补充观点。\n",
        encoding="utf-8",
    )

    (tmp_path / "wiki" / "cases" / "引用案例.md").write_text(
        "---\n"
        "type: case\n"
        'title: "引用案例"\n'
        'summary: "引用了重复概念的案例页。"\n'
        "status: stable\n"
        "visibility: public\n"
        "tags: [案例]\n"
        "---\n\n"
        "# 引用案例\n\n"
        "本案例印证了 [重复概念](../concepts/重复概念.md#补充观点)。\n",
        encoding="utf-8",
    )

    reindex_wiki(tmp_path, config, regenerate_index=True)

    res = merge_wiki_pages(tmp_path, config, "concepts/重复概念.md", "concepts/主概念.md")
    assert res["ok"] is True
    assert res["source_action"] == "deprecated"
    assert res["rewritten_links_count"] == 1

    # Target page has merged body, alias, tag, and preserved original verified prefix
    tgt_text = (tmp_path / "wiki" / "concepts" / "主概念.md").read_text(encoding="utf-8")
    assert "主概念已有核心论述。" in tgt_text
    assert "## 合并补充（来自 重复概念）" in tgt_text
    assert "Duplicate Alias" in tgt_text
    assert "重复概念" in tgt_text
    assert "补充标签" in tgt_text

    # Case page link rewritten to 主概念.md while preserving anchor
    case_text = (tmp_path / "wiki" / "cases" / "引用案例.md").read_text(encoding="utf-8")
    assert "../concepts/主概念.md#补充观点" in case_text

    # Source page retired with deprecated + superseded_by
    src_text = (tmp_path / "wiki" / "concepts" / "重复概念.md").read_text(encoding="utf-8")
    assert "status: deprecated" in src_text
    assert "superseded_by: ./主概念.md" in src_text or 'superseded_by: "./主概念.md"' in src_text

    # Lint passes with 0 errors
    lint_res = lint_wiki(tmp_path, config)
    assert lint_res["ok"] is True
    assert lint_res["error_count"] == 0

    # Audit reports deprecated page and public unverified queue cleanly
    audit_res = cmd_audit(tmp_path, config)
    assert audit_res["ok"] is True
    assert audit_res["counts"]["deprecated"] == 1
    assert audit_res["student_readiness"]["public_verified"] == 1
    assert audit_res["student_readiness"]["public_unverified"] >= 1

    # CLI audit --json works cleanly
    proc = subprocess.run(
        [sys.executable, "-m", "kb_engine.cli", "--root", str(tmp_path), "audit", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["ok"] is True
