"""Text and Markdown input adapter (.md, .txt, and direct text)."""

from __future__ import annotations

import re
from pathlib import Path

from kb_engine.adapters.base import BaseAdapter, RawDocumentPayload
from kb_engine.errors import ErrorCode, KBError
from kb_engine.utils import (
    normalize_newlines,
    normalize_nfc,
    sanitize_filename,
    sha256_text,
)

_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


def _infer_title_from_text(text: str, fallback: str) -> str:
    match = _H1_RE.search(text)
    if match:
        candidate = sanitize_filename(match.group(1), fallback="")
        if candidate:
            return candidate
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("---"):
            candidate = sanitize_filename(stripped[:60], fallback="")
            if candidate:
                return candidate
    return sanitize_filename(fallback, fallback="未命名笔记")


class TextAdapter(BaseAdapter):
    name = "text"
    supported_extensions = (".md", ".markdown", ".txt")

    def extract(self, file_path: Path) -> RawDocumentPayload:
        try:
            raw_bytes = file_path.read_bytes()
        except OSError as exc:
            raise KBError(
                ErrorCode.ADAPTER_ERROR,
                f"无法读取文本文件 {file_path}: {exc}",
            ) from exc

        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = raw_bytes.decode("utf-8", errors="replace")

        normalized = normalize_newlines(normalize_nfc(text))
        content_hash = sha256_text(normalized)
        stem_title = sanitize_filename(file_path.stem, fallback="")
        suggested_title = stem_title or _infer_title_from_text(normalized, "未命名笔记")

        return RawDocumentPayload(
            suggested_title=suggested_title,
            media_type="text",
            content_hash=content_hash,
            markdown_content=normalized,
            asset_files=[],
            needs_vision=False,
            metadata={"original_filename": normalize_nfc(file_path.name)},
        )

    def extract_direct_text(self, text: str, title: str | None = None) -> RawDocumentPayload:
        normalized = normalize_newlines(normalize_nfc(text)).strip() + "\n"
        content_hash = sha256_text(normalized)
        suggested_title = (
            sanitize_filename(title, fallback="")
            if title
            else _infer_title_from_text(normalized, "随手灵感")
        )
        if not suggested_title:
            suggested_title = "随手灵感"
        return RawDocumentPayload(
            suggested_title=suggested_title,
            media_type="text",
            content_hash=content_hash,
            markdown_content=normalized,
            asset_files=[],
            needs_vision=False,
            metadata={"direct_text": True},
        )
