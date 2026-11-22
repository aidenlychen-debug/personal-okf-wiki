"""Pydantic configuration models and loader for config.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

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
    summary: float = 4.0
    body: float = 1.0


class RetrievalConfig(BaseModel):
    default_l1_limit: int = Field(default=50, ge=1)
    default_l2_top_k: int = Field(default=10, ge=1)
    max_expanded_evidences: int = Field(default=15, ge=1)
    fts_weights: FTSWeightsConfig = Field(default_factory=FTSWeightsConfig)


class SafetyConfig(BaseModel):
    auto_git_commit: bool = True
    write_lock_timeout_seconds: float = Field(default=10.0, gt=0.0)
    max_shrinkage_ratio: float = Field(default=0.30, ge=0.0, le=1.0)
    enforce_human_blocks: bool = True
    enforce_verified_append_only: bool = True
    block_unexpected_deletions: bool = True


class AdaptersConfig(BaseModel):
    enabled: list[str] = Field(default_factory=lambda: ["text", "image"])


class KBConfig(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    inbox: InboxConfig = Field(default_factory=InboxConfig)
    watch_folders: list[str] = Field(default_factory=list)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)


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
  fts_weights:
    title: 10.0
    aliases: 8.0
    tags: 6.0
    summary: 4.0
    body: 1.0

safety:
  auto_git_commit: true
  write_lock_timeout_seconds: 10
  max_shrinkage_ratio: 0.30
  enforce_human_blocks: true
  enforce_verified_append_only: true
  block_unexpected_deletions: true

adapters:
  enabled: ["text", "image"]
"""


def load_config(root_dir: Path) -> KBConfig:
    """Load and validate config.yaml from the knowledge base root directory."""
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
