"""Tests for section-level FTS, lifecycle retirement, creator/student modes & pluggable Embedding/Rerank."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kb_engine.cli import cmd_init
from kb_engine.config import EmbeddingConfig, RerankerConfig, load_config
from kb_engine.db import connect_db
from kb_engine.indexer import reindex_wiki
from kb_engine.retriever import query_wiki
from kb_engine.validator import lint_wiki


class _FakeEmbeddingProvider:
    """Deterministic in-memory semantic vector provider (simulates Qwen3/Voyage API)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.config = EmbeddingConfig(enabled=True, model="fake-embed-v1")
        self.fail = fail
        self.last_error: str | None = None
        self.embed_calls: int = 0

    def is_ready(self) -> tuple[bool, str | None]:
        return True, None

    def embed_texts(self, texts: list[str], *, is_query: bool = False) -> list[list[float]] | None:
        self.embed_calls += 1
        if self.fail:
            self.last_error = "embedding_api_error:simulated_offline"
            return None
        vecs: list[list[float]] = []
        for t in texts:
            # Map colloquial "打工" and formal "出售时间"/"线性收入" into the same semantic dimension!
            dim0 = 1.0 if any(k in t for k in ("打工", "出售时间", "线性收入")) else 0.05
            dim1 = 1.0 if any(k in t for k in ("熵增", "热力学", "吃饭", "能量")) else 0.05
            dim2 = 1.0 if any(k in t for k in ("无限游戏", "长期主义")) else 0.05
            vecs.append([dim0, dim1, dim2, 0.1])
        return vecs


class _FakeRerankerProvider:
    """Deterministic reranker prioritizing pages containing a target marker."""

    def __init__(self, prefer_keyword: str, *, score: float = 0.92, fail: bool = False) -> None:
        self.config = RerankerConfig(enabled=True, model="fake-rerank-v1", top_n=10, min_score_student=0.5)
        self.prefer_keyword = prefer_keyword
        self.score = score
        self.fail = fail
        self.last_error: str | None = None

    def is_ready(self) -> tuple[bool, str | None]:
        return True, None

    def rerank(self, query: str, documents: list[str], *, top_n: int | None = None) -> list[dict[str, Any]] | None:
        if self.fail:
            self.last_error = "reranker_api_error:simulated_timeout"
            return None
        scored = []
        for idx, doc in enumerate(documents):
            s = self.score if self.prefer_keyword in doc else 0.25
            scored.append({"index": idx, "relevance_score": s})
        scored.sort(key=lambda x: -x["relevance_score"])
        return scored[: (top_n or len(scored))]


def _seed_lifecycle_vault(root: Path) -> None:
    cmd_init(root)

    # 1. Public, stable, verified page (formal concept: 出售时间与线性收入)
    (root / "wiki" / "concepts" / "出售时间与线性收入.md").write_text(
        "---\n"
        "type: concept\n"
        'title: "出售时间与线性收入"\n'
        'summary: "仅靠出卖单位时间无法获得指数级财富复利。"\n'
        "status: stable\n"
        "visibility: public\n"
        "tags: [杠杆, 财富]\n"
        "verified:\n"
        '  - by: "human:owner"\n'
        '    at: "2026-11-22"\n'
        "---\n\n"
        "# 出售时间与线性收入\n\n"
        "## 核心机制\n"
        "出售时间是典型的线性收入模式，缺乏代码与媒体杠杆。\n",
        encoding="utf-8",
    )

    # 2. Public, stable, unverified page (also mentions 杠杆)
    (root / "wiki" / "concepts" / "媒体杠杆入门.md").write_text(
        "---\n"
        "type: concept\n"
        'title: "媒体杠杆入门"\n'
        'summary: "零边际成本的媒体杠杆放大个人影响力。"\n'
        "status: stable\n"
        "visibility: public\n"
        "tags: [杠杆, 媒体]\n"
        "---\n\n"
        "# 媒体杠杆入门\n\n"
        "## 底层逻辑\n"
        "媒体杠杆让一次创作可以被无限次复制与分发。\n",
        encoding="utf-8",
    )

    # 3. Private draft insight (must be hidden in student mode, visible in creator mode)
    (root / "wiki" / "insights" / "私人杠杆随想.md").write_text(
        "---\n"
        "type: insight\n"
        'title: "私人杠杆随想"\n'
        'summary: "个人未公开的杠杆思考草稿。"\n'
        "status: draft\n"
        "visibility: private\n"
        "tags: [杠杆]\n"
        "---\n\n"
        "# 私人杠杆随想\n\n"
        "## 未整理草稿\n"
        "这是一条私人草稿，讨论杠杆与个人情绪，不应对学员公开。\n",
        encoding="utf-8",
    )

    # 4. Deprecated page superseded by 出售时间与线性收入.md + expired stale_after
    (root / "wiki" / "concepts" / "旧版线性收入观念.md").write_text(
        "---\n"
        "type: concept\n"
        'title: "旧版线性收入观念"\n'
        'summary: "已淘汰的旧版杠杆与线性收入笔记。"\n'
        "status: deprecated\n"
        "visibility: public\n"
        'stale_after: "2025-01-01"\n'
        'superseded_by: "./出售时间与线性收入.md"\n'
        "tags: [杠杆]\n"
        "---\n\n"
        "# 旧版线性收入观念\n\n"
        "## 过时内容\n"
        "这是已经废弃的旧版线性收入与杠杆分析。\n",
        encoding="utf-8",
    )


def test_lifecycle_filtering_student_mode_and_lint(tmp_path: Path) -> None:
    _seed_lifecycle_vault(tmp_path)
    config = load_config(tmp_path)
    idx = reindex_wiki(tmp_path, config, regenerate_index=True)
    assert idx["pages_indexed"] == 4
    assert idx["sections_indexed"] >= 4

    # Lint should pass (superseded_by target exists) and emit STALE_PAGE warning
    lint_res = lint_wiki(tmp_path, config)
    assert lint_res["ok"] is True
    assert any(w["code"] == "STALE_PAGE" for w in lint_res["warnings"])

    # Creator mode excludes deprecated by default, includes private/draft
    res_creator = query_wiki(tmp_path, config, "杠杆", mode="creator")
    creator_paths = [x["rel_path"] for x in res_creator["l1_inventory"]]
    assert "insights/私人杠杆随想.md" in creator_paths
    assert "concepts/旧版线性收入观念.md" not in creator_paths

    # Creator mode with include_deprecated=True brings deprecated page back
    res_all = query_wiki(tmp_path, config, "杠杆", mode="creator", include_deprecated=True)
    assert "concepts/旧版线性收入观念.md" in [x["rel_path"] for x in res_all["l1_inventory"]]

    # Student mode strictly isolates public + stable only, and excludes private/draft/deprecated
    res_student = query_wiki(tmp_path, config, "杠杆", mode="student")
    student_paths = [x["rel_path"] for x in res_student["l1_inventory"]]
    assert "concepts/出售时间与线性收入.md" in student_paths
    assert "concepts/媒体杠杆入门.md" in student_paths
    assert "insights/私人杠杆随想.md" not in student_paths
    assert "concepts/旧版线性收入观念.md" not in student_paths
    assert res_student["confidence_warning"] is None

    # Student mode returns LOW_CONFIDENCE on miss
    res_miss = query_wiki(tmp_path, config, "外星飞船引擎", mode="student")
    assert res_miss["confidence_warning"] == "LOW_CONFIDENCE"


def test_pluggable_incremental_embeddings_rrf_rerank_and_graceful_degradation(tmp_path: Path) -> None:
    _seed_lifecycle_vault(tmp_path)
    config = load_config(tmp_path)
    fake_emb = _FakeEmbeddingProvider()

    # 1. First reindex computes embeddings for all 4 sections
    idx1 = reindex_wiki(tmp_path, config, regenerate_index=True, embedding_provider=fake_emb)
    assert idx1["embeddings_computed"] == 4
    assert fake_emb.embed_calls == 1

    # 2. Second reindex with NO content changes reuses 100% cache (0 API calls!)
    idx2 = reindex_wiki(tmp_path, config, regenerate_index=True, embedding_provider=fake_emb)
    assert idx2["embeddings_computed"] == 0
    assert fake_emb.embed_calls == 1

    # 3. Colloquial query "打工" has 0 lexical hits, but hybrid vector track rescues "出售时间与线性收入"!
    res_lex = query_wiki(tmp_path, config, "打工")
    assert res_lex["stats"]["direct_hits"] == 0
    assert res_lex["stats"]["hybrid_mode"] == "lexical"

    res_hyb = query_wiki(tmp_path, config, "打工", embedding_provider=fake_emb)
    assert res_hyb["stats"]["hybrid_mode"] == "hybrid"
    assert res_hyb["stats"]["vector_used"] is True
    assert res_hyb["l1_inventory"][0]["rel_path"] == "concepts/出售时间与线性收入.md"

    # 4. Reranker reorders candidates cleanly
    fake_rr = _FakeRerankerProvider(prefer_keyword="媒体杠杆入门", score=0.95)
    res_rr = query_wiki(tmp_path, config, "杠杆", mode="student", reranker_provider=fake_rr)
    assert res_rr["stats"]["reranker_used"] is True
    assert res_rr["l1_inventory"][0]["rel_path"] == "concepts/媒体杠杆入门.md"

    # 5. Graceful degradation: if embedding/reranker API fails, query still succeeds via lexical fallback!
    broken_emb = _FakeEmbeddingProvider(fail=True)
    broken_rr = _FakeRerankerProvider(prefer_keyword="媒体", fail=True)
    res_deg = query_wiki(
        tmp_path,
        config,
        "杠杆",
        embedding_provider=broken_emb,
        reranker_provider=broken_rr,
    )
    assert res_deg["ok"] is True
    assert res_deg["stats"]["direct_hits"] >= 2
    assert res_deg["stats"]["vector_used"] is False
    assert res_deg["stats"]["reranker_used"] is False
    assert len(res_deg["stats"]["degraded_reasons"]) == 2
