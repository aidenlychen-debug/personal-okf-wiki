"""Tests for V3.0 P0+P1 upgrades: metadata columns, mechanisms FTS, filters, firsthand boost & typed relations."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from kb_engine.cli import cmd_audit, cmd_init
from kb_engine.config import load_config
from kb_engine.indexer import reindex_wiki
from kb_engine.retriever import query_wiki
from kb_engine.validator import lint_wiki


def _seed_v3_vault(tmp_path: Path) -> None:
    cmd_init(tmp_path)

    # Page 1: firsthand insight with L2/L3 layers, mechanisms, and typed relations (confirmed + candidate + rejected)
    (tmp_path / "wiki" / "insights" / "个人实战复盘.md").write_text(
        "---\n"
        "type: insight\n"
        'title: "个人实战复盘"\n'
        'summary: "关于增长复利的一手复盘思考。"\n'
        "status: stable\n"
        "visibility: public\n"
        "reading_status: read\n"
        "experience_type: firsthand\n"
        "origin: human\n"
        "layers: [L2, L3]\n"
        'mechanisms: ["正反馈飞轮", "注意力复利"]\n'
        "relations:\n"
        '  - target: "../concepts/已确认关联概念.md"\n'
        '    type: "extends"\n'
        '    status: "confirmed"\n'
        '    layer: "L2"\n'
        '    reason: "直接印证了底层飞轮机制"\n'
        '  - target: "../concepts/候选跨界联想.md"\n'
        '    type: "analogizes"\n'
        '    status: "candidate"\n'
        '    layer: "L4"\n'
        '    reason: "可能与生物进化生态位同构"\n'
        '  - target: "../concepts/已否定弱关联.md"\n'
        '    type: "related"\n'
        '    status: "rejected"\n'
        '    reason: "复核后确认机制无关"\n'
        "---\n\n"
        "# 个人实战复盘\n\n"
        "增长复利需要长期坚持与高频迭代。\n",
        encoding="utf-8",
    )

    # Page 2: others insight with identical body text to test creator firsthand boost
    (tmp_path / "wiki" / "insights" / "他人经验转述.md").write_text(
        "---\n"
        "type: insight\n"
        'title: "他人经验转述"\n'
        'summary: "关于增长复利的他人经验笔记。"\n'
        "status: stable\n"
        "visibility: public\n"
        "reading_status: partial\n"
        "experience_type: others\n"
        "origin: mixed\n"
        "layers: [L1]\n"
        "---\n\n"
        "# 他人经验转述\n\n"
        "增长复利需要长期坚持与高频迭代。\n",
        encoding="utf-8",
    )

    # Page 3: evidence page with firsthand tag (should NOT get creator firsthand boost!)
    (tmp_path / "wiki" / "evidences" / "增长实证数据.md").write_text(
        "---\n"
        "type: evidence\n"
        'title: "增长实证数据"\n'
        'summary: "关于增长复利的实证统计记录。"\n'
        "status: stable\n"
        "visibility: public\n"
        "reading_status: read\n"
        "experience_type: firsthand\n"
        "origin: human\n"
        "layers: [L1]\n"
        "---\n\n"
        "# 增长实证数据\n\n"
        "增长复利需要长期坚持与高频迭代。\n",
        encoding="utf-8",
    )

    # Relation target pages
    for name, summary in [
        ("已确认关联概念", "已确认关联的目标概念。"),
        ("候选跨界联想", "待人类确认的候选跨界概念。"),
        ("已否定弱关联", "已被人类否定的弱关联概念。"),
    ]:
        (tmp_path / "wiki" / "concepts" / f"{name}.md").write_text(
            "---\n"
            "type: concept\n"
            f'title: "{name}"\n'
            f'summary: "{summary}"\n'
            "status: stable\n"
            "visibility: public\n"
            "---\n\n"
            f"# {name}\n\n"
            "用于测试类型化关系。\n",
            encoding="utf-8",
        )


def test_v3_mechanisms_fts_and_filters(tmp_path: Path) -> None:
    _seed_v3_vault(tmp_path)
    config = load_config(tmp_path)
    reindex_wiki(tmp_path, config, regenerate_index=True)

    # Mechanisms indexed in FTS even though "正反馈飞轮" is not in title/summary/body
    res_mech = query_wiki(tmp_path, config, "正反馈飞轮")
    assert any(item["rel_path"] == "insights/个人实战复盘.md" for item in res_mech["l1_inventory"])
    hit = next(i for i in res_mech["l1_inventory"] if i["rel_path"] == "insights/个人实战复盘.md")
    assert hit["reading_status"] == "read"
    assert hit["experience_type"] == "firsthand"
    assert hit["origin"] == "human"
    assert hit["layers"] == ["L2", "L3"]
    assert "正反馈飞轮" in hit["mechanisms"]

    # Filters: --layer, --experience-type, --reading-status
    res_l3 = query_wiki(tmp_path, config, "增长复利", layer="L3")
    assert [i["rel_path"] for i in res_l3["l1_inventory"]] == ["insights/个人实战复盘.md"]

    res_partial = query_wiki(tmp_path, config, "增长复利", reading_status="partial")
    assert [i["rel_path"] for i in res_partial["l1_inventory"]] == ["insights/他人经验转述.md"]

    res_others = query_wiki(tmp_path, config, "增长复利", experience_type="others")
    assert [i["rel_path"] for i in res_others["l1_inventory"]] == ["insights/他人经验转述.md"]


def test_v3_creator_firsthand_boost_does_not_inflate_evidence(tmp_path: Path) -> None:
    _seed_v3_vault(tmp_path)
    config = load_config(tmp_path)
    reindex_wiki(tmp_path, config, regenerate_index=True)

    res_creator = query_wiki(tmp_path, config, "增长复利", mode="creator")
    scores_creator = {i["rel_path"]: i["score"] for i in res_creator["l1_inventory"]}
    # Firsthand insight outranks otherwise identical others insight AND evidence page in creator mode
    assert scores_creator["insights/个人实战复盘.md"] > scores_creator["insights/他人经验转述.md"]
    assert scores_creator["insights/个人实战复盘.md"] > scores_creator["evidences/增长实证数据.md"]

    # In student mode, firsthand boost is NOT applied
    res_student = query_wiki(tmp_path, config, "增长复利", mode="student")
    scores_student = {i["rel_path"]: i["score"] for i in res_student["l1_inventory"]}
    assert scores_student["insights/个人实战复盘.md"] == scores_student["insights/他人经验转述.md"]


def test_v3_typed_relations_confirmed_vs_candidate_vs_rejected(tmp_path: Path) -> None:
    _seed_v3_vault(tmp_path)
    config = load_config(tmp_path)
    reindex_wiki(tmp_path, config, regenerate_index=True)

    res = query_wiki(tmp_path, config, "正反馈飞轮")
    confirmed_paths = {n["rel_path"] for n in res["confirmed_relations"]}
    candidate_paths = {n["rel_path"] for n in res["candidate_relations"]}

    assert "concepts/已确认关联概念.md" in confirmed_paths
    assert "concepts/候选跨界联想.md" in candidate_paths
    assert "concepts/候选跨界联想.md" not in confirmed_paths
    # Rejected relation is excluded from both confirmed and candidate
    assert "concepts/已否定弱关联.md" not in confirmed_paths
    assert "concepts/已否定弱关联.md" not in candidate_paths

    # Relation metadata preserved
    conf_item = next(n for n in res["confirmed_relations"] if n["rel_path"] == "concepts/已确认关联概念.md")
    assert conf_item["relation_type"] == "extends"
    assert conf_item["relation_layer"] == "L2"
    assert "底层飞轮" in conf_item["relation_reason"]

    # Audit counts candidate relations & V3 coverage
    audit = cmd_audit(tmp_path, config)
    assert audit["ok"] is True
    assert audit["counts"]["candidate_relations"] == 1
    assert audit["v3_coverage"]["experience_type"]["firsthand"] == 2
    assert audit["v3_coverage"]["layers"]["L2"] == 1


def test_v3_validator_and_cli_flags(tmp_path: Path) -> None:
    _seed_v3_vault(tmp_path)
    config = load_config(tmp_path)
    reindex_wiki(tmp_path, config, regenerate_index=True)

    lint_ok = lint_wiki(tmp_path, config)
    assert lint_ok["ok"] is True

    # CLI flags --layer / --experience-type / --reading-status
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kb_engine.cli",
            "--root",
            str(tmp_path),
            "query",
            "增长复利",
            "--layer",
            "L3",
            "--experience-type",
            "firsthand",
            "--reading-status",
            "read",
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert len(payload["l1_inventory"]) == 1
    assert payload["l1_inventory"][0]["rel_path"] == "insights/个人实战复盘.md"

    # Invalid V3 metadata triggers OKF_SCHEMA_ERROR
    (tmp_path / "wiki" / "concepts" / "错误元数据.md").write_text(
        "---\n"
        "type: concept\n"
        'title: "错误元数据"\n'
        'summary: "用于测试非法 V3 枚举值。"\n'
        "reading_status: invalid_val\n"
        "layers: [L9]\n"
        "---\n\n"
        "# 错误元数据\n",
        encoding="utf-8",
    )
    lint_bad = lint_wiki(tmp_path, config)
    assert lint_bad["ok"] is False
    assert lint_bad["error_count"] >= 2
