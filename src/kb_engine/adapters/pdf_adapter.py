"""Native macOS zero-dependency PDF adapter (Swift PDFKit + pdftotext fallback + light cleaner)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from kb_engine.adapters.base import BaseAdapter, RawDocumentPayload
from kb_engine.adapters.cleaner import light_clean_text
from kb_engine.errors import ErrorCode, KBError
from kb_engine.utils import normalize_nfc, sanitize_filename, sha256_text

_SWIFT_PDFKIT_SCRIPT = r"""
import Foundation
import PDFKit

let args = CommandLine.arguments
guard args.count >= 2 else {
    fputs("missing pdf path\n", stderr)
    exit(2)
}
let url = URL(fileURLWithPath: args[1])
guard let doc = PDFDocument(url: url) else {
    fputs("failed to open pdf\n", stderr)
    exit(3)
}
var out = ""
for i in 0..<doc.pageCount {
    if let page = doc.page(at: i), let txt = page.string {
        out += txt
        out += "\n\n"
    }
}
FileHandle.standardOutput.write(out.data(using: .utf8) ?? Data())
"""


def _extract_pdf_text(file_path: Path) -> str:
    """Extract plain text from PDF using macOS PDFKit (swift) or pdftotext fallback."""
    if shutil.which("swift"):
        proc = subprocess.run(
            ["swift", "-e", _SWIFT_PDFKIT_SCRIPT, str(file_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout

    if shutil.which("pdftotext"):
        proc = subprocess.run(
            ["pdftotext", str(file_path), "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout

    raise KBError(
        ErrorCode.ADAPTER_ERROR,
        f"无法从 PDF 提取文本（可能为纯扫描图片 PDF 或缺少 swift/pdftotext）: {file_path.name}",
        details={"file": str(file_path)},
    )


class PdfAdapter(BaseAdapter):
    name = "pdf"
    supported_extensions = (".pdf",)

    def extract(self, file_path: Path) -> RawDocumentPayload:
        if not file_path.exists():
            raise KBError(ErrorCode.FILE_NOT_FOUND, f"PDF 文件不存在: {file_path}")

        raw_text = _extract_pdf_text(file_path)
        cleaned = light_clean_text(raw_text, merge_hard_wraps=True)
        if not cleaned.strip():
            raise KBError(
                ErrorCode.ADAPTER_ERROR,
                f"PDF 提取清洗后正文为空（可能为纯图片扫描件）: {file_path.name}",
            )

        stem_title = sanitize_filename(file_path.stem, fallback="未命名PDF")
        md_doc = f"# {stem_title}\n\n{cleaned}" if not cleaned.lstrip().startswith("# ") else cleaned
        content_hash = sha256_text(md_doc)

        return RawDocumentPayload(
            suggested_title=stem_title,
            media_type="pdf",
            content_hash=content_hash,
            markdown_content=md_doc,
            asset_files=[],
            needs_vision=False,
            metadata={"original_filename": normalize_nfc(file_path.name)},
        )
