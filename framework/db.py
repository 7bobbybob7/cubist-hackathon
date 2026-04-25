"""SQLite connection helpers and schema initialization."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")


def _migrate_add_column(
    conn: sqlite3.Connection, table: str, column: str, decl: str,
) -> None:
    """ALTER TABLE ADD COLUMN if the column doesn't already exist."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    cols = {r["name"] if isinstance(r, sqlite3.Row) else r[1] for r in rows}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(db_path: str | Path) -> None:
    """Create the database file and apply the schema. Idempotent.

    Also runs additive migrations for older DBs that pre-date a column.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_PATH.read_text()
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        _configure(conn)
        conn.executescript(schema_sql)
        # Phase 6 migration — safe on fresh DBs (column already exists)
        # and safe on pre-Phase-6 DBs (column gets added).
        _migrate_add_column(conn, "tasks", "archived_at", "TEXT")
        conn.commit()
    finally:
        conn.close()


class Database:
    """Per-thread SQLite connection holder.

    SQLite connections are not safe to share across threads, so each thread
    gets its own connection lazily.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path,
                timeout=30.0,
                isolation_level=None,  # autocommit; we manage transactions manually
            )
            _configure(conn)
            self._local.conn = conn
        return conn

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self._conn().execute(sql, params)

    def executemany(self, sql: str, seq) -> sqlite3.Cursor:
        return self._conn().executemany(sql, seq)

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    @contextmanager
    def transaction(self, mode: str = "IMMEDIATE") -> Iterator[sqlite3.Connection]:
        """Open a transaction. mode is DEFERRED, IMMEDIATE, or EXCLUSIVE."""
        conn = self._conn()
        conn.execute(f"BEGIN {mode}")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
