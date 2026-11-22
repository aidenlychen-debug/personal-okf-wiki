"""Deterministic page renaming and vault-wide relative Markdown link rewriting."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from kb_engine.config import KBConfig
from kb_engine.db import db_session
from kb_engine.errors import ErrorCode, KBError
from kb_engine.indexer import iter_wiki_content_files, reindex_wiki_in_conn
from kb_engine.utils import (
    atomic_write_text,
    classify_and_resolve_link,
    git_has_changes,
    normalize_newlines,
    normalize_nfc,
    normalize_rel_path,
    run_git,
    sanitize_filename,
    write_lock,
)

_FENCED_SPLIT_RE = re.compile(r"(```[^\n]*\n.*?\n```)", re.DOTALL)
_INLINE_CODE_SPLIT_RE = re.compile(r"(`[^`\n]+`)")
_MD_LINK_TOKEN_RE = re.compile(r"(!?\[[^\]]*\]\()([^)\n]+)(\))")
_FM_TITLE_RE = re.compile(r'^(title\s*:\s*)(.*)$', re.MULTILINE)
_FM_REF_RE = re.compile(r'^(\s*ref\s*:\s*)(["\']?)([^"\'\n]+)\2(\s*)$', re.MULTILINE)
_H1_LINE_RE = re.compile(r"^(#\s+)(.+)$", re.MULTILINE)


def _strip_wiki_prefix(rel_path: str) -> str:
    norm = normalize_rel_path(rel_path)
    if norm.startswith("wiki/"):
        return norm[5:]
    return norm


def _sanitize_wiki_rel_path(rel_path: str) -> str:
    norm = _strip_wiki_prefix(rel_path)
    p = Path(norm)
    suffix = p.suffix or ".md"
    clean_stem = sanitize_filename(p.stem, fallback="")
    if not clean_stem:
        raise KBError(ErrorCode.INVALID_ARGUMENT, f"目标文件名无效: {rel_path}")
    parent_parts = [sanitize_filename(part, fallback=part) for part in p.parent.parts if part not in ("", ".")]
    if parent_parts:
        return normalize_rel_path(Path(*parent_parts) / f"{clean_stem}{suffix}")
    return normalize_rel_path(f"{clean_stem}{suffix}")


def _compute_relative_md_link(from_file: Path, to_file: Path) -> str:
    rel = os.path.relpath(str(to_file.resolve()), start=str(from_file.resolve().parent))
    rel_posix = normalize_nfc(rel.replace("\\", "/"))
    if not rel_posix.startswith("."):
        rel_posix = f"./{rel_posix}"
    return rel_posix


def _rewrite_links_in_text(
    text: str,
    *,
    source_file: Path,
    old_target_abs: Path,
    new_target_abs: Path,
    root_dir: Path,
) -> tuple[str, int]:
    """Rewrite relative Markdown links pointing to old_target_abs outside code blocks."""
    old_resolved = old_target_abs.resolve()
    replaced_count = 0

    def _replace_in_plain(segment: str) -> str:
        nonlocal replaced_count

        def _repl(match: re.Match[str]) -> str:
            nonlocal replaced_count
            prefix, raw_target, suffix = match.group(1), match.group(2), match.group(3)
            is_image = prefix.startswith("!")
            trimmed = raw_target.strip()

            link_title_tail = ""
            target_core = trimmed
            title_match = re.match(r'^(\S+)(\s+["\'].*["\'])$', trimmed)
            if title_match:
                target_core = title_match.group(1)
                link_title_tail = title_match.group(2)

            resolved = classify_and_resolve_link(source_file, target_core, is_image, root_dir)
            if resolved.kind in ("external", "anchor") or resolved.target_abs_path is None:
                return match.group(0)

            if resolved.target_abs_path.resolve() == old_resolved:
                new_rel = _compute_relative_md_link(source_file, new_target_abs)
                replaced_count += 1
                return f"{prefix}{new_rel}{resolved.anchor}{link_title_tail}{suffix}"
            return match.group(0)

        return _MD_LINK_TOKEN_RE.sub(_repl, segment)

    out_chunks: list[str] = []
    for fenced_chunk in _FENCED_SPLIT_RE.split(text):
        if fenced_chunk.startswith("```"):
            out_chunks.append(fenced_chunk)
            continue
        inline_parts = _INLINE_CODE_SPLIT_RE.split(fenced_chunk)
        for idx, part in enumerate(inline_parts):
            if idx % 2 == 1:
                out_chunks.append(part)
            else:
                out_chunks.append(_replace_in_plain(part))
    return "".join(out_chunks), replaced_count


def _rewrite_outgoing_links_for_moved_file(
    text: str,
    *,
    old_source_abs: Path,
    new_source_abs: Path,
    root_dir: Path,
) -> tuple[str, int]:
    """Rewrite outgoing relative links & YAML sources[].ref when a page moves across directories."""
    if old_source_abs.resolve().parent == new_source_abs.resolve().parent:
        return text, 0

    old_resolved = old_source_abs.resolve()
    new_resolved = new_source_abs.resolve()
    replaced_count = 0

    norm = normalize_newlines(text)
    fm_block = ""
    body_block = norm
    if norm.startswith("---\n"):
        end_idx = norm.find("\n---", 4)
        if end_idx != -1:
            fm_block = norm[: end_idx + 4]
            body_block = norm[end_idx + 4 :]

    # 1. Rewrite YAML frontmatter sources[].ref relative paths
    if fm_block:
        def _fm_ref_repl(match: re.Match[str]) -> str:
            nonlocal replaced_count
            lead, quote, raw_ref, tail = match.group(1), match.group(2), match.group(3), match.group(4)
            resolved = classify_and_resolve_link(old_resolved, raw_ref, False, root_dir)
            if resolved.kind in ("external", "anchor") or resolved.target_abs_path is None:
                return match.group(0)
            target_abs = new_resolved if resolved.target_abs_path.resolve() == old_resolved else resolved.target_abs_path
            new_rel = _compute_relative_md_link(new_resolved, target_abs)
            if new_rel != raw_ref:
                replaced_count += 1
            return f"{lead}{quote}{new_rel}{resolved.anchor}{quote}{tail}"

        fm_block = _FM_REF_RE.sub(_fm_ref_repl, fm_block)

    # 2. Rewrite Markdown outgoing links in body (skipping code blocks)
    def _replace_body_plain(segment: str) -> str:
        nonlocal replaced_count

        def _repl(match: re.Match[str]) -> str:
            nonlocal replaced_count
            prefix, raw_target, suffix = match.group(1), match.group(2), match.group(3)
            is_image = prefix.startswith("!")
            trimmed = raw_target.strip()

            link_title_tail = ""
            target_core = trimmed
            title_match = re.match(r'^(\S+)(\s+["\'].*["\'])$', trimmed)
            if title_match:
                target_core = title_match.group(1)
                link_title_tail = title_match.group(2)

            resolved = classify_and_resolve_link(old_resolved, target_core, is_image, root_dir)
            if resolved.kind in ("external", "anchor") or resolved.target_abs_path is None:
                return match.group(0)

            target_abs = new_resolved if resolved.target_abs_path.resolve() == old_resolved else resolved.target_abs_path
            new_rel = _compute_relative_md_link(new_resolved, target_abs)
            if new_rel != target_core:
                replaced_count += 1
            return f"{prefix}{new_rel}{resolved.anchor}{link_title_tail}{suffix}"

        return _MD_LINK_TOKEN_RE.sub(_repl, segment)

    body_chunks: list[str] = []
    for fenced_chunk in _FENCED_SPLIT_RE.split(body_block):
        if fenced_chunk.startswith("```"):
            body_chunks.append(fenced_chunk)
            continue
        inline_parts = _INLINE_CODE_SPLIT_RE.split(fenced_chunk)
        for idx, part in enumerate(inline_parts):
            if idx % 2 == 1:
                body_chunks.append(part)
            else:
                body_chunks.append(_replace_body_plain(part))

    return fm_block + "".join(body_chunks), replaced_count


def _update_title_in_markdown(text: str, new_title: str) -> str:
    norm = normalize_newlines(text)
    clean_title = normalize_nfc(new_title).strip()
    if norm.startswith("---\n"):
        end_idx = norm.find("\n---", 4)
        if end_idx != -1:
            fm = norm[4:end_idx]
            rest = norm[end_idx:]
            escaped = clean_title.replace('"', '\\"')
            if _FM_TITLE_RE.search(fm):
                fm = _FM_TITLE_RE.sub(rf'\1"{escaped}"', fm, count=1)
            else:
                fm = f'title: "{escaped}"\n' + fm
            rest = _H1_LINE_RE.sub(rf"\1{clean_title}", rest, count=1)
            return f"---\n{fm}{rest}"
    return _H1_LINE_RE.sub(rf"\1{clean_title}", norm, count=1)


def rename_wiki_page(
    root_dir: Path,
    config: KBConfig,
    old_rel: str,
    new_rel: str,
    *,
    new_title: str | None = None,
) -> dict[str, Any]:
    """Deterministically rename/move a wiki page, rewrite all incoming & outgoing links, reindex & commit."""
    wiki_dir = root_dir / "wiki"
    old_wiki_rel = _strip_wiki_prefix(old_rel)
    new_wiki_rel = _sanitize_wiki_rel_path(new_rel)

    if old_wiki_rel in ("index.md", "log.md") or new_wiki_rel in ("index.md", "log.md"):
        raise KBError(ErrorCode.INVALID_ARGUMENT, "禁止重命名系统受控文件 index.md 或 log.md。")

    old_abs = (wiki_dir / old_wiki_rel).resolve()
    new_abs = (wiki_dir / new_wiki_rel).resolve()

    with write_lock(root_dir, config.safety.write_lock_timeout_seconds):
        if not old_abs.exists() or not old_abs.is_file():
            raise KBError(
                ErrorCode.FILE_NOT_FOUND,
                f"待重命名的页面不存在: wiki/{old_wiki_rel}",
                details={"old_path": f"wiki/{old_wiki_rel}"},
            )
        if old_abs != new_abs and new_abs.exists():
            raise KBError(
                ErrorCode.PATH_CONFLICT,
                f"目标路径已存在，禁止覆盖: wiki/{new_wiki_rel}",
                details={"new_path": f"wiki/{new_wiki_rel}"},
            )

        content = normalize_newlines(old_abs.read_text(encoding="utf-8"))
        if new_title:
            content = _update_title_in_markdown(content, new_title)

        # Rewrite moved file's own outgoing relative links & YAML sources[].ref if directory depth changed
        content, outgoing_rewritten = _rewrite_outgoing_links_for_moved_file(
            content,
            old_source_abs=old_abs,
            new_source_abs=new_abs,
            root_dir=root_dir,
        )

        # Write atomically to new path and remove old path
        new_abs.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(new_abs, content)
        if old_abs != new_abs:
            old_abs.unlink()

        # Rewrite all incoming links across wiki/
        updated_files: list[str] = []
        total_rewritten_links = 0
        for file_path in iter_wiki_content_files(root_dir):
            orig = normalize_newlines(file_path.read_text(encoding="utf-8"))
            rewritten, count = _rewrite_links_in_text(
                orig,
                source_file=file_path,
                old_target_abs=old_abs,
                new_target_abs=new_abs,
                root_dir=root_dir,
            )
            if count > 0 and rewritten != orig:
                atomic_write_text(file_path, rewritten)
                updated_files.append(f"wiki/{normalize_rel_path(file_path.relative_to(wiki_dir))}")
                total_rewritten_links += count

        # Reindex DB & regenerate wiki/index.md
        with db_session(root_dir) as conn:
            index_stats = reindex_wiki_in_conn(root_dir, conn, regenerate_index=True)

        commit_sha: str | None = None
        if config.safety.auto_git_commit and (root_dir / ".git").exists():
            if git_has_changes(root_dir, ["wiki"]):
                run_git(root_dir, ["add", "-A", "--", "wiki"])
                run_git(
                    root_dir,
                    [
                        "commit",
                        "-m",
                        f"refactor(wiki): rename {old_wiki_rel} -> {new_wiki_rel}",
                    ],
                )
                commit_sha = run_git(root_dir, ["rev-parse", "HEAD"]).stdout.strip()

    return {
        "ok": True,
        "old_path": f"wiki/{old_wiki_rel}",
        "new_path": f"wiki/{new_wiki_rel}",
        "updated_title": new_title,
        "rewritten_files": updated_files,
        "rewritten_links_count": total_rewritten_links,
        "outgoing_links_rewritten_count": outgoing_rewritten,
        "commit_sha": commit_sha,
        **index_stats,
    }
