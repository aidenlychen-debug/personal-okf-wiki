"""Pluggable input adapters package."""

from __future__ import annotations

from kb_engine.adapters.base import BaseAdapter, RawDocumentPayload
from kb_engine.adapters.image_adapter import ImageAdapter
from kb_engine.adapters.registry import AdapterRegistry, build_default_registry
from kb_engine.adapters.text_adapter import TextAdapter

__all__ = [
    "AdapterRegistry",
    "BaseAdapter",
    "ImageAdapter",
    "RawDocumentPayload",
    "TextAdapter",
    "build_default_registry",
]
