"""Zero-dependency HTML to clean Markdown adapter (.html, .htm)."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from pathlib import Path

from kb_engine.adapters.base import BaseAdapter, RawDocumentPayload
from kb_engine.adapters.cleaner import light_clean_text
from kb_engine.errors import ErrorCode, KBError
from kb_engine.utils import normalize_nfc, sanitize_filename, sha256_text

_SKIP_TAGS = frozenset({"script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "iframe", "form"})
_BLOCK_TAGS = frozenset({"p", "div", "section", "article", "main", "tr", "hr", "dt", "dd"})
_HEADING_TAGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}


class _HTMLToMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.chunks: list[str] = []
        self._in_title = False
        self._skip_depth = 0
        self._in_pre = False
        self._in_blockquote = False
        self._link_href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "title":
            self._in_title = True
            return
        if t in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return

        attr_map = dict(attrs)
        if t in _HEADING_TAGS:
            self.chunks.append(f"\n\n{_HEADING_TAGS[t]} ")
        elif t == "br":
            self.chunks.append("\n")
        elif t == "li":
            self.chunks.append("\n- ")
        elif t == "blockquote":
            self._in_blockquote = True
            self.chunks.append("\n\n> ")
        elif t == "pre":
            self._in_pre = True
            self.chunks.append("\n\n```\n")
        elif t == "a":
            href = (attr_map.get("href") or "").strip()
            if href.startswith(("http://", "https://")):
                self._link_href = href
                self._link_text = []
        elif t in _BLOCK_TAGS:
            self.chunks.append("\n\n")
        elif t in ("td", "th"):
            self.chunks.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "title":
            self._in_title = False
            return
        if t in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth > 0:
            return

        if t in _HEADING_TAGS:
            self.chunks.append("\n\n")
        elif t == "blockquote":
            self._in_blockquote = False
            self.chunks.append("\n\n")
        elif t == "pre":
            self._in_pre = False
            self.chunks.append("\n```\n\n")
        elif t == "a" and self._link_href is not None:
            label = re.sub(r"\s+", " ", "".join(self._link_text)).strip()
            if label:
                self.chunks.append(f"[{label}]({self._link_href})")
            self._link_href = None
            self._link_text = []
        elif t in _BLOCK_TAGS or t in ("ul", "ol", "table"):
            self.chunks.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth > 0:
            return
        if self._link_href is not None:
            self._link_text.append(data)
            return
        if self._in_pre:
            self.chunks.append(data)
            return
        text = re.sub(r"[ \t\r\n]+", " ", data)
        if text:
            self.chunks.append(text)


def html_to_markdown(html_text: str) -> tuple[str, str]:
    """Convert raw HTML into (page_title, clean_markdown) using only Python stdlib."""
    parser = _HTMLToMarkdownParser()
    parser.feed(html_text)
    parser.close()

    title = html.unescape(re.sub(r"\s+", " ", "".join(parser.title_parts))).strip()
    raw_md = html.unescape("".join(parser.chunks))
    cleaned_lines = [line.strip() if not line.startswith("```") else line for line in raw_md.splitlines()]
    compact = "\n".join(cleaned_lines)
    cleaned = light_clean_text(compact, merge_hard_wraps=False)
    return normalize_nfc(title), cleaned


class HtmlAdapter(BaseAdapter):
    name = "html"
    supported_extensions = (".html", ".htm")

    def extract(self, file_path: Path) -> RawDocumentPayload:
        try:
            raw_bytes = file_path.read_bytes()
        except OSError as exc:
            raise KBError(ErrorCode.ADAPTER_ERROR, f"无法读取 HTML 文件 {file_path}: {exc}") from exc

        try:
            html_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            html_text = raw_bytes.decode("utf-8", errors="replace")

        page_title, cleaned_md = html_to_markdown(html_text)
        if not cleaned_md.strip():
            raise KBError(ErrorCode.ADAPTER_ERROR, f"HTML 文件解析后正文为空: {file_path.name}")

        suggested_title = sanitize_filename(page_title or file_path.stem, fallback="未命名网页")
        md_doc = (
            f"# {suggested_title}\n\n{cleaned_md}"
            if not cleaned_md.lstrip().startswith("# ")
            else cleaned_md
        )
        content_hash = sha256_text(md_doc)
        return RawDocumentPayload(
            suggested_title=suggested_title,
            media_type="html",
            content_hash=content_hash,
            markdown_content=md_doc,
            asset_files=[],
            needs_vision=False,
            metadata={"original_filename": normalize_nfc(file_path.name), "html_title": page_title},
        )
