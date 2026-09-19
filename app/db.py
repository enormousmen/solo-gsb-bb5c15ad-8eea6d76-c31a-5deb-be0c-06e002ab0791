"""SQLite 持久化层：连接、建表与文档/修订/幂等请求的读写。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id               TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    document_id   TEXT    NOT NULL REFERENCES documents(id),
    revision      INTEGER NOT NULL,
    content       TEXT    NOT NULL,
    patch         TEXT,
    base_revision INTEGER,
    request_id    TEXT,
    replayed      INTEGER NOT NULL DEFAULT 0,
    author        TEXT,
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (document_id, revision)
);

CREATE TABLE IF NOT EXISTS requests (
    document_id TEXT    NOT NULL REFERENCES documents(id),
    request_id  TEXT    NOT NULL,
    fingerprint TEXT    NOT NULL,
    revision    INTEGER NOT NULL,
    changed     INTEGER NOT NULL,
    PRIMARY KEY (document_id, request_id)
);

CREATE TABLE IF NOT EXISTS contracts (
    document_id TEXT PRIMARY KEY REFERENCES documents(id),
    schema_json TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical(value: Any) -> str:
    """确定性 JSON 序列化：紧凑、键排序，用于幂等指纹与内容比较。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即获取写锁的事务，避免并发写入时的 deferred-lock 死锁。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def init_db() -> None:
    """启动时调用：确保目录存在并自动建表（幂等）。"""
    parent = os.path.dirname(os.path.abspath(settings.db_path))
    os.makedirs(parent, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------- 读取 ----------

def get_document(conn: sqlite3.Connection, doc_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()


def get_revision(
    conn: sqlite3.Connection, doc_id: str, revision: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM revisions WHERE document_id = ? AND revision = ?",
        (doc_id, revision),
    ).fetchone()


def list_revisions(
    conn: sqlite3.Connection,
    doc_id: str,
    limit: int,
    offset: int,
    desc: bool = False,
) -> list[sqlite3.Row]:
    order = "DESC" if desc else "ASC"
    return conn.execute(
        f"""SELECT revision, base_revision, request_id, replayed, author, created_at
            FROM revisions
            WHERE document_id = ?
            ORDER BY revision {order}
            LIMIT ? OFFSET ?""",
        (doc_id, limit, offset),
    ).fetchall()


def get_request(
    conn: sqlite3.Connection, doc_id: str, request_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM requests WHERE document_id = ? AND request_id = ?",
        (doc_id, request_id),
    ).fetchone()


# ---------- 写入 ----------

def insert_document(
    conn: sqlite3.Connection, doc_id: str, content: Any, author: Optional[str], now: str
) -> int:
    """调用方需已开启写事务。插入文档与其修订 1，返回修订号。"""
    conn.execute(
        "INSERT INTO documents (id, current_revision, created_at, updated_at)"
        " VALUES (?, 1, ?, ?)",
        (doc_id, now, now),
    )
    conn.execute(
        """INSERT INTO revisions
               (document_id, revision, content, patch, base_revision,
                request_id, replayed, author, created_at)
           VALUES (?, 1, ?, NULL, NULL, NULL, 0, ?, ?)""",
        (doc_id, canonical(content), author, now),
    )
    return 1


def insert_revision(
    conn: sqlite3.Connection,
    doc_id: str,
    revision: int,
    content: Any,
    patch: Any,
    base_revision: int,
    request_id: str,
    replayed: bool,
    author: Optional[str],
    now: str,
) -> None:
    conn.execute(
        "UPDATE documents SET current_revision = ?, updated_at = ? WHERE id = ?",
        (revision, now, doc_id),
    )
    conn.execute(
        """INSERT INTO revisions
               (document_id, revision, content, patch, base_revision,
                request_id, replayed, author, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            doc_id,
            revision,
            canonical(content),
            canonical(patch),
            base_revision,
            request_id,
            1 if replayed else 0,
            author,
            now,
        ),
    )


# ---------- 契约（JSON Schema 2020-12） ----------


def get_contract(conn: sqlite3.Connection, doc_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM contracts WHERE document_id = ?", (doc_id,)
    ).fetchone()


def upsert_contract(
    conn: sqlite3.Connection, doc_id: str, schema: Any, now: str
) -> None:
    """调用方需已开启写事务，且 *schema* 已通过自检并验证过当前文档。"""
    conn.execute(
        "INSERT INTO contracts (document_id, schema_json, updated_at)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(document_id) DO UPDATE SET"
        "   schema_json = excluded.schema_json, updated_at = excluded.updated_at",
        (doc_id, canonical(schema), now),
    )


def delete_contract(conn: sqlite3.Connection, doc_id: str) -> None:
    """调用方需已开启写事务。无契约时为 no-op。"""
    conn.execute("DELETE FROM contracts WHERE document_id = ?", (doc_id,))


def insert_request(
    conn: sqlite3.Connection,
    doc_id: str,
    request_id: str,
    fingerprint: str,
    revision: int,
    changed: bool,
) -> None:
    conn.execute(
        "INSERT INTO requests (document_id, request_id, fingerprint, revision, changed)"
        " VALUES (?, ?, ?, ?, ?)",
        (doc_id, request_id, fingerprint, revision, 1 if changed else 0),
    )
