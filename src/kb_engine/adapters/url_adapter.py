"""Webpage URL & bookmark adapter (.webloc, .url, .link, and direct URL fetch)."""

from __future__ import annotations

import plistlib
import re
import urllib.error
import urllib.request
from pathlib import Path

from kb_engine.adapters.base import BaseAdapter, RawDocumentPayload
from kb_engine.adapters.html_adapter import html_to_markdown
from kb_engine.errors import ErrorCode, KBError
from kb_engine.utils import local_today_str, normalize_nfc, sanitize_filename, sha256_text

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _parse_url_from_file(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    raw_bytes = file_path.read_bytes()

    if ext == ".webloc":
        try:
            data = plistlib.loads(raw_bytes)
            if isinstance(data, dict) and data.get("URL"):
                return str(data["URL"]).strip()
        except Exception:
            pass

    text = raw_bytes.decode("utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("url="):
            return stripped[4:].strip()
    m = _URL_RE.search(text)
    if m:
        return m.group(0).strip()
    raise KBError(ErrorCode.ADAPTER_ERROR, f"无法从书签文件中解析有效网页链接: {file_path.name}")


def _fetch_html(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw_bytes = resp.read()
            return raw_bytes.decode(charset, errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise KBError(
            ErrorCode.ADAPTER_ERROR,
            f"抓取网页失败 ({url}): {exc}",
            details={"url": url},
        ) from exc


class UrlAdapter(BaseAdapter):
    name = "url"
    supported_extensions = (".webloc", ".url", ".link")

    def extract_url(self, url: str, *, title: str | None = None) -> RawDocumentPayload:
        clean_url = url.strip()
        if not clean_url.startswith(("http://", "https://")):
            raise KBError(ErrorCode.INVALID_ARGUMENT, f"无效的网页链接（需以 http:// 或 https:// 开头）: {url}")

        html_text = _fetch_html(clean_url)
        page_title, cleaned_md = html_to_markdown(html_text)
        if not cleaned_md.strip():
            raise KBError(ErrorCode.ADAPTER_ERROR, f"网页正文提取为空（可能为纯 JS 动态渲染页）: {clean_url}")

        suggested_title = sanitize_filename(title or page_title or "网页摘录", fallback="网页摘录")
        fetched_at = local_today_str()
        md_doc = (
            "---\n"
            f'source_url: "{clean_url}"\n'
            f'fetched_at: "{fetched_at}"\n'
            "---\n\n"
            f"# {suggested_title}\n\n"
            f"{cleaned_md}"
        )
        content_hash = sha256_text(md_doc)
        return RawDocumentPayload(
            suggested_title=suggested_title,
            media_type="url",
            content_hash=content_hash,
            markdown_content=md_doc,
            asset_files=[],
            needs_vision=False,
            metadata={"source_url": clean_url, "fetched_at": fetched_at},
        )

    def extract(self, file_path: Path) -> RawDocumentPayload:
        url = _parse_url_from_file(file_path)
        fallback_title = sanitize_filename(file_path.stem, fallback="") or None
        payload = self.extract_url(url, title=fallback_title)
        payload.metadata["original_filename"] = normalize_nfc(file_path.name)
        return payload
