# ruff: noqa: S608  -- SQL is built only from the module-level column constants below.
"""Merge another shelog database (e.g. the interim local run) into this one.

Idempotent: readings/raw_status are keyed by (device_id, ts, source) and never overwritten;
poll_log rows are skipped when a row with the same (kind, ts) already exists. Everything
happens in one transaction. Reports counts only.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ImportResult:
    readings: int
    raw_status: int
    poll_log: int
    skipped_readings: int


_POLL_COLS = (
    "ts, kind, ok, http_status, error, n_devices, n_new, n_unchanged, n_rejected,"
    " retry_after_s, latency_ms"
)
_READING_COLS = "device_id, ts, ts_source, t_c, rh, battery, flood, polled_at, source"


def import_db(dst: sqlite3.Connection, src_path: Path) -> ImportResult:
    if not src_path.is_file():
        raise FileNotFoundError("source database not found")
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        polls = src.execute(f"SELECT id, {_POLL_COLS} FROM poll_log ORDER BY id").fetchall()
        readings = src.execute(f"SELECT poll_id, {_READING_COLS} FROM reading").fetchall()
        raws = src.execute("SELECT device_id, ts, source, body FROM raw_status").fetchall()
    finally:
        src.close()

    dst.execute("BEGIN IMMEDIATE")
    try:
        existing = {
            (kind, ts): pid for pid, kind, ts in dst.execute("SELECT id, kind, ts FROM poll_log")
        }
        id_map: dict[int, int] = {}
        n_poll = 0
        for old_id, *cols in polls:
            key = (cols[1], cols[0])
            if key in existing:
                id_map[old_id] = existing[key]
                continue
            cur = dst.execute(
                f"INSERT INTO poll_log({_POLL_COLS}) VALUES ({','.join('?' * len(cols))})", cols
            )
            id_map[old_id] = int(cur.lastrowid)
            existing[key] = id_map[old_id]
            n_poll += 1
        n_read = 0
        for poll_id, *cols in readings:
            cur = dst.execute(
                f"INSERT OR IGNORE INTO reading({_READING_COLS}, poll_id)"
                f" VALUES ({','.join('?' * len(cols))}, ?)",
                (*cols, id_map.get(poll_id)),
            )
            n_read += cur.rowcount
        n_raw = 0
        for row in raws:
            cur = dst.execute(
                "INSERT OR IGNORE INTO raw_status(device_id, ts, source, body) VALUES (?,?,?,?)",
                row,
            )
            n_raw += cur.rowcount
        dst.execute("COMMIT")
    except BaseException:
        if dst.in_transaction:
            dst.execute("ROLLBACK")
        raise
    return ImportResult(n_read, n_raw, n_poll, len(readings) - n_read)
