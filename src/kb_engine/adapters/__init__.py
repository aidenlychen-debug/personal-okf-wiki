"""Extensible input adapters (text/subtitles, image/heic, pdf, html, office/epub, url, media)."""

from kb_engine.adapters.base import BaseAdapter, RawDocumentPayload
from kb_engine.adapters.html_adapter import HtmlAdapter
from kb_engine.adapters.image_adapter import ImageAdapter
from kb_engine.adapters.media_adapter import MediaAdapter
from kb_engine.adapters.office_adapter import OfficeAdapter
from kb_engine.adapters.pdf_adapter import PdfAdapter
from kb_engine.adapters.registry import AdapterRegistry, build_default_registry
from kb_engine.adapters.text_adapter import TextAdapter
from kb_engine.adapters.url_adapter import UrlAdapter

__all__ = [
    "AdapterRegistry",
    "BaseAdapter",
    "HtmlAdapter",
    "ImageAdapter",
    "MediaAdapter",
    "OfficeAdapter",
    "PdfAdapter",
    "RawDocumentPayload",
    "TextAdapter",
    "UrlAdapter",
    "build_default_registry",
]
