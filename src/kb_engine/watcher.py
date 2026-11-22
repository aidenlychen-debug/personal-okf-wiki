"""Unified inbox/watch_folder scanner and single-instance lightweight daemon."""

from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import logging
import os
import shutil
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from kb_engine.adapters import AdapterRegistry, ImageAdapter, TextAdapter, build_default_registry
from kb_engine.adapters.image_adapter import render_image_stub
from kb_engine.config import KBConfig
from kb_engine.db import db_session, upsert_raw_document
from kb_engine.errors import ErrorCode, KBError
from kb_engine.utils import (
    atomic_write_bytes,
    atomic_write_text,
    local_today_str,
    normalize_nfc,
    normalize_rel_path,
    unique_target_path,
    write_lock,
)


def _setup_daemon_logger(root_dir: Path) -> logging.Logger:
    logger = logging.getLogger(f"kb_engine.watcher.{root_dir.resolve()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        log_path = root_dir / ".kb" / "daemon.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            str(log_path),
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


def should_ignore_path(path: Path, ignore_globs: list[str], *, root_boundary: Path | None = None) -> bool:
    """Check whether any path segment (relative to root_boundary) matches ignore_globs."""
    if root_boundary is not None:
        try:
            rel_parts = path.resolve().relative_to(root_boundary.resolve()).parts
        except ValueError:
            rel_parts = (path.name,)
    else:
        rel_parts = (path.name,)

    for part in rel_parts:
        norm_part = normalize_nfc(part)
        for pattern in ignore_globs:
            if fnmatch.fnmatch(norm_part, pattern):
                return True
    return False


def wait_file_stable(file_path: Path, debounce_seconds: float) -> bool:
    """Wait until file size is stable and readable across debounce_seconds."""
    if debounce_seconds <= 0:
        return file_path.exists() and file_path.is_file()
    try:
        prev_size = file_path.stat().st_size
    except OSError:
        return False
    time.sleep(debounce_seconds)
    try:
        if not file_path.exists() or not file_path.is_file():
            return False
        curr_size = file_path.stat().st_size
        if curr_size != prev_size:
            time.sleep(debounce_seconds)
            if not file_path.exists():
                return False
            curr_size = file_path.stat().st_size
        with file_path.open("rb") as fh:
            fh.read(1)
        return curr_size >= 0
    except OSError:
        return False


def _cleanup_inbox_file(inbox_dir: Path, file_path: Path, config: KBConfig) -> None:
    if not file_path.exists():
        return
    if config.inbox.cleanup_mode == "delete":
        with contextlib.suppress(OSError):
            file_path.unlink()
        return

    processed_dir = inbox_dir / ".processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    target = unique_target_path(processed_dir, file_path.stem, file_path.suffix)
    shutil.move(str(file_path), str(target))


def purge_expired_processed(inbox_dir: Path, retention_days: int) -> int:
    """Remove expired files inside inbox/.processed/ older than retention_days."""
    processed_dir = inbox_dir / ".processed"
    if not processed_dir.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=retention_days)
    cutoff_ts = cutoff.timestamp()
    removed = 0
    for child in processed_dir.iterdir():
        if not child.is_file():
            continue
        try:
            if child.stat().st_mtime < cutoff_ts:
                child.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _ingest_single_file(
    root_dir: Path,
    conn: Any,
    file_path: Path,
    *,
    source_origin: str,
    registry: AdapterRegistry,
) -> dict[str, Any]:
    """Extract, deduplicate, and atomically archive a single file into raw/."""
    adapter = registry.get_for_file(file_path)
    if adapter is None:
        return {"status": "unsupported", "path": normalize_nfc(str(file_path))}

    stat_info = file_path.stat()
    norm_origin_path = normalize_nfc(str(file_path.resolve()))
    origin_mtime = stat_info.st_mtime

    # Fast-path mtime check for read-only watch_folders
    if source_origin == "watch_folder":
        cached = conn.execute(
            "SELECT content_hash, raw_rel_path FROM raw_documents WHERE origin_path = ? AND origin_mtime = ?",
            (norm_origin_path, origin_mtime),
        ).fetchone()
        if cached:
            return {
                "status": "skipped_unchanged",
                "path": norm_origin_path,
                "content_hash": cached["content_hash"],
            }

    payload = adapter.extract(file_path)

    # Check content hash deduplication before writing any raw/ file
    existing = conn.execute(
        "SELECT * FROM raw_documents WHERE content_hash = ?",
        (payload.content_hash,),
    ).fetchone()
    if existing:
        # Update origin_path / origin_mtime (handles moved/renamed watch_folder files without duplicating)
        upsert_raw_document(
            conn,
            content_hash=payload.content_hash,
            source_origin=source_origin,
            origin_path=norm_origin_path,
            origin_mtime=origin_mtime,
            raw_rel_path=existing["raw_rel_path"],
            asset_rel_path=existing["asset_rel_path"],
            media_type=existing["media_type"],
            needs_vision=bool(existing["needs_vision"]),
            state=existing["state"],
        )
        return {
            "status": "duplicate",
            "path": norm_origin_path,
            "content_hash": payload.content_hash,
            "raw_rel_path": existing["raw_rel_path"],
        }

    today = local_today_str()
    raw_dir = root_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    stem_with_date = f"{today}-{payload.suggested_title}"

    asset_rel_path: str | None = None
    markdown_content = payload.markdown_content

    if payload.media_type == "image" and payload.asset_files:
        assets_dir = raw_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        src_img = payload.asset_files[0]
        asset_target = unique_target_path(assets_dir, stem_with_date, src_img.suffix.lower())
        atomic_write_bytes(asset_target, src_img.read_bytes())
        asset_rel_path = normalize_rel_path(asset_target.relative_to(root_dir))
        markdown_content = render_image_stub(
            title=payload.suggested_title,
            asset_rel_path=asset_rel_path,
            asset_sha256=payload.content_hash,
            captured_at=payload.metadata.get("captured_at", today),
            dimensions=payload.metadata.get("dimensions", "unknown"),
        )

    raw_target = unique_target_path(raw_dir, stem_with_date, ".md")
    atomic_write_text(raw_target, markdown_content)
    raw_rel_path = normalize_rel_path(raw_target.relative_to(root_dir))

    _, record = upsert_raw_document(
        conn,
        content_hash=payload.content_hash,
        source_origin=source_origin,
        origin_path=norm_origin_path,
        origin_mtime=origin_mtime,
        raw_rel_path=raw_rel_path,
        asset_rel_path=asset_rel_path,
        media_type=payload.media_type,
        needs_vision=payload.needs_vision,
        state="PENDING_COMPILE",
    )
    return {
        "status": "ingested",
        "path": norm_origin_path,
        "content_hash": record["content_hash"],
        "raw_rel_path": record["raw_rel_path"],
        "asset_rel_path": record["asset_rel_path"],
        "media_type": record["media_type"],
    }


def scan_once(
    root_dir: Path,
    config: KBConfig,
    *,
    apply_debounce: bool = False,
) -> dict[str, Any]:
    """Scan inbox/ and read-only watch_folders under global write lock."""
    registry = build_default_registry(config.adapters.enabled)
    inbox_dir = root_dir / config.inbox.path
    inbox_dir.mkdir(parents=True, exist_ok=True)

    ingested: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    skipped_unchanged = 0
    unsupported: list[str] = []
    errors: list[dict[str, str]] = []
    purged_processed = 0

    with write_lock(root_dir, config.safety.write_lock_timeout_seconds):
        purged_processed = purge_expired_processed(inbox_dir, config.inbox.archive_retention_days)

        with db_session(root_dir) as conn:
            # 1. Scan inbox/ (archive & cleanup after ingest)
            inbox_files = sorted(p for p in inbox_dir.rglob("*") if p.is_file())
            for file_path in inbox_files:
                if should_ignore_path(file_path, config.scanner.ignore_globs, root_boundary=inbox_dir):
                    continue
                if apply_debounce and not wait_file_stable(file_path, config.inbox.debounce_seconds):
                    continue
                try:
                    res = _ingest_single_file(
                        root_dir,
                        conn,
                        file_path,
                        source_origin="inbox",
                        registry=registry,
                    )
                except Exception as exc:
                    errors.append({"path": normalize_nfc(str(file_path)), "error": str(exc)})
                    continue

                status = res["status"]
                if status == "ingested":
                    ingested.append(res)
                    _cleanup_inbox_file(inbox_dir, file_path, config)
                elif status == "duplicate":
                    duplicates.append(res)
                    _cleanup_inbox_file(inbox_dir, file_path, config)
                elif status == "unsupported":
                    unsupported.append(res["path"])

            # 2. Scan external read-only watch_folders (strictly NEVER write/move/delete)
            for folder_str in config.watch_folders:
                folder = Path(folder_str).expanduser()
                if not folder.exists() or not folder.is_dir():
                    errors.append({"path": str(folder), "error": "外部监听目录不存在或不可读"})
                    continue
                for file_path in sorted(p for p in folder.rglob("*") if p.is_file()):
                    if should_ignore_path(file_path, config.scanner.ignore_globs, root_boundary=folder):
                        continue
                    try:
                        res = _ingest_single_file(
                            root_dir,
                            conn,
                            file_path,
                            source_origin="watch_folder",
                            registry=registry,
                        )
                    except Exception as exc:
                        errors.append({"path": normalize_nfc(str(file_path)), "error": str(exc)})
                        continue

                    status = res["status"]
                    if status == "ingested":
                        ingested.append(res)
                    elif status == "duplicate":
                        duplicates.append(res)
                    elif status == "skipped_unchanged":
                        skipped_unchanged += 1
                    elif status == "unsupported":
                        unsupported.append(res["path"])

    return {
        "ok": True,
        "ingested_count": len(ingested),
        "duplicate_count": len(duplicates),
        "skipped_unchanged_count": skipped_unchanged,
        "unsupported_count": len(unsupported),
        "purged_processed_count": purged_processed,
        "ingested": ingested,
        "duplicates": duplicates,
        "errors": errors,
    }


def add_direct_text(
    root_dir: Path,
    config: KBConfig,
    *,
    text: str,
    title: str | None = None,
) -> dict[str, Any]:
    """Write a quick thought / direct text note into raw/ and enqueue PENDING_COMPILE."""
    if not text or not text.strip():
        raise KBError(ErrorCode.INVALID_ARGUMENT, "--text 内容不能为空。")

    adapter = TextAdapter()
    payload = adapter.extract_direct_text(text, title=title)

    with write_lock(root_dir, config.safety.write_lock_timeout_seconds):
        with db_session(root_dir) as conn:
            existing = conn.execute(
                "SELECT * FROM raw_documents WHERE content_hash = ?",
                (payload.content_hash,),
            ).fetchone()
            if existing:
                return {
                    "ok": True,
                    "duplicate": True,
                    "content_hash": existing["content_hash"],
                    "raw_rel_path": existing["raw_rel_path"],
                    "state": existing["state"],
                }

            today = local_today_str()
            raw_dir = root_dir / "raw"
            raw_target = unique_target_path(raw_dir, f"{today}-{payload.suggested_title}", ".md")
            atomic_write_text(raw_target, payload.markdown_content)
            raw_rel_path = normalize_rel_path(raw_target.relative_to(root_dir))

            _, record = upsert_raw_document(
                conn,
                content_hash=payload.content_hash,
                source_origin="direct_text",
                origin_path=None,
                origin_mtime=None,
                raw_rel_path=raw_rel_path,
                asset_rel_path=None,
                media_type="text",
                needs_vision=False,
                state="PENDING_COMPILE",
            )
            return {
                "ok": True,
                "duplicate": False,
                "content_hash": record["content_hash"],
                "raw_rel_path": record["raw_rel_path"],
                "state": record["state"],
            }


def acquire_watcher_pid_lock(root_dir: Path) -> int:
    """Acquire non-blocking exclusive lock on .kb/watch.pid. Returns open file descriptor."""
    kb_dir = root_dir / ".kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    pid_path = kb_dir / "watch.pid"
    fd = os.open(str(pid_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            existing_pid = os.read(fd, 32).decode("utf-8", errors="ignore").strip()
        os.close(fd)
        raise KBError(
            ErrorCode.WATCHER_ALREADY_RUNNING,
            f"kb watch 守护进程已在运行中（PID: {existing_pid or 'unknown'}），禁止重复启动。",
            details={"pid_file": str(pid_path), "pid": existing_pid},
        )
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
    os.fsync(fd)
    return fd


def is_watcher_running(root_dir: Path) -> tuple[bool, int | None]:
    """Check whether .kb/watch.pid is currently locked by an active watcher."""
    pid_path = root_dir / ".kb" / "watch.pid"
    if not pid_path.exists():
        return False, None
    fd = os.open(str(pid_path), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Lock succeeded -> no live watcher holds it
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False, None
    except BlockingIOError:
        raw = os.read(fd, 32).decode("utf-8", errors="ignore").strip()
        pid = int(raw) if raw.isdigit() else None
        return True, pid
    finally:
        os.close(fd)


class _InboxEventHandler(FileSystemEventHandler):
    def __init__(self, trigger_event: threading.Event, inbox_dir: Path, ignore_globs: list[str]) -> None:
        super().__init__()
        self._trigger = trigger_event
        self._inbox_dir = inbox_dir
        self._ignore_globs = ignore_globs

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        src = Path(os.fsdecode(event.src_path))
        if should_ignore_path(src, self._ignore_globs, root_boundary=self._inbox_dir):
            return
        self._trigger.set()


def run_watcher(
    root_dir: Path,
    config: KBConfig,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """Run single-instance daemon watching inbox/ (debounced) & polling watch_folders."""
    pid_fd = acquire_watcher_pid_lock(root_dir)
    logger = _setup_daemon_logger(root_dir)
    inbox_dir = root_dir / config.inbox.path
    inbox_dir.mkdir(parents=True, exist_ok=True)

    internal_stop = stop_event or threading.Event()
    trigger_scan = threading.Event()

    def _handle_signal(_signum: int, _frame: Any) -> None:
        internal_stop.set()
        trigger_scan.set()

    prev_sigint = None
    prev_sigterm = None
    if threading.current_thread() is threading.main_thread():
        prev_sigint = signal.signal(signal.SIGINT, _handle_signal)
        prev_sigterm = signal.signal(signal.SIGTERM, _handle_signal)

    handler = _InboxEventHandler(trigger_scan, inbox_dir, config.scanner.ignore_globs)
    observer: Any = Observer()
    observer.schedule(handler, str(inbox_dir), recursive=True)
    try:
        observer.start()
        time.sleep(0.15)
        emitters_alive = all(e.is_alive() for e in observer.emitters) if observer.emitters else False
        if not observer.is_alive() or not emitters_alive:
            raise RuntimeError("native observer emitter failed to start")
        observer_mode = type(observer).__name__
    except Exception as exc:
        logger.warning("native observer unavailable (%s); falling back to PollingObserver", exc)
        with contextlib.suppress(Exception):
            observer.stop()
            observer.join(timeout=1.0)
        observer = PollingObserver(timeout=0.5)
        observer.schedule(handler, str(inbox_dir), recursive=True)
        observer.start()
        observer_mode = "PollingObserver"
    logger.info("kb watch started (pid=%s, mode=%s, inbox=%s)", os.getpid(), observer_mode, inbox_dir)

    try:
        # Initial scan on startup
        try:
            res = scan_once(root_dir, config, apply_debounce=False)
            logger.info("initial scan complete: ingested=%d dup=%d", res["ingested_count"], res["duplicate_count"])
        except Exception as exc:
            logger.error("initial scan failed: %s", exc)

        poll_interval = config.scanner.poll_interval_seconds
        while not internal_stop.is_set():
            triggered = trigger_scan.wait(timeout=poll_interval)
            if internal_stop.is_set():
                break
            if triggered:
                trigger_scan.clear()
                try:
                    res = scan_once(root_dir, config, apply_debounce=True)
                    if res["ingested_count"] or res["duplicate_count"] or res["errors"]:
                        logger.info(
                            "inbox event scan: ingested=%d dup=%d errors=%d",
                            res["ingested_count"],
                            res["duplicate_count"],
                            len(res["errors"]),
                        )
                except Exception as exc:
                    logger.error("inbox event scan error: %s", exc)
            else:
                try:
                    res = scan_once(root_dir, config, apply_debounce=False)
                    if res["ingested_count"] or res["errors"]:
                        logger.info(
                            "periodic scan: ingested=%d errors=%d",
                            res["ingested_count"],
                            len(res["errors"]),
                        )
                except Exception as exc:
                    logger.error("periodic scan error: %s", exc)
    finally:
        observer.stop()
        observer.join(timeout=5.0)
        logger.info("kb watch stopped")
        if threading.current_thread() is threading.main_thread():
            if prev_sigint is not None:
                signal.signal(signal.SIGINT, prev_sigint)
            if prev_sigterm is not None:
                signal.signal(signal.SIGTERM, prev_sigterm)
        with contextlib.suppress(OSError):
            fcntl.flock(pid_fd, fcntl.LOCK_UN)
            os.close(pid_fd)
