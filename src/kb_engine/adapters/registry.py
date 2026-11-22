"""Dynamic extension-based plugin adapter registry."""

from __future__ import annotations

from pathlib import Path

from kb_engine.adapters.base import BaseAdapter
from kb_engine.adapters.image_adapter import ImageAdapter
from kb_engine.adapters.text_adapter import TextAdapter


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


def build_default_registry(enabled: list[str] | None = None) -> AdapterRegistry:
    available: dict[str, BaseAdapter] = {
        "text": TextAdapter(),
        "image": ImageAdapter(),
    }
    selected = enabled if enabled is not None else list(available.keys())
    registry = AdapterRegistry()
    for name in selected:
        adapter = available.get(name)
        if adapter is not None:
            registry.register(adapter)
    return registry
