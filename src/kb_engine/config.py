"""Pydantic configuration models and loader for config.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError

from kb_engine.errors import ErrorCode, KBError


class ProjectConfig(BaseModel):
    name: str = "Personal OKF-Wiki"
    language: str = "zh-CN"


class InboxConfig(BaseModel):
    path: str = "inbox"
    cleanup_mode: Literal["archive", "delete"] = "archive"
    archive_retention_days: int = Field(default=7, ge=0)
    debounce_seconds: float = Field(default=1.5, ge=0.0)


class ScannerConfig(BaseModel):
    poll_interval_seconds: float = Field(default=30.0, gt=0.0)
    ignore_globs: list[str] = Field(
        default_factory=lambda: [
            ".*",
            "~$*",
            "*.tmp",
            "*.part",
            "*.crdownload",
            "Thumbs.db",
        ]
    )


class FTSWeightsConfig(BaseModel):
    title: float = 10.0
    aliases: float = 8.0
    tags: float = 6.0
    mechanisms: float = 7.0
    summary: float = 4.0
    body: float = 1.0


class EmbeddingConfig(BaseModel):
    enabled: bool = False
    provider: Literal["openai_compatible", "gemini"] = "openai_compatible"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "text-embedding-v4"
    api_key_env: str = "KB_EMBEDDING_API_KEY"
    dimensions: Optional[int] = None
    query_instruction: str = ""
    timeout_seconds: float = Field(default=15.0, gt=0.0)
    batch_size: int = Field(default=16, ge=1)


class RerankerConfig(BaseModel):
    enabled: bool = False
    provider: Literal["standard_rerank"] = "standard_rerank"
    base_url: str = "https://api.siliconflow.cn/v1/rerank"
    model: str = "Qwen/Qwen3-Reranker-8B"
    api_key_env: str = "KB_RERANK_API_KEY"
    top_n: int = Field(default=15, ge=1)
    min_score_student: float = Field(default=0.35, ge=0.0, le=1.0)
    timeout_seconds: float = Field(default=15.0, gt=0.0)


class RetrievalConfig(BaseModel):
    default_l1_limit: int = Field(default=50, ge=1)
    default_l2_top_k: int = Field(default=10, ge=1)
    max_expanded_evidences: int = Field(default=15, ge=1)
    min_strict_hits: int = Field(default=3, ge=1)
    or_fallback_min_score_ratio: float = Field(default=0.15, ge=0.0, le=1.0)
    graph_seed_top_k: int = Field(default=5, ge=1)
    max_matched_sections: int = Field(default=3, ge=1)
    rrf_k: int = Field(default=60, ge=1)
    section_boost_weight: float = Field(default=0.6, ge=0.0)
    verified_boost_multiplier: float = Field(default=1.25, ge=1.0)
    firsthand_boost_multiplier: float = Field(default=1.15, ge=1.0)
    fts_weights: FTSWeightsConfig = Field(default_factory=FTSWeightsConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    reranker: RerankerConfig = Field(default_factory=RerankerConfig)


class SafetyConfig(BaseModel):
    auto_git_commit: bool = True
    write_lock_timeout_seconds: float = Field(default=10.0, gt=0.0)
    max_shrinkage_ratio: float = Field(default=0.30, ge=0.0, le=1.0)
    enforce_human_blocks: bool = True
    enforce_verified_append_only: bool = True
    block_unexpected_deletions: bool = True


class AdaptersConfig(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: ["text", "image", "pdf", "html", "office", "url", "media"]
    )


class TranscriptionConfig(BaseModel):
    enabled: bool = False
    base_url: str = "https://api.siliconflow.cn/v1/audio/transcriptions"
    model: str = "FunAudioLLM/SenseVoiceSmall"
    api_key_env: str = "KB_TRANSCRIPTION_API_KEY"
    language: str = "zh"
    timeout_seconds: float = Field(default=180.0, gt=0.0)


class EvolutionConfig(BaseModel):
    enabled: bool = True
    initial_interval: int = Field(default=5, ge=1)
    backoff_intervals: list[int] = Field(default_factory=lambda: [5, 10, 20, 40])
    pain_zero_hit_ratio: float = Field(default=0.30, ge=0.0, le=1.0)
    min_pain_queries: int = Field(default=5, ge=1)


class KBConfig(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    inbox: InboxConfig = Field(default_factory=InboxConfig)
    watch_folders: list[str] = Field(default_factory=list)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)


DEFAULT_CONFIG_YAML = """project:
  name: "Personal OKF-Wiki"
  language: "zh-CN"

inbox:
  path: "inbox"
  cleanup_mode: "archive"
  archive_retention_days: 7
  debounce_seconds: 1.5

watch_folders: []

scanner:
  poll_interval_seconds: 30
  ignore_globs:
    - ".*"
    - "~$*"
    - "*.tmp"
    - "*.part"
    - "*.crdownload"
    - "Thumbs.db"

retrieval:
  default_l1_limit: 50
  default_l2_top_k: 10
  max_expanded_evidences: 15
  min_strict_hits: 3
  or_fallback_min_score_ratio: 0.15
  graph_seed_top_k: 5
  max_matched_sections: 3
  rrf_k: 60
  section_boost_weight: 0.6
  verified_boost_multiplier: 1.25
  firsthand_boost_multiplier: 1.15
  fts_weights:
    title: 10.0
    aliases: 8.0
    tags: 6.0
    mechanisms: 7.0
    summary: 4.0
    body: 1.0
  embedding:
    enabled: false
    provider: "openai_compatible"
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: "text-embedding-v4"
    api_key_env: "KB_EMBEDDING_API_KEY"
    dimensions: null
    query_instruction: ""
    timeout_seconds: 15
    batch_size: 16
  reranker:
    enabled: false
    provider: "standard_rerank"
    base_url: "https://api.siliconflow.cn/v1/rerank"
    model: "Qwen/Qwen3-Reranker-8B"
    api_key_env: "KB_RERANK_API_KEY"
    top_n: 15
    min_score_student: 0.35
    timeout_seconds: 15

safety:
  auto_git_commit: true
  write_lock_timeout_seconds: 10
  max_shrinkage_ratio: 0.30
  enforce_human_blocks: true
  enforce_verified_append_only: true
  block_unexpected_deletions: true

adapters:
  enabled: ["text", "image", "pdf", "html", "office", "url", "media"]

transcription:
  enabled: false
  base_url: "https://api.siliconflow.cn/v1/audio/transcriptions"
  model: "FunAudioLLM/SenseVoiceSmall"
  api_key_env: "KB_TRANSCRIPTION_API_KEY"
  language: "zh"
  timeout_seconds: 180

evolution:
  enabled: true
  initial_interval: 5
  backoff_intervals: [5, 10, 20, 40]
  pain_zero_hit_ratio: 0.30
  min_pain_queries: 5
"""


def _load_local_env(root_dir: Path) -> None:
    """Load local secrets from .kb/.env into os.environ without overriding existing env vars."""
    env_path = root_dir / ".kb" / ".env"
    if not env_path.is_file():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)
    except OSError:
        return


def load_config(root_dir: Path) -> KBConfig:
    """Load and validate config.yaml from the knowledge base root directory."""
    _load_local_env(root_dir)
    config_path = root_dir / "config.yaml"
    if not config_path.exists():
        return KBConfig()
    try:
        raw_text = config_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
        if data is None:
            return KBConfig()
        if not isinstance(data, dict):
            raise KBError(
                ErrorCode.CONFIG_INVALID,
                "config.yaml 顶层结构必须为映射对象。",
                details={"path": str(config_path)},
            )
        return KBConfig.model_validate(data)
    except KBError:
        raise
    except (yaml.YAMLError, ValidationError) as exc:
        raise KBError(
            ErrorCode.CONFIG_INVALID,
            f"config.yaml 解析或校验失败: {exc}",
            details={"path": str(config_path)},
        ) from exc
