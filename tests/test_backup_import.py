"""Snapshots verify; import merges idempotently and never overwrites."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from shelog import backup, importer
from shelog.db import connect_rw, migrate


def _db(path: Path, rows: list[tuple[str, int, float]], poll_ts: list[int]) -> sqlite3.Connection:
    con = connect_rw(path)
    migrate(con)
    for ts in poll_ts:
        con.execute("INSERT INTO poll_log(ts, kind, ok) VALUES (?, 'shelly', 1)", (ts,))
    pid = con.execute("SELECT MAX(id) FROM poll_log").fetchone()[0]
    for dev, ts, t in rows:
        con.execute(
            "INSERT INTO reading(device_id, ts, ts_source, t_c, polled_at, poll_id)"
            " VALUES (?, ?, 'cloud', ?, ?, ?)",
            (dev, ts, t, ts, pid),
        )
        con.execute("INSERT INTO raw_status(device_id, ts, body) VALUES (?, ?, '{}')", (dev, ts))
    return con


def test_snapshot_and_verify(tmp_path: Path):
    con = _db(tmp_path / "a.db", [("aa0000000001", 1_800_000_000, 20.0)], [1_800_000_000])
    out = tmp_path / "bk" / "snap.db"
    assert backup.snapshot(con, out) == 1
    assert backup.verify(out) == 1
    assert (out.stat().st_mode & 0o777) == 0o600
    con.close()
    out.write_bytes(b"garbage" * 100)
    try:
        backup.verify(out)
    except backup.BackupError:
        pass
    else:  # pragma: no cover
        raise AssertionError("garbage verified")


def test_daily_rotation(tmp_path: Path):
    con = _db(tmp_path / "a.db", [], [])
    for day in range(10):
        assert backup.daily(con, tmp_path / "bk", now=1_800_000_000 + day * 86400) is not None
    assert backup.daily(con, tmp_path / "bk", now=1_800_000_000 + 9 * 86400) is None
    con.close()
    assert len(list((tmp_path / "bk").glob("shelog-*.db"))) == backup.KEEP_DAILY


def test_import_is_idempotent_and_never_overwrites(tmp_path: Path):
    src = _db(
        tmp_path / "src.db",
        [("aa0000000001", 100_000_100, 1.0), ("aa0000000001", 100_000_200, 2.0)],
        [100_000_100, 100_000_200],
    )
    src.close()
    dst = _db(tmp_path / "dst.db", [("aa0000000001", 100_000_200, 99.0)], [100_000_300])
    first = importer.import_db(dst, tmp_path / "src.db")
    assert (first.readings, first.poll_log, first.skipped_readings) == (1, 2, 1)
    again = importer.import_db(dst, tmp_path / "src.db")
    assert (again.readings, again.raw_status, again.poll_log) == (0, 0, 0)
    rows = dst.execute("SELECT ts, t_c FROM reading ORDER BY ts").fetchall()
    assert rows == [(100_000_100, 1.0), (100_000_200, 99.0)]
    assert dst.execute("SELECT COUNT(*) FROM poll_log").fetchone()[0] == 3
    orphan = dst.execute(
        "SELECT COUNT(*) FROM reading WHERE poll_id NOT IN (SELECT id FROM poll_log)"
    ).fetchone()[0]
    dst.close()
    assert orphan == 0
