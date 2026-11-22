"""Image input adapter (.png, .jpg, .jpeg, .webp) with self-describing stubs."""

from __future__ import annotations

import struct
from pathlib import Path

from kb_engine.adapters.base import BaseAdapter, RawDocumentPayload
from kb_engine.errors import ErrorCode, KBError
from kb_engine.utils import local_today_str, normalize_nfc, sanitize_filename, sha256_bytes


def _detect_image_dimensions(data: bytes) -> str:
    """Detect PNG/JPEG/WEBP image dimensions without external imaging libraries."""
    # PNG: 8-byte signature + IHDR chunk (width, height at bytes 16..24)
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        return f"{width}x{height}"

    # WEBP: RIFF....WEBP
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            w = 1 + int.from_bytes(data[24:27], "little")
            h = 1 + int.from_bytes(data[27:30], "little")
            return f"{w}x{h}"
        if chunk == b"VP8 " and len(data) >= 30:
            w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return f"{w}x{h}"
        if chunk == b"VP8L" and len(data) >= 25:
            bits = struct.unpack("<I", data[21:25])[0]
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            return f"{w}x{h}"

    # JPEG: scan SOF0/SOF1/SOF2 markers
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            i += 2
            if marker in (0xD8, 0xD9, 0x01) or (0xD0 <= marker <= 0xD7):
                continue
            if i + 2 > len(data):
                break
            seg_len = struct.unpack(">H", data[i : i + 2])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3) and i + 7 <= len(data):
                height, width = struct.unpack(">HH", data[i + 3 : i + 7])
                return f"{width}x{height}"
            if seg_len < 2:
                break
            i += seg_len

    return "unknown"


def render_image_stub(
    *,
    title: str,
    asset_rel_path: str,
    asset_sha256: str,
    captured_at: str,
    dimensions: str,
) -> str:
    """Render self-describing image stub Markdown per PRD Section 4.3."""
    asset_filename = Path(asset_rel_path).name
    return (
        "---\n"
        "raw_type: image_stub\n"
        "media_type: image\n"
        "needs_vision: true\n"
        f'asset_rel_path: "{asset_rel_path}"\n'
        f'asset_sha256: "{asset_sha256}"\n'
        f'captured_at: "{captured_at}"\n'
        f'dimensions: "{dimensions}"\n'
        "---\n\n"
        f"# {title}\n\n"
        f"![{title}](./assets/{asset_filename})\n"
    )


class ImageAdapter(BaseAdapter):
    name = "image"
    supported_extensions = (".png", ".jpg", ".jpeg", ".webp")

    def extract(self, file_path: Path) -> RawDocumentPayload:
        try:
            raw_bytes = file_path.read_bytes()
        except OSError as exc:
            raise KBError(
                ErrorCode.ADAPTER_ERROR,
                f"无法读取图片文件 {file_path}: {exc}",
            ) from exc

        binary_hash = sha256_bytes(raw_bytes)
        dimensions = _detect_image_dimensions(raw_bytes)
        suggested_title = sanitize_filename(file_path.stem, fallback="未命名图片")
        captured_at = local_today_str()
        placeholder_asset_rel = f"raw/assets/{captured_at}-{suggested_title}{file_path.suffix.lower()}"
        stub_md = render_image_stub(
            title=suggested_title,
            asset_rel_path=placeholder_asset_rel,
            asset_sha256=binary_hash,
            captured_at=captured_at,
            dimensions=dimensions,
        )
        return RawDocumentPayload(
            suggested_title=suggested_title,
            media_type="image",
            content_hash=binary_hash,
            markdown_content=stub_md,
            asset_files=[file_path],
            needs_vision=True,
            metadata={
                "dimensions": dimensions,
                "captured_at": captured_at,
                "original_filename": normalize_nfc(file_path.name),
                "asset_suffix": file_path.suffix.lower(),
            },
        )
