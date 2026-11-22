"""BaseAdapter abstract contract and RawDocumentPayload dataclass."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RawDocumentPayload:
    suggested_title: str
    media_type: str
    content_hash: str
    markdown_content: str
    asset_files: list[Path] = field(default_factory=list)
    needs_vision: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseAdapter(ABC):
    name: str
    supported_extensions: tuple[str, ...]

    @abstractmethod
    def extract(self, file_path: Path) -> RawDocumentPayload:
        """Extract normalized payload from an input file without mutating the source."""
        ...
