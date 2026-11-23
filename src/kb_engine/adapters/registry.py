"""Dynamic extension-based plugin adapter registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kb_engine.adapters.base import BaseAdapter
from kb_engine.adapters.html_adapter import HtmlAdapter
from kb_engine.adapters.image_adapter import ImageAdapter
from kb_engine.adapters.media_adapter import MediaAdapter
from kb_engine.adapters.office_adapter import OfficeAdapter
from kb_engine.adapters.pdf_adapter import PdfAdapter
from kb_engine.adapters.text_adapter import TextAdapter
from kb_engine.adapters.url_adapter import UrlAdapter


class AdapterRegistry:
    """Registry mapping file extensions to enabled BaseAdapter instances."""

    def __init__(self) -> None:
        self._adapters_by_name: dict[str, BaseAdapter] = {}
        self._adapters_by_ext: dict[str, BaseAdapter] = {}

    def register(self, adapter: BaseAdapter) -> None:
        self._adapters_by_name[adapter.name] = adapter
        for ext in adapter.supported_extensions:
            self._adapters_by_ext[ext.lower()] = adapter

    def get_for_file(self, file_path: Path) -> BaseAdapter | None:
        return self._adapters_by_ext.get(file_path.suffix.lower())

    def get_by_name(self, name: str) -> BaseAdapter | None:
        return self._adapters_by_name.get(name)

    @property
    def enabled_names(self) -> list[str]:
        return sorted(self._adapters_by_name.keys())

    @property
    def supported_extensions(self) -> list[str]:
        return sorted(self._adapters_by_ext.keys())


def build_default_registry(
    enabled: list[str] | None = None,
    *,
    transcription_config: Any = None,
) -> AdapterRegistry:
    available: dict[str, BaseAdapter] = {
        "text": TextAdapter(),
        "image": ImageAdapter(),
        "pdf": PdfAdapter(),
        "html": HtmlAdapter(),
        "office": OfficeAdapter(),
        "url": UrlAdapter(),
        "media": MediaAdapter(transcription_config),
    }
    selected = enabled if enabled is not None else list(available.keys())
    registry = AdapterRegistry()
    for name in selected:
        adapter = available.get(name)
        if adapter is not None:
            registry.register(adapter)
    return registry
