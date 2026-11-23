"""Zero-dependency Office & Ebook adapter (.docx, .pptx, .epub, .csv) via Python stdlib."""

from __future__ import annotations

import csv
import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from kb_engine.adapters.base import BaseAdapter, RawDocumentPayload
from kb_engine.adapters.cleaner import light_clean_text
from kb_engine.adapters.html_adapter import html_to_markdown
from kb_engine.errors import ErrorCode, KBError
from kb_engine.utils import normalize_nfc, sanitize_filename, sha256_text

_SLIDE_NUM_RE = re.compile(r"slide(\d+)\.xml$", re.IGNORECASE)


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _extract_docx(file_path: Path) -> str:
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "word/document.xml" not in zf.namelist():
                raise KBError(ErrorCode.ADAPTER_ERROR, f"无效的 DOCX 文件（缺少 word/document.xml）: {file_path.name}")
            xml_bytes = zf.read("word/document.xml")
    except zipfile.BadZipFile as exc:
        raise KBError(ErrorCode.ADAPTER_ERROR, f"DOCX 文件损坏: {file_path.name}") from exc

    root = ET.fromstring(xml_bytes)
    lines: list[str] = []
    for elem in root.iter():
        if _local_tag(elem.tag) != "p":
            continue
        style_val = ""
        for child in elem.iter():
            if _local_tag(child.tag) == "pStyle":
                for k, v in child.attrib.items():
                    if _local_tag(k) == "val":
                        style_val = v
                        break
        texts = [node.text for node in elem.iter() if _local_tag(node.tag) == "t" and node.text]
        para = "".join(texts).strip()
        if not para:
            continue
        if style_val.lower().startswith("heading"):
            digits = "".join(ch for ch in style_val if ch.isdigit())
            level = min(6, max(1, int(digits))) if digits else 2
            lines.append(f"{'#' * level} {para}")
        else:
            lines.append(para)
    return "\n\n".join(lines)


def _extract_pptx(file_path: Path) -> str:
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            slide_entries: list[tuple[int, str]] = []
            for name in zf.namelist():
                if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
                    m = _SLIDE_NUM_RE.search(name)
                    num = int(m.group(1)) if m else 9999
                    slide_entries.append((num, name))
            slide_entries.sort(key=lambda x: x[0])

            sections: list[str] = []
            for num, name in slide_entries:
                root = ET.fromstring(zf.read(name))
                slide_lines: list[str] = []
                for p in root.iter():
                    if _local_tag(p.tag) != "p":
                        continue
                    texts = [t.text for t in p.iter() if _local_tag(t.tag) == "t" and t.text]
                    line = "".join(texts).strip()
                    if line:
                        slide_lines.append(line)
                if slide_lines:
                    sections.append(f"## Slide {num}\n\n" + "\n".join(slide_lines))
    except zipfile.BadZipFile as exc:
        raise KBError(ErrorCode.ADAPTER_ERROR, f"PPTX 文件损坏: {file_path.name}") from exc

    return "\n\n".join(sections)


def _extract_epub(file_path: Path) -> tuple[str, str]:
    book_title = ""
    chapters: list[str] = []
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            namelist = set(zf.namelist())
            opf_path: str | None = None
            if "META-INF/container.xml" in namelist:
                c_root = ET.fromstring(zf.read("META-INF/container.xml"))
                for elem in c_root.iter():
                    if _local_tag(elem.tag) == "rootfile" and elem.attrib.get("full-path"):
                        opf_path = elem.attrib["full-path"]
                        break

            html_paths: list[str] = []
            if opf_path and opf_path in namelist:
                opf_dir = posixpath.dirname(opf_path)
                opf_root = ET.fromstring(zf.read(opf_path))
                manifest: dict[str, str] = {}
                for elem in opf_root.iter():
                    lt = _local_tag(elem.tag)
                    if lt == "title" and elem.text and not book_title:
                        book_title = elem.text.strip()
                    elif lt == "item" and elem.attrib.get("id") and elem.attrib.get("href"):
                        manifest[elem.attrib["id"]] = elem.attrib["href"]
                for elem in opf_root.iter():
                    if _local_tag(elem.tag) == "itemref":
                        idref = elem.attrib.get("idref")
                        href = manifest.get(idref or "")
                        if href:
                            joined = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
                            if joined in namelist:
                                html_paths.append(joined)

            if not html_paths:
                html_paths = sorted(
                    n for n in namelist if n.lower().endswith((".xhtml", ".html", ".htm"))
                )

            for hp in html_paths:
                raw_html = zf.read(hp).decode("utf-8", errors="replace")
                _t, md = html_to_markdown(raw_html)
                if md.strip():
                    chapters.append(md.strip())
    except zipfile.BadZipFile as exc:
        raise KBError(ErrorCode.ADAPTER_ERROR, f"EPUB 文件损坏: {file_path.name}") from exc

    return book_title, "\n\n".join(chapters)


def _extract_csv(file_path: Path) -> str:
    raw_text = file_path.read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(raw_text)))
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        return ""
    header = [c.strip().replace("|", "\\|") for c in rows[0]]
    md_lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * len(header)) + " |",
    ]
    for r in rows[1:]:
        padded = [r[i].strip().replace("|", "\\|") if i < len(r) else "" for i in range(len(header))]
        md_lines.append("| " + " | ".join(padded) + " |")
    return "\n".join(md_lines)


class OfficeAdapter(BaseAdapter):
    name = "office"
    supported_extensions = (".docx", ".pptx", ".epub", ".csv")

    def extract(self, file_path: Path) -> RawDocumentPayload:
        ext = file_path.suffix.lower()
        extracted_title = ""
        if ext == ".docx":
            raw_md = _extract_docx(file_path)
            cleaned = light_clean_text(raw_md, merge_hard_wraps=False)
        elif ext == ".pptx":
            raw_md = _extract_pptx(file_path)
            cleaned = light_clean_text(raw_md, merge_hard_wraps=False)
        elif ext == ".epub":
            extracted_title, raw_md = _extract_epub(file_path)
            cleaned = light_clean_text(raw_md, merge_hard_wraps=False)
        elif ext == ".csv":
            cleaned = _extract_csv(file_path)
        else:
            raise KBError(ErrorCode.ADAPTER_ERROR, f"不支持的办公/电子书格式: {ext}")

        if not cleaned.strip():
            raise KBError(ErrorCode.ADAPTER_ERROR, f"文件解析后正文为空: {file_path.name}")

        suggested_title = sanitize_filename(extracted_title or file_path.stem, fallback="未命名文档")
        md_doc = (
            f"# {suggested_title}\n\n{cleaned}"
            if not cleaned.lstrip().startswith("# ")
            else cleaned
        )
        content_hash = sha256_text(md_doc)
        return RawDocumentPayload(
            suggested_title=suggested_title,
            media_type=ext.lstrip("."),
            content_hash=content_hash,
            markdown_content=md_doc,
            asset_files=[],
            needs_vision=False,
            metadata={"original_filename": normalize_nfc(file_path.name)},
        )
