"""SQLite connection setup and the ordered package-migration runner."""

from __future__ import annotations

import re
import sqlite3
from importlib import resources
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(\d{4})_[A-Za-z0-9_]+\.sql$")


class SchemaTooNew(Exception):
    """The database was written by a newer version of the app than this one."""


def connect_rw(path: Path) -> sqlite3.Connection:
    """Open the read-write connection (single writer). Autocommit; explicit transactions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(path, isolation_level=None, timeout=5)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA busy_timeout = 5000")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _migrations() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for entry in (resources.files("shelog") / "migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.read_text(encoding="utf-8")))
    found.sort()
    return found


def migrate(con: sqlite3.Connection) -> int:
    """Apply pending migrations in order; return the resulting user_version."""
    migrations = _migrations()
    highest = migrations[-1][0] if migrations else 0
    current = int(con.execute("PRAGMA user_version").fetchone()[0])
    if current > highest:
        raise SchemaTooNew(f"database schema version {current} is newer than known {highest}")
    for number, sql in migrations:
        if number <= current:
            continue
        try:
            con.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        except Exception:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        current = number
    return current


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert one meta row in its own transaction."""
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise


def get_meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])
