"""SQLite WAL database initialization, schema DDL, and transaction state machine."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from kb_engine.errors import ErrorCode, KBError
from kb_engine.utils import normalize_nfc, normalize_rel_path, utc_now_iso

SCHEMA_DDL = """
-- 1. 原始资料与增量状态机表（以内容 SHA256 为主键；图片存原图二进制 SHA256）
CREATE TABLE IF NOT EXISTS raw_documents (
    content_hash TEXT PRIMARY KEY,
    source_origin TEXT NOT NULL,
    origin_path TEXT,
    origin_mtime REAL,
    raw_rel_path TEXT UNIQUE NOT NULL,
    asset_rel_path TEXT,
    media_type TEXT NOT NULL,
    needs_vision INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    session_id TEXT,
    error_message TEXT,
    last_error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 2. 编译事务会话表（引入 COMMITTING 中间态支撑 Git/SQLite 跨系统对账）
CREATE TABLE IF NOT EXISTS compile_sessions (
    session_id TEXT PRIMARY KEY,
    base_commit_sha TEXT NOT NULL,
    status TEXT NOT NULL,
    recovery_ref TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

-- 3. Wiki 页面元数据与内容表
CREATE TABLE IF NOT EXISTS wiki_pages (
    rel_path TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'stable',
    domain TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'private',
    tags_json TEXT NOT NULL DEFAULT '[]',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    reading_status TEXT NOT NULL DEFAULT '',
    experience_type TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    layers_json TEXT NOT NULL DEFAULT '[]',
    mechanisms_json TEXT NOT NULL DEFAULT '[]',
    is_verified INTEGER NOT NULL DEFAULT 0,
    stale_after TEXT,
    superseded_by TEXT,
    char_count INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 4. 中文双轨全文检索虚拟表（jieba 搜索引擎模式预分词 + unicode61）
CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
    rel_path UNINDEXED,
    title_seg,
    aliases_seg,
    tags_seg,
    mechanisms_seg,
    summary_seg,
    body_seg,
    tokenize='unicode61 remove_diacritics 2'
);

-- 4B. 段落级结构表与段落级全文检索（消除长页稀释与跨段误匹配，支撑增量向量缓存）
CREATE TABLE IF NOT EXISTS wiki_sections (
    section_id TEXT PRIMARY KEY,
    rel_path TEXT NOT NULL,
    section_order INTEGER NOT NULL,
    heading TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS wiki_sections_fts USING fts5(
    section_id UNINDEXED,
    rel_path UNINDEXED,
    heading_seg,
    content_seg,
    tokenize='unicode61 remove_diacritics 2'
);

-- 4C. 段落级增量向量缓存表（仅对新增/变更段落重算 Embedding）
CREATE TABLE IF NOT EXISTS wiki_section_embeddings (
    section_id TEXT PRIMARY KEY,
    rel_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector_blob BLOB NOT NULL,
    updated_at TEXT NOT NULL
);

-- 4D. 引擎元数据表（记录向量模型版本等）
CREATE TABLE IF NOT EXISTS kb_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 5. Wiki 内部相对链接与轻量类型化关系图谱表
CREATE TABLE IF NOT EXISTS wiki_links (
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    raw_link TEXT NOT NULL,
    is_broken INTEGER NOT NULL DEFAULT 0,
    link_source TEXT NOT NULL DEFAULT 'body',
    relation_type TEXT NOT NULL DEFAULT 'mentions',
    relation_status TEXT NOT NULL DEFAULT 'confirmed',
    relation_layer TEXT NOT NULL DEFAULT '',
    relation_reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source_path, target_path, raw_link)
);

-- 6. 血缘映射表（支撑 kb rebuild）
CREATE TABLE IF NOT EXISTS provenance_map (
    raw_content_hash TEXT NOT NULL,
    wiki_rel_path TEXT NOT NULL,
    PRIMARY KEY (raw_content_hash, wiki_rel_path)
);

-- 7. 轻量查询遥测表（仅记录真实用户查询，供 kb evolve-report 诊断召回痛点）
CREATE TABLE IF NOT EXISTS query_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_text TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'creator',
    direct_hits INTEGER NOT NULL DEFAULT 0,
    strict_hits INTEGER NOT NULL DEFAULT 0,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    confidence_warning TEXT,
    top_score REAL NOT NULL DEFAULT 0.0,
    latency_ms REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL
);

-- 8. 自适应架构自迭代检查点表（记录收益、能力上限休眠与退避间隔）
CREATE TABLE IF NOT EXISTS evolution_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    compiled_raw_total INTEGER NOT NULL,
    wiki_pages_total INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    next_interval INTEGER NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_documents_state ON raw_documents(state);
CREATE INDEX IF NOT EXISTS idx_raw_documents_session ON raw_documents(session_id);
CREATE INDEX IF NOT EXISTS idx_raw_documents_origin ON raw_documents(origin_path);
CREATE INDEX IF NOT EXISTS idx_wiki_links_target ON wiki_links(target_path);
CREATE INDEX IF NOT EXISTS idx_wiki_sections_rel_path ON wiki_sections(rel_path);
CREATE INDEX IF NOT EXISTS idx_wiki_section_emb_model ON wiki_section_embeddings(model);
CREATE INDEX IF NOT EXISTS idx_query_logs_created ON query_logs(created_at);
"""


def get_db_path(root_dir: Path) -> Path:
    return root_dir / ".kb" / "kb.db"


def _ensure_migrations(conn: sqlite3.Connection) -> None:
    """Apply lightweight additive column migrations for existing .kb/kb.db files."""
    page_cols = {row["name"] for row in conn.execute("PRAGMA table_info(wiki_pages)").fetchall()}
    if page_cols:
        page_additions = [
            ("superseded_by", "TEXT"),
            ("reading_status", "TEXT NOT NULL DEFAULT ''"),
            ("experience_type", "TEXT NOT NULL DEFAULT ''"),
            ("origin", "TEXT NOT NULL DEFAULT ''"),
            ("layers_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("mechanisms_json", "TEXT NOT NULL DEFAULT '[]'"),
        ]
        for col_name, col_def in page_additions:
            if col_name not in page_cols:
                conn.execute(f"ALTER TABLE wiki_pages ADD COLUMN {col_name} {col_def}")

    link_cols = {row["name"] for row in conn.execute("PRAGMA table_info(wiki_links)").fetchall()}
    if link_cols:
        link_additions = [
            ("link_source", "TEXT NOT NULL DEFAULT 'body'"),
            ("relation_type", "TEXT NOT NULL DEFAULT 'mentions'"),
            ("relation_status", "TEXT NOT NULL DEFAULT 'confirmed'"),
            ("relation_layer", "TEXT NOT NULL DEFAULT ''"),
            ("relation_reason", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col_name, col_def in link_additions:
            if col_name not in link_cols:
                conn.execute(f"ALTER TABLE wiki_links ADD COLUMN {col_name} {col_def}")

    fts_cols = {row["name"] for row in conn.execute("PRAGMA table_info(wiki_fts)").fetchall()}
    if fts_cols and "mechanisms_seg" not in fts_cols:
        conn.execute("DROP TABLE wiki_fts")
        conn.execute(
            """
            CREATE VIRTUAL TABLE wiki_fts USING fts5(
                rel_path UNINDEXED,
                title_seg,
                aliases_seg,
                tags_seg,
                mechanisms_seg,
                summary_seg,
                body_seg,
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )


def connect_db(root_dir: Path, *, ensure_schema: bool = True) -> sqlite3.Connection:
    """Open SQLite connection with mandatory WAL mode, foreign keys & busy timeout."""
    db_path = get_db_path(root_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    if ensure_schema:
        conn.executescript(SCHEMA_DDL)
        _ensure_migrations(conn)
    return conn


@contextlib.contextmanager
def db_session(root_dir: Path, *, ensure_schema: bool = True) -> Iterator[sqlite3.Connection]:
    conn = connect_db(root_dir, ensure_schema=ensure_schema)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database(root_dir: Path, *, reset: bool = False) -> Path:
    """Initialize (or reset) .kb/kb.db with full schema."""
    db_path = get_db_path(root_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if reset:
        for suffix in ("", "-wal", "-shm"):
            p = Path(f"{db_path}{suffix}")
            with contextlib.suppress(OSError):
                p.unlink()
    with db_session(root_dir, ensure_schema=True):
        pass
    return db_path


def get_open_session(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Return any ACTIVE or COMMITTING session if present."""
    row = conn.execute(
        """
        SELECT * FROM compile_sessions
        WHERE status IN ('ACTIVE', 'COMMITTING')
        ORDER BY started_at DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def get_session_by_id(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM compile_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def upsert_raw_document(
    conn: sqlite3.Connection,
    *,
    content_hash: str,
    source_origin: str,
    origin_path: str | None,
    origin_mtime: float | None,
    raw_rel_path: str,
    asset_rel_path: str | None,
    media_type: str,
    needs_vision: bool,
    state: str = "PENDING_COMPILE",
) -> tuple[bool, dict[str, Any]]:
    """Insert new raw_document or update origin metadata if content_hash already exists.

    Returns (is_new, record_dict).
    """
    now = utc_now_iso()
    norm_origin = normalize_nfc(origin_path) if origin_path else None
    norm_raw = normalize_rel_path(raw_rel_path)
    norm_asset = normalize_rel_path(asset_rel_path) if asset_rel_path else None

    existing = conn.execute(
        "SELECT * FROM raw_documents WHERE content_hash = ?",
        (content_hash,),
    ).fetchone()
    if existing:
        if norm_origin is not None or origin_mtime is not None:
            conn.execute(
                """
                UPDATE raw_documents
                SET origin_path = COALESCE(?, origin_path),
                    origin_mtime = COALESCE(?, origin_mtime),
                    updated_at = ?
                WHERE content_hash = ?
                """,
                (norm_origin, origin_mtime, now, content_hash),
            )
        updated = conn.execute(
            "SELECT * FROM raw_documents WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        return False, dict(updated)

    conn.execute(
        """
        INSERT INTO raw_documents (
            content_hash, source_origin, origin_path, origin_mtime,
            raw_rel_path, asset_rel_path, media_type, needs_vision,
            state, session_id, error_message, last_error_message,
            retry_count, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?, ?)
        """,
        (
            content_hash,
            source_origin,
            norm_origin,
            origin_mtime,
            norm_raw,
            norm_asset,
            media_type,
            1 if needs_vision else 0,
            state,
            now,
            now,
        ),
    )
    created = conn.execute(
        "SELECT * FROM raw_documents WHERE content_hash = ?",
        (content_hash,),
    ).fetchone()
    return True, dict(created)


def mark_document_failed(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    content_hash: str,
    reason: str,
) -> dict[str, Any]:
    session = get_session_by_id(conn, session_id)
    if not session or session["status"] != "ACTIVE":
        raise KBError(
            ErrorCode.SESSION_NOT_FOUND,
            f"未找到活跃的编译会话: {session_id}",
            details={"session_id": session_id},
        )
    row = conn.execute(
        "SELECT * FROM raw_documents WHERE content_hash = ? AND session_id = ? AND state = 'COMPILING'",
        (content_hash, session_id),
    ).fetchone()
    if not row:
        raise KBError(
            ErrorCode.FILE_NOT_FOUND,
            f"会话 {session_id} 中未找到处于 COMPILING 状态的文档: {content_hash}",
            details={"session_id": session_id, "content_hash": content_hash},
        )
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE raw_documents
        SET state = 'FAILED',
            error_message = ?,
            last_error_message = ?,
            retry_count = retry_count + 1,
            updated_at = ?
        WHERE content_hash = ?
        """,
        (reason, reason, now, content_hash),
    )
    updated = conn.execute(
        "SELECT * FROM raw_documents WHERE content_hash = ?",
        (content_hash,),
    ).fetchone()
    return dict(updated)


def retry_failed_documents(
    conn: sqlite3.Connection,
    *,
    content_hash: str | None = None,
    session_id: str | None = None,
    all_failed: bool = False,
) -> list[dict[str, Any]]:
    if not any([content_hash, session_id, all_failed]):
        raise KBError(
            ErrorCode.INVALID_ARGUMENT,
            "请至少指定 --hash、--session 或 --all-failed 之一。",
        )
    clauses = ["state = 'FAILED'"]
    params: list[Any] = []
    if content_hash:
        clauses.append("content_hash = ?")
        params.append(content_hash)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)

    where_sql = " AND ".join(clauses)
    rows = conn.execute(f"SELECT * FROM raw_documents WHERE {where_sql}", params).fetchall()
    if not rows:
        return []

    now = utc_now_iso()
    hashes = [r["content_hash"] for r in rows]
    placeholders = ",".join("?" for _ in hashes)
    conn.execute(
        f"""
        UPDATE raw_documents
        SET state = 'PENDING_COMPILE',
            session_id = NULL,
            error_message = NULL,
            updated_at = ?
        WHERE content_hash IN ({placeholders})
        """,
        [now, *hashes],
    )
    updated = conn.execute(
        f"SELECT * FROM raw_documents WHERE content_hash IN ({placeholders})",
        hashes,
    ).fetchall()
    return [dict(r) for r in updated]
