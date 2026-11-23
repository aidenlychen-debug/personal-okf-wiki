"""Zero-pollution light text cleaner for raw/ archiving (no semantic alteration)."""

from __future__ import annotations

import re

from kb_engine.utils import normalize_newlines, normalize_nfc

# Video / podcast timestamps on standalone lines or line prefixes
_STANDALONE_TS_RE = re.compile(
    r"^\s*(?:\[?\d{1,2}:\d{2}(?::\d{2})?\]?|\d+\s*分钟\s*\d+\s*秒钟?|\d+\s*小时\s*\d+\s*分钟(?:\s*\d+\s*秒钟?)?)\s*$"
)
_INLINE_PREFIX_TS_RE = re.compile(
    r"^\s*(?:\[\d{1,2}:\d{2}(?::\d{2})?\]|\d{1,2}:\d{2}(?::\d{2})?)\s+(?=\S)"
)
_AUDIO_TAG_RE = re.compile(
    r"\[(?:Music|Laughter|Applause|Cheering|Inaudible|Silence|音乐|笑声|掌声)\]",
    re.IGNORECASE,
)

# Isolated PDF page numbers & common e-book download watermarks
_PDF_PAGE_NUM_RE = re.compile(
    r"^\s*(?:[-—–·•]\s*\d{1,4}\s*[-—–·•]|第\s*\d{1,4}\s*页(?:\s*/\s*共\s*\d+\s*页)?|Page\s+\d{1,4}(?:\s+of\s+\d+)?|\d{1,4})\s*$",
    re.IGNORECASE,
)
_WATERMARK_RE = re.compile(
    r"(?:z-library\.sk|1lib\.sk|z-lib\.sk|singlelogin\.re|oceanofpdf\.com|ebook-hunter\.org|annas-archive\.org)",
    re.IGNORECASE,
)

# Markdown structural line markers that must NEVER be merged with adjacent lines
_MD_BLOCK_START_RE = re.compile(
    r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s?|```|~~~|\||\-\-\-$|\*\*\*$)"
)
_SENTENCE_END_RE = re.compile(r"[。！？!?；;：:…\"'”’）)】\]]$")
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")


def _split_frontmatter_prefix(text: str) -> tuple[str, str]:
    """Preserve leading YAML frontmatter verbatim so metadata is never altered."""
    if text.startswith("---\n"):
        end_idx = text.find("\n---\n", 4)
        if end_idx != -1:
            return text[: end_idx + 5], text[end_idx + 5 :]
        if text.endswith("\n---"):
            return text, ""
    return "", text


def _join_wrapped_lines(prev: str, nxt: str) -> str:
    """Join two broken wrapped lines with smart CJK vs Latin spacing."""
    prev_s = prev.rstrip()
    nxt_s = nxt.lstrip()
    if not prev_s:
        return nxt_s
    if not nxt_s:
        return prev_s
    # Hyphenated English line-break ("informa-\ntion" -> "information")
    if prev_s.endswith("-") and len(prev_s) >= 2 and prev_s[-2].isalpha() and nxt_s[0].isalpha():
        return prev_s[:-1] + nxt_s
    # No space between adjacent CJK characters
    if _CJK_CHAR_RE.search(prev_s[-1]) or _CJK_CHAR_RE.search(nxt_s[0]):
        return prev_s + nxt_s
    return f"{prev_s} {nxt_s}"


def light_clean_text(text: str, *, merge_hard_wraps: bool = True) -> str:
    """Apply zero-pollution light cleaning: strip timestamps/page-nums/watermarks & fix broken wraps.

    Strictly preserves original wording and Markdown structure (frontmatter, headings, lists,
    blockquotes, tables, code fences).
    """
    norm = normalize_newlines(normalize_nfc(text))
    fm_prefix, body = _split_frontmatter_prefix(norm)

    in_code_fence = False
    cleaned_lines: list[str] = []

    for raw_line in body.splitlines():
        stripped = raw_line.strip()

        # Preserve fenced code blocks verbatim
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_fence = not in_code_fence
            cleaned_lines.append(raw_line)
            continue
        if in_code_fence:
            cleaned_lines.append(raw_line)
            continue

        # Preserve blank lines (collapsed later to max 2)
        if not stripped:
            cleaned_lines.append("")
            continue

        # Drop standalone timestamps, isolated PDF page numbers & e-book watermarks
        if _STANDALONE_TS_RE.match(stripped):
            continue
        if _PDF_PAGE_NUM_RE.match(stripped):
            continue
        if _WATERMARK_RE.search(stripped) and len(stripped) < 160:
            continue

        # Strip inline prefix timestamps & [Music]/[Laughter] audio tags
        line = _INLINE_PREFIX_TS_RE.sub("", raw_line)
        line = _AUDIO_TAG_RE.sub("", line).rstrip()
        if not line.strip():
            continue
        cleaned_lines.append(line)

    # Optionally merge broken hard-wraps inside plain prose paragraphs
    if merge_hard_wraps:
        merged_lines: list[str] = []
        in_code = False
        for line in cleaned_lines:
            s = line.strip()
            if s.startswith("```") or s.startswith("~~~"):
                in_code = not in_code
                merged_lines.append(line)
                continue
            if in_code or not s:
                merged_lines.append(line)
                continue

            if (
                merged_lines
                and merged_lines[-1].strip()
                and not _MD_BLOCK_START_RE.match(s)
                and not _MD_BLOCK_START_RE.match(merged_lines[-1].strip())
                and not _SENTENCE_END_RE.search(merged_lines[-1].strip())
            ):
                merged_lines[-1] = _join_wrapped_lines(merged_lines[-1], s)
            else:
                merged_lines.append(line)
        cleaned_lines = merged_lines

    # Collapse 3+ consecutive blank lines into a single blank line
    final_body = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned_lines)).strip()
    if fm_prefix:
        return f"{fm_prefix}\n{final_body}\n" if final_body else fm_prefix
    return f"{final_body}\n" if final_body else ""
