"""CLI entrypoint (`kb`) for Personal OKF-Wiki with strict --json & error contracts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kb_engine.config import DEFAULT_CONFIG_YAML, KBConfig, load_config
from kb_engine.db import (
    connect_db,
    db_session,
    get_open_session,
    get_session_by_id,
    init_database,
    mark_document_failed,
    retry_failed_documents,
)
from kb_engine.errors import ErrorCode, KBError
from kb_engine.evaluator import run_eval
from kb_engine.evolution import (
    build_evolve_report,
    get_evolution_status,
    record_evolve_checkpoint,
)
from kb_engine.exporter import export_skill_bundle
from kb_engine.indexer import (
    append_wiki_log,
    generate_wiki_index,
    rebuild_database,
    reindex_wiki,
    reindex_wiki_in_conn,
)
from kb_engine.refactor import merge_wiki_pages, rename_wiki_page
from kb_engine.retriever import query_wiki
from kb_engine.utils import (
    atomic_write_text,
    ensure_git_repo,
    generate_session_id,
    git_backup_dirty_wiki,
    git_find_commit_by_session,
    git_has_changes,
    git_head_sha,
    git_latest_commits,
    git_restore_wiki_to_commit,
    git_status_porcelain,
    normalize_rel_path,
    run_git,
    utc_now_iso,
    write_lock,
)
from kb_engine.adapters import build_default_registry
from kb_engine.validator import lint_wiki
from kb_engine.watcher import add_direct_text, add_url, is_watcher_running, run_watcher, scan_once

_DEFAULT_GITIGNORE = """.kb/
.venv/
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.DS_Store
inbox/.processed/
"""

_DEFAULT_LOG_MD = """# 知识库更新日志

> 本文件由 `kb compile-commit` 确定性自动追加，请勿手动修改历史条目。
"""


# ---------------------------------------------------------------------------
# High-level command implementations (callable both from CLI and tests/agents)
# ---------------------------------------------------------------------------


def cmd_init(root_dir: Path) -> dict[str, Any]:
    """Idempotently initialize directory tree, Git repo/identity, config, index/log & DB."""
    root_dir.mkdir(parents=True, exist_ok=True)
    # Create .kb first so write_lock works
    (root_dir / ".kb").mkdir(parents=True, exist_ok=True)

    with write_lock(root_dir, timeout_seconds=10.0):
        subdirs = [
            "inbox",
            "inbox/.processed",
            "raw",
            "raw/assets",
            "wiki",
            "wiki/sources",
            "wiki/concepts",
            "wiki/cases",
            "wiki/evidences",
            "wiki/insights",
            "skills/kb-ingest",
            "skills/kb-viral-ingest",
            "skills/kb-query",
            "skills/kb-lint",
            "skills/kb-evolve",
            "docs",
        ]
        for sub in subdirs:
            (root_dir / sub).mkdir(parents=True, exist_ok=True)

        config_path = root_dir / "config.yaml"
        if not config_path.exists():
            atomic_write_text(config_path, DEFAULT_CONFIG_YAML)

        gitignore_path = root_dir / ".gitignore"
        if not gitignore_path.exists():
            atomic_write_text(gitignore_path, _DEFAULT_GITIGNORE)

        index_path = root_dir / "wiki" / "index.md"
        if not index_path.exists():
            generate_wiki_index(root_dir, [])

        log_path = root_dir / "wiki" / "log.md"
        if not log_path.exists():
            atomic_write_text(log_path, _DEFAULT_LOG_MD)

        init_database(root_dir, reset=False)
        git_info = ensure_git_repo(root_dir)

        head_sha = git_head_sha(root_dir)
        initial_commit_created = False
        if head_sha is None:
            run_git(root_dir, ["add", ".gitignore", "config.yaml", "wiki/index.md", "wiki/log.md"])
            run_git(root_dir, ["commit", "-m", "chore(init): initialize Personal OKF-Wiki"])
            head_sha = git_head_sha(root_dir)
            initial_commit_created = True

    return {
        "ok": True,
        "root_dir": str(root_dir.resolve()),
        "head_sha": head_sha,
        "initial_commit_created": initial_commit_created,
        **git_info,
    }


def cmd_compile_begin(root_dir: Path, config: KBConfig, *, limit: int = 10) -> dict[str, Any]:
    """Isolate human wiki/ edits, open ACTIVE compile session, and lock up to `limit` pending items."""
    with write_lock(root_dir, config.safety.write_lock_timeout_seconds):
        ensure_git_repo(root_dir)
        with db_session(root_dir) as conn:
            existing = get_open_session(conn)
            if existing:
                raise KBError(
                    ErrorCode.ACTIVE_SESSION_EXISTS,
                    f"已存在未关闭的编译会话 {existing['session_id']}（状态: {existing['status']}），请先执行 kb recover 或继续完成该会话。",
                    details={"session_id": existing["session_id"], "status": existing["status"]},
                )

            # Strictly check ONLY wiki/ for human uncommitted edits (never touch raw/)
            human_checkpoint_created = False
            if config.safety.auto_git_commit and git_has_changes(root_dir, ["wiki"]):
                run_git(root_dir, ["add", "-A", "--", "wiki"])
                run_git(root_dir, ["commit", "-m", "chore(human): pre-ingest checkpoint"])
                human_checkpoint_created = True

            base_commit_sha = git_head_sha(root_dir)
            if not base_commit_sha:
                raise KBError(
                    ErrorCode.GIT_OPERATION_FAILED,
                    "Git 仓库尚无初始提交，请先运行 kb init。",
                )

            pending_rows = conn.execute(
                """
                SELECT * FROM raw_documents
                WHERE state = 'PENDING_COMPILE'
                ORDER BY created_at ASC, raw_rel_path ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

            session_id = generate_session_id()
            now = utc_now_iso()
            conn.execute(
                """
                INSERT INTO compile_sessions (session_id, base_commit_sha, status, recovery_ref, started_at, finished_at)
                VALUES (?, ?, 'ACTIVE', NULL, ?, NULL)
                """,
                (session_id, base_commit_sha, now),
            )

            items: list[dict[str, Any]] = []
            for row in pending_rows:
                conn.execute(
                    """
                    UPDATE raw_documents
                    SET state = 'COMPILING', session_id = ?, updated_at = ?
                    WHERE content_hash = ?
                    """,
                    (session_id, now, row["content_hash"]),
                )
                items.append(
                    {
                        "content_hash": row["content_hash"],
                        "raw_rel_path": row["raw_rel_path"],
                        "asset_rel_path": row["asset_rel_path"],
                        "media_type": row["media_type"],
                        "needs_vision": bool(row["needs_vision"]),
                        "source_origin": row["source_origin"],
                    }
                )

    return {
        "ok": True,
        "session_id": session_id,
        "base_commit_sha": base_commit_sha,
        "human_checkpoint_created": human_checkpoint_created,
        "wiki_index_path": "wiki/index.md",
        "items": items,
    }


def cmd_compile_fail(
    root_dir: Path,
    config: KBConfig,
    *,
    session_id: str,
    content_hash: str,
    reason: str,
) -> dict[str, Any]:
    """Poison-pill isolation: mark one broken raw document as FAILED without blocking the batch."""
    with write_lock(root_dir, config.safety.write_lock_timeout_seconds):
        with db_session(root_dir) as conn:
            record = mark_document_failed(
                conn,
                session_id=session_id,
                content_hash=content_hash,
                reason=reason,
            )
    return {
        "ok": True,
        "session_id": session_id,
        "content_hash": record["content_hash"],
        "raw_rel_path": record["raw_rel_path"],
        "state": record["state"],
        "retry_count": record["retry_count"],
        "error_message": record["error_message"],
    }


def cmd_retry(
    root_dir: Path,
    config: KBConfig,
    *,
    content_hash: str | None = None,
    session_id: str | None = None,
    all_failed: bool = False,
) -> dict[str, Any]:
    """Reset FAILED raw documents back to PENDING_COMPILE while preserving last_error_message."""
    with write_lock(root_dir, config.safety.write_lock_timeout_seconds):
        with db_session(root_dir) as conn:
            retried = retry_failed_documents(
                conn,
                content_hash=content_hash,
                session_id=session_id,
                all_failed=all_failed,
            )
    return {
        "ok": True,
        "retried_count": len(retried),
        "items": [
            {
                "content_hash": r["content_hash"],
                "raw_rel_path": r["raw_rel_path"],
                "state": r["state"],
                "retry_count": r["retry_count"],
                "last_error_message": r["last_error_message"],
            }
            for r in retried
        ],
    }


def cmd_compile_commit(
    root_dir: Path,
    config: KBConfig,
    *,
    session_id: str,
    note: str,
    allow_shrinkage: list[str] | None = None,
    allow_delete: list[str] | None = None,
) -> dict[str, Any]:
    """Validate, regenerate index/log, transition COMMITTING -> exact Git commit -> COMPILED."""
    with write_lock(root_dir, config.safety.write_lock_timeout_seconds):
        # Verify ACTIVE session and gather compiling/failed rows
        with db_session(root_dir) as conn:
            session = get_session_by_id(conn, session_id)
            if not session or session["status"] != "ACTIVE":
                raise KBError(
                    ErrorCode.SESSION_NOT_FOUND,
                    f"未找到处于 ACTIVE 状态的编译会话: {session_id}",
                    details={"session_id": session_id},
                )
            base_commit_sha = session["base_commit_sha"]
            compiling_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM raw_documents WHERE session_id = ? AND state = 'COMPILING'",
                    (session_id,),
                ).fetchall()
            ]
            failed_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM raw_documents WHERE session_id = ? AND state = 'FAILED'",
                    (session_id,),
                ).fetchall()
            ]

        wiki_dirty_before = git_status_porcelain(root_dir, ["wiki"])

        # Short-circuit: all items marked FAILED (or empty batch) and no wiki changes -> close without empty commit
        if not compiling_rows and not wiki_dirty_before:
            now = utc_now_iso()
            with db_session(root_dir) as conn:
                conn.execute(
                    "UPDATE compile_sessions SET status = 'COMMITTED', finished_at = ? WHERE session_id = ?",
                    (now, session_id),
                )
            return {
                "ok": True,
                "session_id": session_id,
                "commit_sha": None,
                "compiled_count": 0,
                "failed_count": len(failed_rows),
                "empty_commit_skipped": True,
            }

        # Step 2: Run full hard validation relative to base_commit_sha
        lint_res = lint_wiki(
            root_dir,
            config,
            base_commit=base_commit_sha,
            allow_shrinkage=allow_shrinkage,
            allow_delete=allow_delete,
        )
        if not lint_res["ok"]:
            first_code = lint_res["errors"][0]["code"]
            raise KBError(
                first_code,
                f"提交被安全校验阻断（共 {lint_res['error_count']} 项错误）: {lint_res['errors'][0]['message']}",
                details={"errors": lint_res["errors"], "warnings": lint_res["warnings"]},
            )

        # Compute changed wiki pages for log.md
        changed_pages: list[str] = []
        for line in wiki_dirty_before:
            path_part = line[3:].strip().strip('"')
            if " -> " in path_part:
                path_part = path_part.split(" -> ", 1)[1].strip().strip('"')
            norm_p = normalize_rel_path(path_part)
            if norm_p not in ("wiki/index.md", "wiki/log.md"):
                changed_pages.append(norm_p)

        # Step 3: Deterministically regenerate wiki/index.md and prepend wiki/log.md
        with db_session(root_dir) as conn:
            reindex_wiki_in_conn(root_dir, conn, regenerate_index=True)

        append_wiki_log(
            root_dir,
            note=note,
            session_id=session_id,
            changed_pages=changed_pages,
            failed_items=failed_rows,
        )

        # Step 4A: Transition SQLite session to COMMITTING and commit local transaction
        with db_session(root_dir) as conn:
            conn.execute(
                "UPDATE compile_sessions SET status = 'COMMITTING' WHERE session_id = ?",
                (session_id,),
            )

        # Step 4B: Exact Git staging (wiki/ + ONLY this session's COMPILING raw_rel_path/asset_rel_path)
        commit_sha: str | None = None
        if config.safety.auto_git_commit:
            run_git(root_dir, ["add", "-A", "--", "wiki"])
            exact_raw_paths: list[str] = []
            for row in compiling_rows:
                raw_p = root_dir / row["raw_rel_path"]
                if raw_p.exists():
                    exact_raw_paths.append(row["raw_rel_path"])
                if row["asset_rel_path"]:
                    asset_p = root_dir / row["asset_rel_path"]
                    if asset_p.exists():
                        exact_raw_paths.append(row["asset_rel_path"])
            if exact_raw_paths:
                run_git(root_dir, ["add", "--", *exact_raw_paths])

            # Commit with immutable session trailer
            staged_check = run_git(root_dir, ["diff", "--cached", "--quiet"], check=False)
            if staged_check.returncode != 0:
                commit_msg = f"feat(wiki): {note}\n\n[kb-session:{session_id}]"
                run_git(root_dir, ["commit", "-m", commit_msg])
                commit_sha = git_head_sha(root_dir)

        # Step 4C: Git succeeded -> mark COMPILING docs COMPILED, session COMMITTED, refresh index
        now = utc_now_iso()
        with db_session(root_dir) as conn:
            conn.execute(
                """
                UPDATE raw_documents
                SET state = 'COMPILED', updated_at = ?
                WHERE session_id = ? AND state = 'COMPILING'
                """,
                (now, session_id),
            )
            conn.execute(
                """
                UPDATE compile_sessions
                SET status = 'COMMITTED', finished_at = ?
                WHERE session_id = ?
                """,
                (now, session_id),
            )
            index_stats = reindex_wiki_in_conn(root_dir, conn, regenerate_index=True)
            evolution_status = get_evolution_status(conn, config)

    return {
        "ok": True,
        "session_id": session_id,
        "commit_sha": commit_sha,
        "compiled_count": len(compiling_rows),
        "failed_count": len(failed_rows),
        "changed_pages": sorted(set(changed_pages)),
        "warnings": lint_res["warnings"],
        "evolution": evolution_status,
        **index_stats,
    }


def cmd_recover(root_dir: Path, config: KBConfig) -> dict[str, Any]:
    """Cross-system reconciliation & zero-data-loss recovery via refs/kb/recovery/<id>."""
    with write_lock(root_dir, config.safety.write_lock_timeout_seconds):
        with db_session(root_dir) as conn:
            session = get_open_session(conn)
            if not session:
                return {
                    "ok": True,
                    "session_id": None,
                    "action": "noop",
                    "reconciled_commit_sha": None,
                    "recovery_ref": None,
                    "backed_up_files": [],
                    "reset_to_pending_count": 0,
                }

            session_id = session["session_id"]
            base_commit_sha = session["base_commit_sha"]
            status = session["status"]

            # 1. Reconcile COMMITTING session against Git history by [kb-session:<id>] trailer
            if status == "COMMITTING":
                matched_sha = git_find_commit_by_session(root_dir, session_id, limit=10)
                if matched_sha:
                    now = utc_now_iso()
                    conn.execute(
                        """
                        UPDATE raw_documents
                        SET state = 'COMPILED', updated_at = ?
                        WHERE session_id = ? AND state = 'COMPILING'
                        """,
                        (now, session_id),
                    )
                    conn.execute(
                        """
                        UPDATE compile_sessions
                        SET status = 'COMMITTED', finished_at = ?
                        WHERE session_id = ?
                        """,
                        (now, session_id),
                    )
                    reindex_wiki_in_conn(root_dir, conn, regenerate_index=True)
                    return {
                        "ok": True,
                        "session_id": session_id,
                        "action": "reconciled",
                        "reconciled_commit_sha": matched_sha,
                        "recovery_ref": None,
                        "backed_up_files": [],
                        "reset_to_pending_count": 0,
                    }

            # 2. Zero-data-loss backup of dirty wiki/ to refs/kb/recovery/<session_id>
            recovery_ref, backed_up_files = git_backup_dirty_wiki(
                root_dir,
                base_commit_sha,
                session_id,
            )

            # 3. Restore wiki/ to base_commit_sha (preserving human checkpoint, raw/, and FAILED docs)
            git_restore_wiki_to_commit(root_dir, base_commit_sha)

            now = utc_now_iso()
            cur = conn.execute(
                """
                UPDATE raw_documents
                SET state = 'PENDING_COMPILE', session_id = NULL, updated_at = ?
                WHERE session_id = ? AND state = 'COMPILING'
                """,
                (now, session_id),
            )
            reset_count = cur.rowcount

            conn.execute(
                """
                UPDATE compile_sessions
                SET status = 'RECOVERED', recovery_ref = ?, finished_at = ?
                WHERE session_id = ?
                """,
                (recovery_ref, now, session_id),
            )
            reindex_wiki_in_conn(root_dir, conn, regenerate_index=True)

    return {
        "ok": True,
        "session_id": session_id,
        "action": "rolled_back",
        "reconciled_commit_sha": None,
        "recovery_ref": recovery_ref,
        "backed_up_files": backed_up_files,
        "reset_to_pending_count": reset_count,
    }


def cmd_status(root_dir: Path, config: KBConfig) -> dict[str, Any]:
    """Lock-free health dashboard for the knowledge base."""
    watcher_running, watcher_pid = is_watcher_running(root_dir)
    conn = connect_db(root_dir, ensure_schema=True)
    try:
        page_counts = {
            row["type"]: row["cnt"]
            for row in conn.execute(
                "SELECT type, COUNT(*) AS cnt FROM wiki_pages GROUP BY type"
            ).fetchall()
        }
        raw_counts = {
            row["state"]: row["cnt"]
            for row in conn.execute(
                "SELECT state, COUNT(*) AS cnt FROM raw_documents GROUP BY state"
            ).fetchall()
        }
        open_session = get_open_session(conn)
        broken_links = conn.execute(
            "SELECT COUNT(*) FROM wiki_links WHERE is_broken = 1"
        ).fetchone()[0]
        evolution_status = get_evolution_status(conn, config)
    finally:
        conn.close()

    commits = git_latest_commits(root_dir, limit=5) if (root_dir / ".git").exists() else []
    return {
        "ok": True,
        "project": config.project.name,
        "root_dir": str(root_dir.resolve()),
        "wiki_pages": {
            "total": sum(page_counts.values()),
            "by_type": page_counts,
            "broken_links": broken_links,
        },
        "raw_documents": {
            "total": sum(raw_counts.values()),
            "pending_compile": raw_counts.get("PENDING_COMPILE", 0),
            "compiling": raw_counts.get("COMPILING", 0),
            "compiled": raw_counts.get("COMPILED", 0),
            "failed": raw_counts.get("FAILED", 0),
        },
        "active_session": open_session,
        "watcher": {
            "running": watcher_running,
            "pid": watcher_pid,
        },
        "evolution": evolution_status,
        "recent_commits": commits,
    }


def cmd_audit(root_dir: Path, config: KBConfig) -> dict[str, Any]:
    """Lock-free governance audit: student readiness, deprecated/stale/orphan pages, duplicates & V3 coverage."""
    from datetime import date

    lint_res = lint_wiki(root_dir, config)
    today = date.today()

    conn = connect_db(root_dir, ensure_schema=True)
    try:
        rows = conn.execute(
            """
            SELECT rel_path, type, title, status, visibility, is_verified,
                   stale_after, superseded_by, aliases_json,
                   reading_status, experience_type, origin, layers_json
            FROM wiki_pages
            ORDER BY rel_path ASC
            """
        ).fetchall()
        broken_links = conn.execute(
            "SELECT COUNT(*) FROM wiki_links WHERE is_broken = 1"
        ).fetchone()[0]
        candidate_relations_count = conn.execute(
            "SELECT COUNT(*) FROM wiki_links WHERE relation_status = 'candidate' AND is_broken = 0"
        ).fetchone()[0]
    finally:
        conn.close()

    status_counts = {"stable": 0, "draft": 0, "deprecated": 0}
    deprecated_pages: list[dict[str, Any]] = []
    stale_pages: list[dict[str, Any]] = []
    public_verified = 0
    public_unverified = 0
    unverified_queue: list[dict[str, Any]] = []
    name_index: dict[str, list[str]] = {}

    v3_reading: dict[str, int] = {}
    v3_experience: dict[str, int] = {}
    v3_origin: dict[str, int] = {}
    v3_layers: dict[str, int] = {}

    for r in rows:
        st = r["status"] or "stable"
        status_counts[st] = status_counts.get(st, 0) + 1
        if st == "deprecated":
            deprecated_pages.append(
                {
                    "rel_path": r["rel_path"],
                    "title": r["title"],
                    "superseded_by": r["superseded_by"],
                }
            )
        else:
            key_title = r["title"].strip().lower()
            if key_title:
                name_index.setdefault(key_title, []).append(r["rel_path"])
            try:
                aliases = json.loads(r["aliases_json"] or "[]")
            except (ValueError, TypeError):
                aliases = []
            for al in aliases:
                k = str(al).strip().lower()
                if k:
                    name_index.setdefault(k, []).append(r["rel_path"])

        if r["visibility"] == "public" and st == "stable":
            if r["is_verified"]:
                public_verified += 1
            else:
                public_unverified += 1
                unverified_queue.append(
                    {
                        "rel_path": r["rel_path"],
                        "title": r["title"],
                        "type": r["type"],
                    }
                )

        if r["stale_after"]:
            try:
                sd = date.fromisoformat(str(r["stale_after"]))
                if sd < today:
                    stale_pages.append(
                        {
                            "rel_path": r["rel_path"],
                            "title": r["title"],
                            "stale_after": sd.isoformat(),
                        }
                    )
            except ValueError:
                pass

        if r["reading_status"]:
            v3_reading[r["reading_status"]] = v3_reading.get(r["reading_status"], 0) + 1
        if r["experience_type"]:
            v3_experience[r["experience_type"]] = v3_experience.get(r["experience_type"], 0) + 1
        if r["origin"]:
            v3_origin[r["origin"]] = v3_origin.get(r["origin"], 0) + 1
        try:
            for lv in json.loads(r["layers_json"] or "[]"):
                v3_layers[lv] = v3_layers.get(lv, 0) + 1
        except (ValueError, TypeError):
            pass

    duplicate_candidates = [
        {"term": term, "pages": sorted(set(paths))}
        for term, paths in sorted(name_index.items())
        if len(set(paths)) > 1
    ]
    orphan_pages = [
        w["path"] for w in lint_res["warnings"] if w.get("code") == "ORPHAN_PAGE"
    ]

    return {
        "ok": lint_res["ok"],
        "counts": {
            "total": len(rows),
            "stable": status_counts.get("stable", 0),
            "draft": status_counts.get("draft", 0),
            "deprecated": status_counts.get("deprecated", 0),
            "stale": len(stale_pages),
            "orphans": len(orphan_pages),
            "broken_links": broken_links,
            "candidate_relations": candidate_relations_count,
        },
        "student_readiness": {
            "public_stable_total": public_verified + public_unverified,
            "public_verified": public_verified,
            "public_unverified": public_unverified,
            "unverified_queue": unverified_queue,
        },
        "deprecated_pages": deprecated_pages,
        "stale_pages": stale_pages,
        "orphan_pages": orphan_pages,
        "duplicate_candidates": duplicate_candidates,
        "v3_coverage": {
            "reading_status": v3_reading,
            "experience_type": v3_experience,
            "origin": v3_origin,
            "layers": v3_layers,
        },
        "lint_summary": {
            "error_count": lint_res["error_count"],
            "warning_count": lint_res["warning_count"],
        },
    }


# ---------------------------------------------------------------------------
# Argument parser & CLI dispatcher
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        default=argparse.SUPPRESS,
        help="输出纯净单对象 JSON",
    )
    common.add_argument(
        "--root",
        type=str,
        default=argparse.SUPPRESS,
        help="知识库根目录路径（默认当前目录）",
    )

    parser = argparse.ArgumentParser(
        prog="kb",
        description="Personal OKF-Wiki — 可提取的模块化个人知识库引擎",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", parents=[common], help="幂等初始化目录树、Git、配置与数据库")
    sub.add_parser("scan", parents=[common], help="扫描 inbox/ 与只读 watch_folders 并防重归档至 raw/")

    p_add = sub.add_parser("add", parents=[common], help="写入随手灵感或抓取网页链接并入队 PENDING_COMPILE")
    add_group = p_add.add_mutually_exclusive_group(required=True)
    add_group.add_argument("--text", default=None, help="文本内容")
    add_group.add_argument("--url", default=None, help="网页链接（自动抓取正文转 Markdown）")
    p_add.add_argument("--title", default=None, help="可选标题")

    sub.add_parser("watch", parents=[common], help="启动单实例轻量化守护脚本")

    p_begin = sub.add_parser("compile-begin", parents=[common], help="开启编译会话并锁定待编译文档")
    p_begin.add_argument("--limit", type=int, default=10, help="单批最多锁定文档数")

    p_fail = sub.add_parser("compile-fail", parents=[common], help="毒丸隔离：标记单个文档编译失败")
    p_fail.add_argument("--session", required=True, help="当前活跃会话 ID")
    p_fail.add_argument("--hash", required=True, dest="content_hash", help="文档 content_hash")
    p_fail.add_argument("--reason", required=True, help="失败原因说明")

    p_commit = sub.add_parser("compile-commit", parents=[common], help="全量校验、重建索引并精确提交")
    p_commit.add_argument("--session", required=True, help="当前活跃会话 ID")
    p_commit.add_argument("--note", required=True, help="中文更新摘要")
    p_commit.add_argument("--allow-shrinkage", action="append", default=[], help="允许缩水的页面路径")
    p_commit.add_argument("--allow-delete", action="append", default=[], help="允许删除的页面路径")

    sub.add_parser("recover", parents=[common], help="跨系统对账或零丢失回滚恢复")

    p_retry = sub.add_parser("retry", parents=[common], help="将 FAILED 文件重置回待编译队列")
    p_retry.add_argument("--hash", dest="content_hash", default=None, help="指定文档 content_hash")
    p_retry.add_argument("--session", default=None, help="指定会话 ID")
    p_retry.add_argument("--all-failed", action="store_true", help="重试全部 FAILED 文件")

    sub.add_parser("rebuild", parents=[common], help="从 raw/ 与 wiki/ 无损重建 .kb/kb.db")
    sub.add_parser("reindex", parents=[common], help="全量重建 SQLite 索引、图谱与 wiki/index.md")

    p_refactor = sub.add_parser("refactor", parents=[common], help="确定性重构工具")
    ref_sub = p_refactor.add_subparsers(dest="refactor_action", required=True)
    p_rename = ref_sub.add_parser("rename", parents=[common], help="重命名页面并批量修复全库相对链接")
    p_rename.add_argument("old_path", help="旧相对路径（如 concepts/旧名.md）")
    p_rename.add_argument("new_path", help="新相对路径（如 concepts/新名.md）")
    p_rename.add_argument("--title", default=None, help="可选更新页面标题")

    p_merge = ref_sub.add_parser("merge", parents=[common], help="合并重复页面并批量重写全库相对链接")
    p_merge.add_argument("source_path", help="待合并的源页面相对路径（如 concepts/重复概念.md）")
    p_merge.add_argument("target_path", help="保留的目标主页面相对路径（如 concepts/主概念.md）")
    p_merge.add_argument("--delete-source", action="store_true", default=False, help="合并后直接删除源页（默认标记 deprecated + superseded_by）")

    p_query = sub.add_parser("query", parents=[common], help="执行段落+双轨/混合 + 1 跳图谱检索")
    p_query.add_argument("query_text", help="检索关键词或短语")
    p_query.add_argument("--type", dest="page_type", default=None, help="过滤页面类型")
    p_query.add_argument(
        "--mode",
        choices=["creator", "student"],
        default="creator",
        help="检索模式: creator（个人创作全库发散）| student（学员问答严选公开稳定页）",
    )
    p_query.add_argument(
        "--visibility",
        choices=["private", "team", "public"],
        default=None,
        help="可选可见性过滤（student 模式强制为 public）",
    )
    p_query.add_argument(
        "--include-deprecated",
        action="store_true",
        default=False,
        help="是否包含已废弃(deprecated)的历史页面",
    )
    p_query.add_argument(
        "--layer",
        choices=["L1", "L2", "L3", "L4", "L5", "L6"],
        default=None,
        help="按认知层级过滤（L1-L6）",
    )
    p_query.add_argument(
        "--experience-type",
        dest="experience_type",
        choices=["firsthand", "observation", "others"],
        default=None,
        help="按经验来源过滤（firsthand/observation/others）",
    )
    p_query.add_argument(
        "--reading-status",
        dest="reading_status",
        choices=["read", "partial", "unread"],
        default=None,
        help="按阅读状态过滤（read/partial/unread）",
    )
    p_query.add_argument("--l1-limit", type=int, default=None, help="L1 返回上限")
    p_query.add_argument("--offset", type=int, default=0, help="L1 翻页偏移量")
    p_query.add_argument("--top-k", type=int, default=None, help="L2 展开正文上限")

    p_lint = sub.add_parser("lint", parents=[common], help="运行 OKF v0.3 合规与安全校验")
    p_lint.add_argument("--base-commit", default=None, help="指定基准 Git Commit SHA")
    p_lint.add_argument("--allow-shrinkage", action="append", default=[], help="允许缩水的页面路径")
    p_lint.add_argument("--allow-delete", action="append", default=[], help="允许删除的页面路径")

    p_eval = sub.add_parser("eval", parents=[common], help="运行黄金测试集召回与延迟评测")
    p_eval.add_argument("--cases", default="tests/eval_cases.yaml", help="黄金测试集 YAML 路径")

    sub.add_parser("status", parents=[common], help="输出知识库健康度面板")
    sub.add_parser("audit", parents=[common], help="输出知识库治理审计报告（含学员就绪度/废弃/过时/孤儿/重复候选）")
    sub.add_parser("adapters", parents=[common], help="检查全部已启用输入适配器、支持后缀及本机解码器就绪状态")
    sub.add_parser("evolve-report", parents=[common], help="生成客观入库/召回遥测诊断报告，供架构自迭代决策")
    p_evo_mark = sub.add_parser("evolve-mark", parents=[common], help="登记本次自迭代结论并自动调整下一次退避间隔")
    p_evo_mark.add_argument(
        "--outcome",
        required=True,
        choices=["evolved", "no_gain", "model_ceiling", "skipped"],
        help="自迭代结论: evolved | no_gain | model_ceiling | skipped",
    )
    p_evo_mark.add_argument("--summary", required=True, help="本次自检/迭代结论简述")
    p_evo_mark.add_argument("--next-interval", type=int, default=None, help="可选手动覆盖下一次触发间隔篇数")

    p_export = sub.add_parser("export", parents=[common], help="Phase 3 商业化 Skill 导出（预留）")
    p_export.add_argument("--output", default="dist/skill-bundle", help="导出目录")
    p_export.add_argument("--visibility", default="public", help="可见性过滤（public/team/private）")
    p_export.add_argument("--domain", action="append", default=[], help="领域过滤（可重复指定）")
    p_export.add_argument("--dry-run", action="store_true", default=True, help="仅预览筛选结果")

    return parser


def _format_human_output(command: str, data: dict[str, Any]) -> str:
    """Render concise human-readable terminal output when --json is not set."""
    if command == "status":
        wp = data["wiki_pages"]
        rd = data["raw_documents"]
        w = data["watcher"]
        by_type = ", ".join(f"{k}:{v}" for k, v in sorted(wp["by_type"].items())) or "无"
        lines = [
            f"知识库: {data['project']} ({data['root_dir']})",
            f"Wiki 页面: {wp['total']} 篇 ({by_type}) | 断链: {wp['broken_links']}",
            (
                f"Raw 档案: {rd['total']} 份 "
                f"(待编译:{rd['pending_compile']} / 编译中:{rd['compiling']} / 已编译:{rd['compiled']} / 失败:{rd['failed']})"
            ),
            f"守护进程: {'运行中 (PID ' + str(w['pid']) + ')' if w['running'] else '未运行'}",
            f"活跃事务: {data['active_session']['session_id'] + ' [' + data['active_session']['status'] + ']' if data['active_session'] else '无'}",
        ]
        if data["recent_commits"]:
            latest = data["recent_commits"][0]
            lines.append(f"最近快照: {latest['sha'][:8]} {latest['subject']}")
        return "\n".join(lines) + "\n"

    if command == "query":
        st = data["stats"]
        lines = [
            f"查询: 「{data['query']}」 | 耗时: {data['latency_ms']}ms | "
            f"直接命中: {st['direct_hits']} | 1跳邻居: {st['graph_neighbors']} | 截断: {st['truncated']}"
        ]
        for idx, item in enumerate(data["l1_inventory"], 1):
            lines.append(
                f"  {idx}. [{item['type']}] {item['title']} ({item['rel_path']}) "
                f"[{item['hit_source']}, score={item['score']}]"
            )
            if item.get("summary"):
                lines.append(f"     摘要: {item['summary']}")
        if data["graph_neighbors"]:
            lines.append("关联邻居（1跳）:")
            for nb in data["graph_neighbors"][:10]:
                lines.append(
                    f"  - [{nb['type']}] {nb['title']} ({nb['rel_path']}) "
                    f"<- {nb['direction']} via {nb['linked_via']}"
                )
        return "\n".join(lines) + "\n"

    if command == "lint":
        status_str = "通过 (0 errors)" if data["ok"] else f"失败 ({data['error_count']} errors)"
        lines = [f"OKF 巡检结果: {status_str} | 警告: {data['warning_count']}"]
        for err in data["errors"]:
            lines.append(f"  [ERROR:{err['code']}] {err['path']}: {err['message']}")
        for warn in data["warnings"]:
            lines.append(f"  [WARN:{warn['code']}] {warn['path']}: {warn['message']}")
        return "\n".join(lines) + "\n"

    if command == "eval":
        m = data["metrics"]
        lines = [
            f"评测完成 ({data['total_cases']} 条用例): "
            f"Recall={m['recall']:.2%} | Precision={m['precision']:.2%} | "
            f"负向误报={m['negative_false_positives']}/{m['negative_cases']} | "
            f"延迟 avg={m['avg_latency_ms']}ms / max={m['max_latency_ms']}ms"
        ]
        for r in data["results"]:
            flag = "PASS" if r["passed"] else "FAIL"
            lines.append(f"  [{flag}] {r['id']} («{r['query']}», {r['latency_ms']}ms)")
        return "\n".join(lines) + "\n"

    if command == "scan":
        return (
            f"扫描完成: 新增入库 {data['ingested_count']} | 重复跳过 {data['duplicate_count']} | "
            f"未变更跳过 {data['skipped_unchanged_count']} | 清理过期暂存 {data['purged_processed_count']}\n"
        )

    if command == "compile-commit":
        return (
            f"提交成功: session={data['session_id']} | commit={data.get('commit_sha') or 'skipped'} | "
            f"已编译={data['compiled_count']} | 失败隔离={data['failed_count']}\n"
        )

    if command == "recover":
        return (
            f"恢复完成: action={data['action']} | session={data['session_id']} | "
            f"recovery_ref={data['recovery_ref']} | reset={data['reset_to_pending_count']}\n"
        )

    if command == "refactor":
        if "source_path" in data:
            return (
                f"合并完成: {data['source_path']} -> {data['target_path']} "
                f"(源页处理={data['source_action']}, 入链重写={data['rewritten_links_count']})\n"
            )
        return (
            f"重命名完成: {data['old_path']} -> {data['new_path']} | "
            f"入链重写={data['rewritten_links_count']} | 出链重写={data.get('outgoing_links_rewritten_count', 0)}\n"
        )

    if command == "audit":
        c = data["counts"]
        sr = data["student_readiness"]
        lines = [
            f"治理审计: 总计 {c['total']} 篇 (stable:{c['stable']} / draft:{c['draft']} / deprecated:{c['deprecated']})",
            f"健康指标: 断链={c['broken_links']} | 孤儿={c['orphans']} | 过期={c['stale']} | 候选关系={c['candidate_relations']}",
            f"学员就绪度: 公开稳定页={sr['public_stable_total']} (已核验:{sr['public_verified']} / 待核验:{sr['public_unverified']})",
            f"重复候选词条: {len(data['duplicate_candidates'])} 组",
        ]
        return "\n".join(lines) + "\n"

    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _emit_output(command: str, data: dict[str, Any], as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(_format_human_output(command, data))


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    as_json_hint = "--json" in raw_argv
    parser = _build_parser()

    try:
        args = parser.parse_args(raw_argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        if code != 0 and as_json_hint:
            err = KBError(ErrorCode.INVALID_ARGUMENT, "命令行参数解析失败。", exit_code=code)
            sys.stdout.write(json.dumps(err.to_dict(), ensure_ascii=False) + "\n")
        return code

    as_json = bool(getattr(args, "as_json", False) or as_json_hint)
    root_arg = getattr(args, "root", None)
    root_dir = Path(root_arg).resolve() if root_arg else Path.cwd().resolve()

    try:
        if args.command == "init":
            result = cmd_init(root_dir)
            _emit_output("init", result, as_json)
            return 0

        config = load_config(root_dir)

        if args.command == "scan":
            result = scan_once(root_dir, config, apply_debounce=False)
        elif args.command == "add":
            if args.url:
                result = add_url(root_dir, config, url=args.url, title=args.title)
            else:
                result = add_direct_text(root_dir, config, text=args.text, title=args.title)
        elif args.command == "watch":
            run_watcher(root_dir, config)
            return 0
        elif args.command == "compile-begin":
            result = cmd_compile_begin(root_dir, config, limit=args.limit)
        elif args.command == "compile-fail":
            result = cmd_compile_fail(
                root_dir,
                config,
                session_id=args.session,
                content_hash=args.content_hash,
                reason=args.reason,
            )
        elif args.command == "compile-commit":
            result = cmd_compile_commit(
                root_dir,
                config,
                session_id=args.session,
                note=args.note,
                allow_shrinkage=args.allow_shrinkage,
                allow_delete=args.allow_delete,
            )
        elif args.command == "recover":
            result = cmd_recover(root_dir, config)
        elif args.command == "retry":
            result = cmd_retry(
                root_dir,
                config,
                content_hash=args.content_hash,
                session_id=args.session,
                all_failed=args.all_failed,
            )
        elif args.command == "rebuild":
            result = rebuild_database(root_dir, config)
        elif args.command == "reindex":
            result = reindex_wiki(root_dir, config, regenerate_index=True)
        elif args.command == "refactor" and args.refactor_action == "rename":
            result = rename_wiki_page(
                root_dir,
                config,
                args.old_path,
                args.new_path,
                new_title=args.title,
            )
        elif args.command == "refactor" and args.refactor_action == "merge":
            result = merge_wiki_pages(
                root_dir,
                config,
                args.source_path,
                args.target_path,
                delete_source=args.delete_source,
            )
        elif args.command == "query":
            result = query_wiki(
                root_dir,
                config,
                args.query_text,
                page_type=args.page_type,
                mode=args.mode,
                visibility=args.visibility,
                include_deprecated=args.include_deprecated,
                layer=args.layer,
                experience_type=args.experience_type,
                reading_status=args.reading_status,
                l1_limit=args.l1_limit,
                offset=args.offset,
                top_k=args.top_k,
            )
        elif args.command == "lint":
            result = lint_wiki(
                root_dir,
                config,
                base_commit=args.base_commit,
                allow_shrinkage=args.allow_shrinkage,
                allow_delete=args.allow_delete,
            )
            _emit_output("lint", result, as_json)
            return 0 if result["ok"] else 1
        elif args.command == "eval":
            cases_p = Path(args.cases)
            if not cases_p.is_absolute():
                cases_p = root_dir / cases_p
            result = run_eval(root_dir, config, cases_p)
        elif args.command == "status":
            result = cmd_status(root_dir, config)
        elif args.command == "audit":
            result = cmd_audit(root_dir, config)
        elif args.command == "adapters":
            import os
            import shutil
            reg = build_default_registry(
                config.adapters.enabled,
                transcription_config=config.transcription,
            )
            result = {
                "ok": True,
                "enabled_adapters": reg.enabled_names,
                "supported_extensions": reg.supported_extensions,
                "system_decoders": {
                    "pdf_swift_pdfkit": bool(shutil.which("swift")),
                    "pdf_pdftotext_fallback": bool(shutil.which("pdftotext")),
                    "heic_macos_sips": bool(shutil.which("sips")),
                    "video_ffmpeg": bool(shutil.which("ffmpeg")),
                    "cloud_transcription_enabled": config.transcription.enabled,
                    "cloud_transcription_key_set": bool(os.environ.get(config.transcription.api_key_env, "").strip()),
                },
            }
        elif args.command == "evolve-report":
            result = build_evolve_report(root_dir, config)
        elif args.command == "evolve-mark":
            result = record_evolve_checkpoint(
                root_dir,
                config,
                outcome=args.outcome,
                summary=args.summary,
                next_interval=args.next_interval,
            )
        elif args.command == "export":
            result = export_skill_bundle(
                root_dir,
                Path(args.output),
                visibility=args.visibility,
                domains=args.domain or None,
                dry_run=args.dry_run,
            )
        else:
            raise KBError(ErrorCode.INVALID_ARGUMENT, f"未知命令: {args.command}")

        _emit_output(args.command, result, as_json)
        return 0

    except KBError as exc:
        if as_json:
            sys.stdout.write(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2) + "\n")
        else:
            sys.stderr.write(f"[{exc.error_code}] {exc.message}\n")
        return exc.exit_code
    except Exception as exc:
        err = KBError(ErrorCode.INTERNAL_ERROR, f"未预期内部错误: {exc}")
        if as_json:
            sys.stdout.write(json.dumps(err.to_dict(), ensure_ascii=False, indent=2) + "\n")
        else:
            sys.stderr.write(f"[{err.error_code}] {err.message}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
