"""Pluggable zero-dependency HTTP providers for Cloud Embedding & Rerank APIs."""

from __future__ import annotations

import array
import json
import math
import os
import urllib.error
import urllib.request
from typing import Any

from kb_engine.config import EmbeddingConfig, RerankerConfig


def pack_vector(vec: list[float]) -> bytes:
    """Pack float list into compact little-endian float32 bytes for SQLite BLOB."""
    arr = array.array("f", vec)
    return arr.tobytes()


def unpack_vector(blob: bytes) -> array.array:
    """Unpack float32 SQLite BLOB back to float array."""
    arr = array.array("f")
    arr.frombytes(blob)
    return arr


def cosine_similarity(vec_a: Any, vec_b: Any) -> float:
    """Compute cosine similarity between two equal-length float sequences."""
    if len(vec_a) != len(vec_b) or not vec_a:
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for a, b in zip(vec_a, vec_b):
        dot += a * b
        norm_a += a * a
        norm_b += b * b
    denom = math.sqrt(norm_a) * math.sqrt(norm_b)
    if denom <= 1e-12:
        return 0.0
    return dot / denom


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


class EmbeddingProvider:
    """Standard pluggable Embedding API client (OpenAI-compatible / Voyage / Qwen3 / Gemini)."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self.last_error: str | None = None

    def get_api_key(self) -> str:
        if not self.config.api_key_env:
            return ""
        return os.environ.get(self.config.api_key_env, "").strip()

    def is_ready(self) -> tuple[bool, str | None]:
        if not self.config.enabled:
            return False, "embedding_disabled"
        if not self.get_api_key():
            return False, f"missing_env:{self.config.api_key_env}"
        return True, None

    def embed_texts(self, texts: list[str], *, is_query: bool = False) -> list[list[float]] | None:
        """Embed a batch of texts. Returns None and sets last_error on any failure."""
        ready, reason = self.is_ready()
        if not ready:
            self.last_error = reason
            return None
        if not texts:
            return []

        prepared = texts
        if is_query and self.config.query_instruction:
            prefix = self.config.query_instruction
            prepared = [f"{prefix}{t}" for t in texts]

        api_key = self.get_api_key()
        try:
            if self.config.provider == "gemini":
                return self._embed_gemini(prepared, api_key)
            return self._embed_openai_compatible(prepared, api_key)
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
            self.last_error = f"embedding_api_error:{exc}"
            return None

    def _embed_openai_compatible(self, texts: list[str], api_key: str) -> list[list[float]]:
        base = self.config.base_url.rstrip("/")
        url = base if base.endswith("/embeddings") else f"{base}/embeddings"
        batch_size = max(1, self.config.batch_size)
        all_vecs: list[list[float]] = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            payload: dict[str, Any] = {
                "model": self.config.model,
                "input": batch,
            }
            if self.config.dimensions:
                payload["dimensions"] = self.config.dimensions

            resp = _post_json(
                url,
                payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=self.config.timeout_seconds,
            )
            items = sorted(resp["data"], key=lambda x: x.get("index", 0))
            for item in items:
                all_vecs.append([float(v) for v in item["embedding"]])

        return all_vecs

    def _embed_gemini(self, texts: list[str], api_key: str) -> list[list[float]]:
        base = self.config.base_url.rstrip("/")
        model = self.config.model
        if not model.startswith("models/"):
            model = f"models/{model}"
        url = f"{base}/{model}:batchEmbedContents?key={api_key}"
        requests_payload = []
        for t in texts:
            item: dict[str, Any] = {
                "model": model,
                "content": {"parts": [{"text": t}]},
            }
            if self.config.dimensions:
                item["outputDimensionality"] = self.config.dimensions
            requests_payload.append(item)

        resp = _post_json(
            url,
            {"requests": requests_payload},
            headers={},
            timeout=self.config.timeout_seconds,
        )
        return [[float(v) for v in emb["values"]] for emb in resp["embeddings"]]


class RerankerProvider:
    """Standard pluggable Rerank API client (Cohere / Voyage / Qwen3-Reranker / Jina)."""

    def __init__(self, config: RerankerConfig) -> None:
        self.config = config
        self.last_error: str | None = None

    def get_api_key(self) -> str:
        if not self.config.api_key_env:
            return ""
        return os.environ.get(self.config.api_key_env, "").strip()

    def is_ready(self) -> tuple[bool, str | None]:
        if not self.config.enabled:
            return False, "reranker_disabled"
        if not self.get_api_key():
            return False, f"missing_env:{self.config.api_key_env}"
        return True, None

    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
    ) -> list[dict[str, Any]] | None:
        """Return list of {'index': int, 'relevance_score': float} sorted desc, or None on error."""
        ready, reason = self.is_ready()
        if not ready:
            self.last_error = reason
            return None
        if not documents:
            return []

        api_key = self.get_api_key()
        eff_top_n = min(len(documents), top_n or self.config.top_n)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "query": query,
            "documents": documents,
            "top_n": eff_top_n,
        }
        try:
            resp = _post_json(
                self.config.base_url,
                payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=self.config.timeout_seconds,
            )
            raw_results = resp.get("results") or resp.get("data") or []
            parsed: list[dict[str, Any]] = []
            for item in raw_results:
                idx = int(item["index"])
                score = float(
                    item.get("relevance_score", item.get("score", 0.0))
                )
                parsed.append({"index": idx, "relevance_score": score})
            parsed.sort(key=lambda x: -x["relevance_score"])
            return parsed
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
            self.last_error = f"reranker_api_error:{exc}"
            return None
