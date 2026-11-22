"""Gate 1 tests: NFC/URL safety, filename sanitization, init idempotency, watcher & write lock."""

from __future__ import annotations

import concurrent.futures
import os
import struct
import threading
import time
import unicodedata
import zlib
from pathlib import Path

import pytest

from kb_engine.cli import cmd_init
from kb_engine.config import KBConfig, load_config
from kb_engine.db import connect_db
from kb_engine.errors import ErrorCode, KBError
from kb_engine.indexer import reindex_wiki
from kb_engine.utils import (
    classify_and_resolve_link,
    normalize_nfc,
    sanitize_filename,
    unique_target_path,
    write_lock,
)
from kb_engine.watcher import (
    acquire_watcher_pid_lock,
    is_watcher_running,
    scan_once,
    should_ignore_path,
)


def make_test_png(width: int = 32, height: int = 16) -> bytes:
    """Create a minimal valid PNG binary payload without Pillow."""
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_scanlines = b"".join(b"\x00" + b"\x11\x22\x33" * width for _ in range(height))
    idat = zlib.compress(raw_scanlines)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def test_nfc_url_decode_anchor_and_sanitize(tmp_path: Path) -> None:
    cmd_init(tmp_path)
    # macOS NFD decomposed Chinese/accented characters
    nfd_name = unicodedata.normalize("NFD", "café-知识复利.md")
    nfc_name = normalize_nfc(nfd_name)
    target_file = tmp_path / "wiki" / "concepts" / nfc_name
    target_file.write_text("# test\n", encoding="utf-8")

    src_file = tmp_path / "wiki" / "cases" / "案例.md"
    # Percent-encoded + anchor
    encoded = "../concepts/caf%C3%A9-%E7%9F%A5%E8%AF%86%E5%A4%8D%E5%88%A9.md#核心要义"
    resolved = classify_and_resolve_link(src_file, encoded, False, tmp_path)
    assert resolved.kind == "wiki"
    assert resolved.anchor == "#核心要义"
    assert resolved.target_rel_path == f"concepts/{nfc_name}"
    assert resolved.target_abs_path is not None and resolved.target_abs_path.exists()

    # External & pure anchor ignored
    ext = classify_and_resolve_link(src_file, "https://example.com/a?b=1", False, tmp_path)
    assert ext.kind == "external"
    anc = classify_and_resolve_link(src_file, "#局部标题", False, tmp_path)
    assert anc.kind == "anchor"

    # Filename sanitization
    dirty = 'A/B\\C:D*E?F"G|H(I)[J]#K   L'
    assert sanitize_filename(dirty) == "A B C D E F G H I J K L"

    # Immutable raw/ collision increment (-2, -3)
    raw_dir = tmp_path / "raw"
    p1 = unique_target_path(raw_dir, "2026-11-21-笔记", ".md")
    p1.write_text("1", encoding="utf-8")
    p2 = unique_target_path(raw_dir, "2026-11-21-笔记", ".md")
    p2.write_text("2", encoding="utf-8")
    p3 = unique_target_path(raw_dir, "2026-11-21-笔记", ".md")
    assert p1.name == "2026-11-21-笔记.md"
    assert p2.name == "2026-11-21-笔记-2.md"
    assert p3.name == "2026-11-21-笔记-3.md"


def test_init_idempotent_and_git_identity_fallback(tmp_path: Path) -> None:
    res1 = cmd_init(tmp_path)
    assert res1["ok"] is True
    assert res1["head_sha"] is not None

    # Running init a second time must be idempotent and keep HEAD intact
    res2 = cmd_init(tmp_path)
    assert res2["ok"] is True
    assert res2["head_sha"] == res1["head_sha"]
    assert res2["initial_commit_created"] is False


def test_watcher_single_instance_and_ignore_globs(tmp_path: Path) -> None:
    cmd_init(tmp_path)
    config = load_config(tmp_path)

    # Ignore globs filter junk/temp files
    inbox = tmp_path / "inbox"
    assert should_ignore_path(inbox / ".DS_Store", config.scanner.ignore_globs, root_boundary=inbox)
    assert should_ignore_path(inbox / "draft.tmp", config.scanner.ignore_globs, root_boundary=inbox)
    assert should_ignore_path(inbox / "~$word.docx", config.scanner.ignore_globs, root_boundary=inbox)
    assert not should_ignore_path(inbox / "正常笔记.md", config.scanner.ignore_globs, root_boundary=inbox)

    # Single-instance watch.pid lock
    fd = acquire_watcher_pid_lock(tmp_path)
    try:
        running, pid = is_watcher_running(tmp_path)
        assert running is True
        assert pid == os.getpid()

        # Second lock attempt in another file descriptor (or subprocess) must fail
        # Note: fcntl.flock on macOS is per open file description, so opening again blocks/fails!
        with pytest.raises(KBError) as exc_info:
            acquire_watcher_pid_lock(tmp_path)
        assert exc_info.value.error_code == ErrorCode.WATCHER_ALREADY_RUNNING.value
    finally:
        os.close(fd)


def test_readonly_watch_folder_rename_no_duplicate(tmp_path: Path) -> None:
    kb_root = tmp_path / "kb"
    ext_dir = tmp_path / "external_clippings"
    ext_dir.mkdir()
    cmd_init(kb_root)

    cfg_path = kb_root / "config.yaml"
    cfg_text = cfg_path.read_text(encoding="utf-8").replace(
        "watch_folders: []",
        f'watch_folders:\n  - "{ext_dir.as_posix()}"',
    )
    cfg_path.write_text(cfg_text, encoding="utf-8")
    config = load_config(kb_root)

    clip = ext_dir / "clipping-1.md"
    clip.write_text("# 外部摘录\n这是只读外部目录中的笔记。\n", encoding="utf-8")

    # First scan ingests without touching external file
    r1 = scan_once(kb_root, config)
    assert r1["ingested_count"] == 1
    assert clip.exists()

    # Second scan skips via mtime fast path
    r2 = scan_once(kb_root, config)
    assert r2["ingested_count"] == 0
    assert r2["skipped_unchanged_count"] == 1

    # Rename external file -> hash matches -> zero duplicate raw archive created
    renamed = ext_dir / "clipping-renamed.md"
    clip.rename(renamed)
    r3 = scan_once(kb_root, config)
    assert r3["ingested_count"] == 0
    assert r3["duplicate_count"] == 1

    conn = connect_db(kb_root)
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM raw_documents").fetchone()[0]
        origin = conn.execute("SELECT origin_path FROM raw_documents").fetchone()[0]
        assert cnt == 1
        assert origin == normalize_nfc(str(renamed.resolve()))
    finally:
        conn.close()


def test_concurrent_write_lock_stress(tmp_path: Path) -> None:
    cmd_init(tmp_path)
    config = load_config(tmp_path)

    # Seed several files in inbox and run concurrent scan_once + reindex_wiki across threads
    for i in range(6):
        (tmp_path / "inbox" / f"note_{i}.md").write_text(f"# 并发笔记 {i}\n内容 {i}\n", encoding="utf-8")

    def _worker(idx: int) -> bool:
        if idx % 2 == 0:
            res = scan_once(tmp_path, config)
            return res["ok"]
        res = reindex_wiki(tmp_path, config)
        return res["ok"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_worker, range(12)))
    assert all(results)

    # Verify all 6 notes ingested cleanly without lock errors
    conn = connect_db(tmp_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM raw_documents").fetchone()[0] == 6
    finally:
        conn.close()
