"""SQLite access and migrations.

One connection per unit of work. SQLite in WAL mode copes fine with many
readers and a single writer, which is exactly this application's shape, and it
avoids every thread-affinity problem a shared connection would bring.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app import config

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_db_path: Path | None = None


def set_db_path(path: Path | str) -> None:
    """Point the module at a database file. Used at startup and by tests."""
    global _db_path
    _db_path = Path(path)


def db_path() -> Path:
    return _db_path if _db_path is not None else config.DB_PATH


def _connect() -> sqlite3.Connection:
    path = db_path()
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """A connection wrapped in one transaction."""
    conn = _connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def readonly() -> Iterator[sqlite3.Connection]:
    """A connection for reads, with no surrounding transaction."""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    with readonly() as conn:
        return list(conn.execute(sql, params))


def query_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    with readonly() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    with session() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid if cur.lastrowid is not None else cur.rowcount


def migrate() -> list[str]:
    """Apply any unapplied migrations in filename order. Idempotent.

    Deliberately not wrapped in :func:`session`. ``executescript`` commits any
    open transaction before it runs, so an outer BEGIN would be closed under us
    and the matching COMMIT would then fail. Instead the BEGIN/COMMIT are put
    *inside* the script, together with the bookkeeping insert, which makes each
    migration atomic: either the schema change and its record both land, or
    neither does.
    """
    applied: list[str] = []
    conn = _connect()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration ("
            " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migration")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            # executescript cannot bind parameters, so the name is inlined --
            # hence the check that it cannot contain anything but a filename.
            if not re.fullmatch(r"[A-Za-z0-9_.\-]+", path.name):
                raise ValueError(f"unsafe migration filename: {path.name!r}")
            body = path.read_text(encoding="utf-8").strip()
            if not body.endswith(";"):
                body += ";"
            conn.executescript(
                "BEGIN;\n"
                f"{body}\n"
                f"INSERT INTO schema_migration (name) VALUES ('{path.name}');\n"
                "COMMIT;"
            )
            applied.append(path.name)
            log.info("migration applied", extra={"migration": path.name})
    finally:
        conn.close()
    return applied


def bump_counter(name: str, by: int = 1) -> None:
    execute(
        "INSERT INTO counter (name, value) VALUES (?, ?) "
        "ON CONFLICT (name) DO UPDATE SET value = value + excluded.value",
        (name, by),
    )


def get_counter(name: str) -> int:
    row = query_one("SELECT value FROM counter WHERE name = ?", (name,))
    return int(row["value"]) if row else 0
