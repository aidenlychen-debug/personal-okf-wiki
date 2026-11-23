"""Text, Markdown & Subtitle input adapter (.md, .txt, .srt, .vtt, and direct text)."""

from __future__ import annotations

import re
from pathlib import Path

from kb_engine.adapters.base import BaseAdapter, RawDocumentPayload
from kb_engine.adapters.cleaner import light_clean_text
from kb_engine.errors import ErrorCode, KBError
from kb_engine.utils import (
    normalize_newlines,
    normalize_nfc,
    sanitize_filename,
    sha256_text,
)

_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
_SUBTITLE_TS_RE = re.compile(r"^\s*(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s*-->\s*(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}.*$")
_SUBTITLE_TAG_RE = re.compile(r"</?(?:c|i|b|u|v|font)[^>]*>", re.IGNORECASE)


def _strip_subtitle_markup(text: str) -> str:
    """Strip SRT/VTT cue indices, WEBVTT headers, and timestamp range lines."""
    out: list[str] = []
    for raw_line in normalize_newlines(text).splitlines():
        s = raw_line.strip()
        if not s:
            out.append("")
            continue
        if s.upper().startswith("WEBVTT") or s.startswith("NOTE") or s.isdigit():
            continue
        if _SUBTITLE_TS_RE.match(s):
            continue
        cleaned_line = _SUBTITLE_TAG_RE.sub("", s).strip()
        if cleaned_line:
            out.append(cleaned_line)
    return "\n".join(out)


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
    supported_extensions = (".md", ".markdown", ".txt", ".srt", ".vtt")

    def extract(self, file_path: Path) -> RawDocumentPayload:
        try:
            raw_bytes = file_path.read_bytes()
        except OSError as exc:
            raise KBError(
                ErrorCode.ADAPTER_ERROR,
                f"无法读取文本文件 {file_path}: {exc}",
            ) from exc

        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw_bytes.decode("utf-8", errors="replace")

        if file_path.suffix.lower() in (".srt", ".vtt"):
            text = _strip_subtitle_markup(text)
        normalized = light_clean_text(text, merge_hard_wraps=True)
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
