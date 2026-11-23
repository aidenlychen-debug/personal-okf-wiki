"""Pluggable cloud Audio/Video transcription adapter (.mp3/.m4a/.wav/.mp4/.mov etc.).

Never runs unstable local heavy models and never bloats Git with raw video binaries:
only the cleaned Markdown transcript is archived into raw/.
"""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from kb_engine.adapters.base import BaseAdapter, RawDocumentPayload
from kb_engine.adapters.cleaner import light_clean_text
from kb_engine.config import TranscriptionConfig
from kb_engine.errors import ErrorCode, KBError
from kb_engine.utils import local_today_str, normalize_nfc, sanitize_filename, sha256_text

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm")


def _extract_audio_with_ffmpeg(video_path: Path, out_mp3: Path) -> None:
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise KBError(
            ErrorCode.ADAPTER_ERROR,
            f"处理视频文件 {video_path.name} 需要安装 ffmpeg（请运行 `brew install ffmpeg` 后重试）。",
            details={"missing_tool": "ffmpeg", "file": video_path.name},
        )
    proc = subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "48k",
            str(out_mp3),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0 or not out_mp3.exists():
        raise KBError(
            ErrorCode.ADAPTER_ERROR,
            f"ffmpeg 提取音轨失败 ({video_path.name}): {(proc.stderr or '')[-300:]}",
        )


def _build_multipart_body(
    fields: dict[str, str],
    file_field: str,
    file_path: Path,
) -> tuple[bytes, str]:
    boundary = f"----KBMediaBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, val in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        chunks.append(f"{val}\r\n".encode("utf-8"))

    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    chunks.append(f"--{boundary}\r\n".encode("utf-8"))
    chunks.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode("utf-8")
    )
    chunks.append(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _transcribe_via_cloud_api(audio_path: Path, cfg: TranscriptionConfig) -> str:
    if not cfg.enabled:
        raise KBError(
            ErrorCode.ADAPTER_ERROR,
            f"音视频云端转写默认关闭，无法解码 {audio_path.name}。请在 config.yaml 设置 transcription.enabled: true 并配置环境变量 {cfg.api_key_env}。",
            details={"api_key_env": cfg.api_key_env},
        )
    api_key = os.environ.get(cfg.api_key_env, "").strip()
    if not api_key:
        raise KBError(
            ErrorCode.ADAPTER_ERROR,
            f"缺少音视频转写 API Key 环境变量 {cfg.api_key_env}，请配置后重新运行 kb scan。",
            details={"api_key_env": cfg.api_key_env},
        )

    fields: dict[str, str] = {"model": cfg.model}
    if cfg.language:
        fields["language"] = cfg.language

    body, content_type = _build_multipart_body(fields, "file", audio_path)
    req = urllib.request.Request(
        cfg.base_url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": content_type,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data: Any = json.loads(raw)
            if isinstance(data, dict) and data.get("text"):
                return str(data["text"]).strip()
            raise KBError(ErrorCode.ADAPTER_ERROR, f"云端转写返回缺少 text 字段: {raw[:200]}")
    except KBError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise KBError(ErrorCode.ADAPTER_ERROR, f"云端音视频转写请求失败: {exc}") from exc


class MediaAdapter(BaseAdapter):
    name = "media"
    supported_extensions = AUDIO_EXTENSIONS + VIDEO_EXTENSIONS

    def __init__(self, config: TranscriptionConfig | None = None) -> None:
        self.config = config or TranscriptionConfig()

    def extract(self, file_path: Path) -> RawDocumentPayload:
        ext = file_path.suffix.lower()
        is_video = ext in VIDEO_EXTENSIONS
        if is_video:
            with tempfile.TemporaryDirectory(prefix="kb_media_") as tmp_dir:
                tmp_mp3 = Path(tmp_dir) / f"{file_path.stem}.mp3"
                _extract_audio_with_ffmpeg(file_path, tmp_mp3)
                transcript = _transcribe_via_cloud_api(tmp_mp3, self.config)
        else:
            transcript = _transcribe_via_cloud_api(file_path, self.config)

        cleaned = light_clean_text(transcript, merge_hard_wraps=True)
        if not cleaned.strip():
            raise KBError(ErrorCode.ADAPTER_ERROR, f"音视频转写结果为空: {file_path.name}")

        suggested_title = sanitize_filename(file_path.stem, fallback="未命名音视频")
        transcribed_at = local_today_str()
        md_doc = (
            "---\n"
            f'source_media: "{normalize_nfc(file_path.name)}"\n'
            f'media_kind: "{"video" if is_video else "audio"}"\n'
            f'transcribed_at: "{transcribed_at}"\n'
            f'transcription_model: "{self.config.model}"\n'
            "---\n\n"
            f"# {suggested_title}\n\n"
            f"{cleaned}"
        )
        content_hash = sha256_text(md_doc)
        return RawDocumentPayload(
            suggested_title=suggested_title,
            media_type="video" if is_video else "audio",
            content_hash=content_hash,
            markdown_content=md_doc,
            asset_files=[],
            needs_vision=False,
            metadata={"original_filename": normalize_nfc(file_path.name)},
        )
