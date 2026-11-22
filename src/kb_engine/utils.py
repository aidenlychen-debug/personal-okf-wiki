"""Core deterministic utilities: NFC/URL safety, write lock, atomic IO, Markdown & Git."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

from kb_engine.errors import ErrorCode, KBError

_FILENAME_FORBIDDEN_RE = re.compile(r'[/\\:*?"|()\[\]#<>]+')
_WHITESPACE_RE = re.compile(r"\s+")
_FENCED_CODE_RE = re.compile(r"(^|\n)```[^\n]*\n.*?(\n```(?=\n|$)|$)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_MD_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)\n]+)\)")
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")
_HUMAN_BLOCK_RE = re.compile(
    r"<!--\s*human:start\s*-->.*?<!--\s*human:end\s*-->",
    re.DOTALL,
)
_EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "tel:", "data:", "javascript:")


def normalize_nfc(text: str) -> str:
    """Normalize any Unicode string (e.g., macOS NFD paths) to NFC."""
    return unicodedata.normalize("NFC", text)


def normalize_rel_path(path: str | Path) -> str:
    """Convert path to NFC POSIX-style relative string without leading './'."""
    raw = path.as_posix() if isinstance(path, Path) else str(path).replace("\\", "/")
    norm = normalize_nfc(raw).strip()
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def sanitize_filename(name: str, fallback: str = "未命名文档", max_len: int = 100) -> str:
    """Sanitize a filename stem according to PRD Iron Law #2."""
    cleaned = normalize_nfc(name or "")
    cleaned = _FILENAME_FORBIDDEN_RE.sub(" ", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    cleaned = cleaned.strip(".- ")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(".- ")
    return cleaned or fallback


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def generate_session_id() -> str:
    now = datetime.now()
    return f"sess_{now.strftime('%Y%m%d_%H%M%S')}_{now.microsecond:06d}"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes atomically via same-directory temp file -> flush+fsync -> os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".kb_tmp_", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def atomic_write_text(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def unique_target_path(directory: Path, stem: str, suffix: str) -> Path:
    """Return a non-colliding path in `directory`, appending -2, -3... if needed."""
    directory.mkdir(parents=True, exist_ok=True)
    safe_stem = sanitize_filename(stem)
    if suffix and not suffix.startswith("."):
        suffix = f".{suffix}"
    candidate = directory / f"{safe_stem}{suffix}"
    if not candidate.exists():
        return candidate
    idx = 2
    while True:
        candidate = directory / f"{safe_stem}-{idx}{suffix}"
        if not candidate.exists():
            return candidate
        idx += 1


# Thread-safe + process-safe reentrant lock tracking keyed by resolved lock file path
_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_LOCK_STATE: dict[str, tuple[int, int]] = {}  # path -> (fd, depth)


@contextlib.contextmanager
def write_lock(root_dir: Path, timeout_seconds: float = 10.0) -> Iterator[None]:
    """Acquire thread-reentrant & cross-process exclusive write lock at .kb/write.lock."""
    kb_dir = root_dir / ".kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    lock_path = (kb_dir / "write.lock").resolve()
    key = str(lock_path)

    with _LOCK_GUARD:
        tlock = _THREAD_LOCKS.get(key)
        if tlock is None:
            tlock = threading.RLock()
            _THREAD_LOCKS[key] = tlock

    deadline = time.monotonic() + timeout_seconds
    # Step 1: Acquire in-process reentrant thread lock
    remaining = max(0.0, deadline - time.monotonic())
    if not tlock.acquire(timeout=remaining):
        raise KBError(
            ErrorCode.WRITE_LOCK_TIMEOUT,
            f"等待全局写锁 .kb/write.lock 超时（{timeout_seconds}s），请确认是否有其他写入任务正在运行。",
            details={"lock_file": str(lock_path), "timeout_seconds": timeout_seconds},
        )

    acquired_file_lock = False
    fd = -1
    try:
        state = _LOCK_STATE.get(key)
        if state is not None:
            # Same thread reentering while already holding the file lock
            _LOCK_STATE[key] = (state[0], state[1] + 1)
        else:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired_file_lock = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        with contextlib.suppress(OSError):
                            os.close(fd)
                        raise KBError(
                            ErrorCode.WRITE_LOCK_TIMEOUT,
                            f"等待全局写锁 .kb/write.lock 超时（{timeout_seconds}s），请确认是否有其他写入任务正在运行。",
                            details={"lock_file": str(lock_path), "timeout_seconds": timeout_seconds},
                        )
                    time.sleep(0.05)
            _LOCK_STATE[key] = (fd, 1)

        yield
    finally:
        state = _LOCK_STATE.get(key)
        if state is not None:
            cur_fd, depth = state
            if depth > 1:
                _LOCK_STATE[key] = (cur_fd, depth - 1)
            else:
                _LOCK_STATE.pop(key, None)
                with contextlib.suppress(OSError):
                    fcntl.flock(cur_fd, fcntl.LOCK_UN)
                with contextlib.suppress(OSError):
                    os.close(cur_fd)
        elif acquired_file_lock and fd != -1:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)
        tlock.release()


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str, str | None]:
    """Parse YAML frontmatter from Markdown text.

    Returns (metadata_dict_or_none, body_without_frontmatter, error_message_or_none).
    """
    norm = normalize_newlines(text)
    if not norm.startswith("---\n"):
        return None, norm, None
    end_idx = norm.find("\n---", 4)
    if end_idx == -1:
        return None, norm, "Frontmatter 缺少结束分隔符 ---"
    after_end = end_idx + 4
    if after_end < len(norm) and norm[after_end] not in ("\n",):
        # Ensure --- is on its own line
        line_end = norm.find("\n", after_end)
        trailing = norm[after_end:] if line_end == -1 else norm[after_end:line_end]
        if trailing.strip():
            return None, norm, "Frontmatter 结束分隔符 --- 后存在非法字符"
    yaml_str = norm[4:end_idx]
    body_start = after_end + 1 if after_end < len(norm) and norm[after_end] == "\n" else after_end
    body = norm[body_start:]
    try:
        parsed = yaml.safe_load(yaml_str)
    except yaml.YAMLError as exc:
        return None, body, f"YAML 解析失败: {exc}"
    if parsed is None:
        return {}, body, None
    if not isinstance(parsed, dict):
        return None, body, "Frontmatter 顶层必须为键值对映射"
    return parsed, body, None


def strip_code_blocks(markdown_text: str) -> str:
    """Strip fenced and inline code from Markdown while keeping link scanning safe."""
    no_fenced = _FENCED_CODE_RE.sub(lambda m: "\n" * m.group(0).count("\n"), markdown_text)
    return _INLINE_CODE_RE.sub("", no_fenced)


def find_wikilinks(markdown_text: str) -> list[str]:
    """Return any forbidden [[wikilinks]] outside code blocks."""
    cleaned = strip_code_blocks(markdown_text)
    return [m.group(0) for m in _WIKILINK_RE.finditer(cleaned)]


def extract_human_blocks(markdown_text: str) -> list[str]:
    """Extract all `<!-- human:start -->...<!-- human:end -->` blocks byte-accurately."""
    norm = normalize_newlines(markdown_text)
    return [m.group(0) for m in _HUMAN_BLOCK_RE.finditer(norm)]


@dataclass
class ResolvedLink:
    raw_link: str
    is_image: bool
    kind: str  # "external" | "anchor" | "wiki" | "raw" | "other_local"
    anchor: str
    decoded_target: str
    target_abs_path: Path | None
    target_rel_path: str | None  # relative to wiki/ when kind=='wiki', else relative to root_dir


def extract_markdown_links(markdown_text: str) -> list[tuple[bool, str, str]]:
    """Extract (is_image, label, raw_target) outside code blocks."""
    cleaned = strip_code_blocks(markdown_text)
    results: list[tuple[bool, str, str]] = []
    for match in _MD_LINK_RE.finditer(cleaned):
        is_image = match.group(1) == "!"
        label = match.group(2)
        target_expr = match.group(3).strip()
        # Strip optional title part: path "title"
        if target_expr.startswith("<") and ">" in target_expr:
            target_expr = target_expr[1 : target_expr.index(">")].strip()
        else:
            # Split optional markdown link title if present
            title_split = re.match(r'^(\S+)\s+["\'].*["\']$', target_expr)
            if title_split:
                target_expr = title_split.group(1)
        results.append((is_image, label, target_expr))
    return results


def classify_and_resolve_link(
    source_file: Path,
    raw_link: str,
    is_image: bool,
    root_dir: Path,
) -> ResolvedLink:
    """Classify and resolve a Markdown link according to PRD Iron Law #2."""
    trimmed = raw_link.strip()
    lower = trimmed.lower()
    if any(lower.startswith(scheme) for scheme in _EXTERNAL_SCHEMES):
        return ResolvedLink(
            raw_link=raw_link,
            is_image=is_image,
            kind="external",
            anchor="",
            decoded_target=trimmed,
            target_abs_path=None,
            target_rel_path=None,
        )
    if trimmed.startswith("#"):
        return ResolvedLink(
            raw_link=raw_link,
            is_image=is_image,
            kind="anchor",
            anchor=trimmed,
            decoded_target="",
            target_abs_path=None,
            target_rel_path=None,
        )

    # 1. Strip #anchor -> 2. urllib.parse.unquote() -> 3. Unicode NFC
    if "#" in trimmed:
        path_part, anchor_part = trimmed.split("#", 1)
        anchor = f"#{anchor_part}"
    else:
        path_part, anchor = trimmed, ""

    decoded = normalize_nfc(urllib.parse.unquote(path_part))
    if not decoded:
        return ResolvedLink(
            raw_link=raw_link,
            is_image=is_image,
            kind="anchor",
            anchor=anchor,
            decoded_target="",
            target_abs_path=None,
            target_rel_path=None,
        )

    root_resolved = root_dir.resolve()
    wiki_resolved = (root_resolved / "wiki").resolve()
    raw_resolved = (root_resolved / "raw").resolve()

    if decoded.startswith("/"):
        target_abs = Path(decoded).resolve()
    else:
        target_abs = (source_file.resolve().parent / decoded).resolve()

    # Check if inside wiki/
    try:
        rel_wiki = target_abs.relative_to(wiki_resolved)
        return ResolvedLink(
            raw_link=raw_link,
            is_image=is_image,
            kind="wiki",
            anchor=anchor,
            decoded_target=decoded,
            target_abs_path=target_abs,
            target_rel_path=normalize_rel_path(rel_wiki),
        )
    except ValueError:
        pass

    # Check if inside raw/
    try:
        rel_root = target_abs.relative_to(root_resolved)
        target_abs.relative_to(raw_resolved)
        return ResolvedLink(
            raw_link=raw_link,
            is_image=is_image,
            kind="raw",
            anchor=anchor,
            decoded_target=decoded,
            target_abs_path=target_abs,
            target_rel_path=normalize_rel_path(rel_root),
        )
    except ValueError:
        pass

    try:
        rel_root = normalize_rel_path(target_abs.relative_to(root_resolved))
    except ValueError:
        rel_root = normalize_nfc(str(target_abs))

    return ResolvedLink(
        raw_link=raw_link,
        is_image=is_image,
        kind="other_local",
        anchor=anchor,
        decoded_target=decoded,
        target_abs_path=target_abs,
        target_rel_path=rel_root,
    )


def compute_effective_char_count(body: str) -> int:
    """Compute effective character count (non-whitespace characters) of Markdown body."""
    return len(re.sub(r"\s+", "", body))


# ---------------------------------------------------------------------------
# Git deterministic transaction & recovery helpers
# ---------------------------------------------------------------------------


def run_git(
    root_dir: Path,
    args: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(root_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=merged_env,
    )
    if check and proc.returncode != 0:
        raise KBError(
            ErrorCode.GIT_OPERATION_FAILED,
            f"Git 命令执行失败: git {' '.join(args)}",
            details={
                "returncode": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            },
        )
    return proc


def ensure_git_repo(root_dir: Path) -> dict[str, Any]:
    """Idempotently initialize Git repo, configure UTF-8 paths & local identity fallback."""
    created_repo = False
    identity_configured = False

    if not (root_dir / ".git").exists():
        run_git(root_dir, ["init"])
        created_repo = True

    # Prevent octal escaping of Chinese paths in git status / ls-tree
    run_git(root_dir, ["config", "core.quotepath", "false"])
    # Preserve NFC/Unicode cleanly on macOS
    run_git(root_dir, ["config", "core.precomposeunicode", "true"], check=False)

    name_proc = run_git(root_dir, ["config", "--get", "user.name"], check=False)
    email_proc = run_git(root_dir, ["config", "--get", "user.email"], check=False)

    if name_proc.returncode != 0 or not name_proc.stdout.strip():
        run_git(root_dir, ["config", "user.name", "Personal OKF-Wiki"])
        identity_configured = True
    if email_proc.returncode != 0 or not email_proc.stdout.strip():
        run_git(root_dir, ["config", "user.email", "local@okf-wiki.invalid"])
        identity_configured = True

    return {
        "created_repo": created_repo,
        "identity_configured": identity_configured,
    }


def git_head_sha(root_dir: Path) -> str | None:
    proc = run_git(root_dir, ["rev-parse", "HEAD"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def git_status_porcelain(root_dir: Path, paths: list[str] | None = None) -> list[str]:
    args = ["status", "--porcelain", "-uall"]
    if paths:
        args.append("--")
        args.extend(paths)
    proc = run_git(root_dir, args)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def git_has_changes(root_dir: Path, paths: list[str] | None = None) -> bool:
    return bool(git_status_porcelain(root_dir, paths))


def git_list_tracked_files(
    root_dir: Path, commit_sha: str, prefix: str = "wiki"
) -> list[str]:
    proc = run_git(
        root_dir,
        ["ls-tree", "-r", "--name-only", commit_sha, "--", prefix],
        check=False,
    )
    if proc.returncode != 0:
        return []
    return [normalize_rel_path(line) for line in proc.stdout.splitlines() if line.strip()]


def git_show_file(root_dir: Path, commit_sha: str, rel_path: str) -> str | None:
    norm_path = normalize_rel_path(rel_path)
    proc = run_git(root_dir, ["show", f"{commit_sha}:{norm_path}"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def git_find_commit_by_session(
    root_dir: Path, session_id: str, limit: int = 10
) -> str | None:
    marker = f"[kb-session:{session_id}]"
    proc = run_git(
        root_dir,
        [
            "log",
            f"-n{limit}",
            "--fixed-strings",
            f"--grep={marker}",
            "--format=%H",
        ],
        check=False,
    )
    if proc.returncode != 0:
        return None
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return lines[0] if lines else None


def git_backup_dirty_wiki(
    root_dir: Path,
    base_commit_sha: str,
    session_id: str,
) -> tuple[str | None, list[str]]:
    """Atomically snapshot dirty wiki/ to refs/kb/recovery/<session_id> using a temporary index."""
    dirty_lines = git_status_porcelain(root_dir, ["wiki"])
    if not dirty_lines:
        return None, []

    backed_up_files: list[str] = []
    for line in dirty_lines:
        # Porcelain format: XY <path> or XY <old> -> <new>
        path_part = line[3:].strip()
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1].strip()
        path_part = path_part.strip('"')
        backed_up_files.append(normalize_rel_path(path_part))

    tmp_fd, tmp_index_name = tempfile.mkstemp(prefix=".kb_git_idx_", dir=str(root_dir / ".kb"))
    os.close(tmp_fd)
    # Remove the empty file so git read-tree creates a valid index file
    os.unlink(tmp_index_name)
    env = {"GIT_INDEX_FILE": tmp_index_name}
    ref_name = f"refs/kb/recovery/{session_id}"
    try:
        run_git(root_dir, ["read-tree", base_commit_sha], env=env)
        run_git(root_dir, ["add", "-A", "--", "wiki"], env=env)
        tree_sha = run_git(root_dir, ["write-tree"], env=env).stdout.strip()
        commit_sha = run_git(
            root_dir,
            [
                "commit-tree",
                tree_sha,
                "-p",
                base_commit_sha,
                "-m",
                f"chore(recovery): backup dirty wiki before recover\n\n[kb-recovery:{session_id}]",
            ],
            env=env,
        ).stdout.strip()
        run_git(root_dir, ["update-ref", ref_name, commit_sha])
        return ref_name, sorted(set(backed_up_files))
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_index_name)


def git_restore_wiki_to_commit(root_dir: Path, commit_sha: str) -> None:
    """Restore wiki/ to commit_sha and remove untracked half-baked files inside wiki/."""
    run_git(root_dir, ["checkout", commit_sha, "--", "wiki"])
    run_git(root_dir, ["clean", "-fd", "--", "wiki"])


def git_latest_commits(root_dir: Path, limit: int = 5) -> list[dict[str, str]]:
    proc = run_git(
        root_dir,
        ["log", f"-n{limit}", "--format=%H|%cI|%s"],
        check=False,
    )
    if proc.returncode != 0:
        return []
    commits: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({"sha": parts[0], "date": parts[1], "subject": parts[2]})
    return commits
