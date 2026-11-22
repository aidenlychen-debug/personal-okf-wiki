"""Gates 5, 6, 7 tests: Dual-track eval, clean JSON stdout, 4 safety locks & deterministic rename."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from kb_engine.cli import cmd_compile_begin, cmd_compile_commit, cmd_init
from kb_engine.config import load_config
from kb_engine.errors import ErrorCode
from kb_engine.evaluator import run_eval
from kb_engine.indexer import reindex_wiki
from kb_engine.refactor import rename_wiki_page
from kb_engine.utils import git_head_sha, run_git
from kb_engine.validator import lint_wiki


def _seed_golden_wiki(root: Path) -> str:
    """Create interlinked golden wiki pages matching tests/eval_cases.yaml and commit them."""
    cmd_init(root)
    config = load_config(root)

    # Create a dummy raw source
    raw_src = root / "raw" / "2026-11-21-llm-wiki-architecture.md"
    raw_src.write_text("# LLM Wiki Architecture\nOKF-v0.3 reference.\n", encoding="utf-8")

    # 1. Concept page (verified + human block + external link + anchor)
    (root / "wiki" / "concepts" / "知识复利效应.md").write_text(
        "---\n"
        "type: concept\n"
        'title: "知识复利效应"\n'
        'summary: "持续预编译与互联让知识产生指数级复利。"\n'
        "status: stable\n"
        "tags: [知识管理, 内容创作]\n"
        'aliases: ["Knowledge Compounding"]\n'
        'domain: "personal-growth"\n'
        "visibility: public\n"
        "verified:\n"
        '  - by: "human:owner"\n'
        '    at: "2026-11-21"\n'
        "sources:\n"
        "  - id: s1\n"
        '    ref: "../../raw/2026-11-21-llm-wiki-architecture.md"\n'
        "---\n\n"
        "# 知识复利效应\n\n"
        "## 核心要义\n"
        "持续预编译与交叉链接让个人知识库越用越厚，而不是越存越乱。参考外部标准 [Google OKF](https://example.com/okf) 以及页内 [关联知识](#关联知识)。\n\n"
        "<!-- human:start -->\n"
        "这是人类亲手写的批注，禁止任何 AI 改动一个字。\n"
        "<!-- human:end -->\n\n"
        "## 关联知识\n"
        "- 相关案例：[某团队落地案例](../cases/某团队落地案例.md)\n"
        "- 支撑证据：[检索效率对比数据](../evidences/检索效率对比数据.md)\n"
        "- 来源精读：[LLM Wiki 架构解析](../sources/LLM-Wiki-架构解析.md)\n",
        encoding="utf-8",
    )

    # 2. Case page (references concept with plain Chinese + anchor)
    (root / "wiki" / "cases" / "某团队落地案例.md").write_text(
        "---\n"
        "type: case\n"
        'title: "某团队落地案例"\n'
        'summary: "创作者团队使用预编译知识库提升产文效率。"\n'
        "status: stable\n"
        "tags: [案例, 效率]\n"
        "---\n\n"
        "# 某团队落地案例\n\n"
        "该团队落地了 [知识复利效应](../concepts/知识复利效应.md#核心要义)，并参考了 [外链说明](https://obsidian.md)。\n",
        encoding="utf-8",
    )

    # 3. Evidence page (references concept with URL-encoded link!)
    (root / "wiki" / "evidences" / "检索效率对比数据.md").write_text(
        "---\n"
        "type: evidence\n"
        'title: "检索效率对比数据"\n'
        'summary: "预编译图谱检索召回率显著高于纯切片。"\n'
        "status: stable\n"
        "tags: [数据, 检索]\n"
        "sources:\n"
        "  - id: s1\n"
        '    ref: "../../raw/2026-11-21-llm-wiki-architecture.md"\n'
        "---\n\n"
        "# 检索效率对比数据\n\n"
        "实测数据支撑 [知识复利](../concepts/%E7%9F%A5%E8%AF%86%E5%A4%8D%E5%88%A9%E6%95%88%E5%BA%94.md)。\n",
        encoding="utf-8",
    )

    # 4. Source page (contains hyphenated OKF-v0.3)
    (root / "wiki" / "sources" / "LLM-Wiki-架构解析.md").write_text(
        "---\n"
        "type: source\n"
        'title: "LLM Wiki 架构解析"\n'
        'summary: "解析 Karpathy LLM Wiki 与 OKF-v0.3 结合架构。"\n'
        "status: stable\n"
        "tags: [架构, OKF]\n"
        'aliases: ["OKF-v0.3", "LLM Wiki"]\n'
        "sources:\n"
        "  - id: s1\n"
        '    ref: "../../raw/2026-11-21-llm-wiki-architecture.md"\n'
        "---\n\n"
        "# LLM Wiki 架构解析\n\n"
        "本文基于 OKF-v0.3 规范，核心概念见 [知识复利效应](../concepts/知识复利效应.md)。\n",
        encoding="utf-8",
    )

    reindex_wiki(root, config, regenerate_index=True)
    run_git(root, ["add", "-A", "--", "wiki", "raw"])
    run_git(root, ["commit", "-m", "feat(wiki): seed golden wiki"])
    head = git_head_sha(root)
    assert head is not None
    return head


def test_gate5_eval_and_pure_json_cli_stdout(tmp_path: Path) -> None:
    _seed_golden_wiki(tmp_path)
    config = load_config(tmp_path)

    cases_file = Path(__file__).parent / "eval_cases.yaml"
    eval_res = run_eval(tmp_path, config, cases_file)
    assert eval_res["ok"] is True
    assert eval_res["metrics"]["recall"] == 1.0
    assert eval_res["metrics"]["negative_false_positives"] == 0
    assert eval_res["metrics"]["max_latency_ms"] < 200.0

    # Verify CLI --json stdout is 100% pure single-object JSON (no jieba/log noise)
    proc_ok = subprocess.run(
        [sys.executable, "-m", "kb_engine.cli", "--root", str(tmp_path), "query", "复利", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc_ok.returncode == 0
    parsed_ok = json.loads(proc_ok.stdout)
    assert parsed_ok["ok"] is True
    assert parsed_ok["stats"]["direct_hits"] >= 1

    # Verify CLI --json error contract returns non-zero & pure single JSON object
    proc_err = subprocess.run(
        [
            sys.executable,
            "-m",
            "kb_engine.cli",
            "--root",
            str(tmp_path),
            "compile-commit",
            "--session",
            "sess_nonexistent",
            "--note",
            "test",
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc_err.returncode != 0
    parsed_err = json.loads(proc_err.stdout)
    assert parsed_err["ok"] is False
    assert parsed_err["error_code"] == ErrorCode.SESSION_NOT_FOUND.value


def test_gate6_four_safety_locks_and_external_links(tmp_path: Path) -> None:
    base_sha = _seed_golden_wiki(tmp_path)
    config = load_config(tmp_path)

    # External https:// links & #anchors produce 0 errors
    clean_lint = lint_wiki(tmp_path, config, base_commit=base_sha)
    assert clean_lint["ok"] is True
    assert clean_lint["error_count"] == 0

    concept_path = tmp_path / "wiki" / "concepts" / "知识复利效应.md"
    orig_concept = concept_path.read_text(encoding="utf-8")

    # 1. Modify 1 character inside <!-- human:start --> -> HUMAN_BLOCK_MODIFIED
    concept_path.write_text(orig_concept.replace("禁止任何 AI 改动一个字。", "禁止任何 AI 改动一个字!"), encoding="utf-8")
    r1 = lint_wiki(tmp_path, config, base_commit=base_sha)
    assert not r1["ok"]
    assert any(e["code"] == ErrorCode.HUMAN_BLOCK_MODIFIED.value for e in r1["errors"])

    # 2a. Verified page: appending at the end MUST PASS
    concept_path.write_text(orig_concept.rstrip() + "\n\n## 追加补充\n新增一条验证后补充内容。\n", encoding="utf-8")
    r2_ok = lint_wiki(tmp_path, config, base_commit=base_sha)
    assert r2_ok["ok"] is True

    # 2b. Verified page: modifying earlier body -> VERIFIED_CONTENT_MODIFIED
    concept_path.write_text(orig_concept.replace("越用越厚", "越用越多"), encoding="utf-8")
    r2_fail = lint_wiki(tmp_path, config, base_commit=base_sha)
    assert not r2_fail["ok"]
    assert any(e["code"] == ErrorCode.VERIFIED_CONTENT_MODIFIED.value for e in r2_fail["errors"])

    # Restore concept page
    concept_path.write_text(orig_concept, encoding="utf-8")

    # 3. Unexpected deletion -> UNEXPECTED_FILE_DELETED
    case_path = tmp_path / "wiki" / "cases" / "某团队落地案例.md"
    orig_case = case_path.read_text(encoding="utf-8")
    case_path.unlink()
    r3 = lint_wiki(tmp_path, config, base_commit=base_sha)
    assert not r3["ok"]
    assert any(e["code"] == ErrorCode.UNEXPECTED_FILE_DELETED.value for e in r3["errors"])
    case_path.write_text(orig_case, encoding="utf-8")

    # 4. Content shrinkage > 30% -> CONTENT_SHRINKAGE
    case_path.write_text(
        "---\ntype: case\ntitle: \"某团队落地案例\"\nsummary: \"简短摘要\"\n---\n\n# 简短\n短。\n",
        encoding="utf-8",
    )
    r4 = lint_wiki(tmp_path, config, base_commit=base_sha)
    assert not r4["ok"]
    assert any(e["code"] == ErrorCode.CONTENT_SHRINKAGE.value for e in r4["errors"])


def test_gate7_deterministic_rename_zero_broken_links(tmp_path: Path) -> None:
    _seed_golden_wiki(tmp_path)
    config = load_config(tmp_path)

    rename_res = rename_wiki_page(
        tmp_path,
        config,
        "concepts/知识复利效应.md",
        "concepts/知识复利飞轮.md",
        new_title="知识复利飞轮",
    )
    assert rename_res["ok"] is True
    assert rename_res["rewritten_links_count"] == 3  # case (with anchor) + evidence (URL-encoded) + source (plain)
    assert (tmp_path / "wiki" / "concepts" / "知识复利飞轮.md").exists()
    assert not (tmp_path / "wiki" / "concepts" / "知识复利效应.md").exists()

    # Check anchor preserved and external link untouched
    case_text = (tmp_path / "wiki" / "cases" / "某团队落地案例.md").read_text(encoding="utf-8")
    assert "../concepts/知识复利飞轮.md#核心要义" in case_text
    assert "https://obsidian.md" in case_text

    # Check URL-encoded link rewritten cleanly
    ev_text = (tmp_path / "wiki" / "evidences" / "检索效率对比数据.md").read_text(encoding="utf-8")
    assert "../concepts/知识复利飞轮.md" in ev_text

    # Full lint must report 0 errors and 0 broken links
    lint_res = lint_wiki(tmp_path, config)
    assert lint_res["ok"] is True
    assert lint_res["error_count"] == 0
