"""Consistent SQLite snapshots (Connection.backup) and their verification."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_DAILY = 7
SNAPSHOT_PREFIX = "shelog-"


class BackupError(Exception):
    """A snapshot could not be written or failed verification (message has no paths' data)."""


def verify(path: Path) -> int:
    """Open a snapshot read-only, run integrity_check; return its reading count."""
    if not path.is_file():
        raise BackupError("snapshot file not found")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise BackupError("integrity_check failed")
        return int(con.execute("SELECT COUNT(*) FROM reading").fetchone()[0])
    except sqlite3.Error as err:
        raise BackupError(f"snapshot unreadable ({type(err).__name__})") from None
    finally:
        con.close()


def snapshot(src: sqlite3.Connection, out: Path) -> int:
    """Write a consistent copy of ``src`` to ``out`` atomically, verify it, return row count."""
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = out.with_name(out.name + ".tmp")
    tmp.unlink(missing_ok=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")  # single self-contained file
    finally:
        dst.close()
    os.chmod(tmp, 0o600)
    count = verify(tmp)
    tmp.replace(out)
    return count


def daily(src: sqlite3.Connection, backup_dir: Path, now: float | None = None) -> Path | None:
    """Write today's snapshot if missing and prune to KEEP_DAILY. None if already done."""
    now = time.time() if now is None else now
    day = time.strftime("%Y%m%d", time.gmtime(now))
    out = backup_dir / f"{SNAPSHOT_PREFIX}{day}.db"
    if out.exists():
        return None
    count = snapshot(src, out)
    snaps = sorted(backup_dir.glob(f"{SNAPSHOT_PREFIX}*.db"))
    for old in snaps[:-KEEP_DAILY]:
        old.unlink(missing_ok=True)
    log.info("daily backup written (%d readings, %d kept)", count, min(len(snaps), KEEP_DAILY))
    return out
