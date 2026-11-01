"""Dedup decisions and the single-transaction cycle writer."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from shelog.shelly.parse import TS_SOURCE_POLL, Report

MIN_PLAUSIBLE_TS = 1_577_836_800  # 2020-01-01: earlier values are garbage (e.g. 0)
MAX_FUTURE_SKEW_S = 120

INSERT = "insert"
UNCHANGED = "unchanged"
REGRESSED = "regressed"
IMPLAUSIBLE = "implausible"


def plausible_ts(ts: int, now: int) -> bool:
    return MIN_PLAUSIBLE_TS <= ts <= now + MAX_FUTURE_SKEW_S


@dataclass(frozen=True)
class LastState:
    ts: int
    t_c: float | None
    rh: float | None
    battery: float | None
    flood: int | None


@dataclass(frozen=True)
class Decision:
    action: str
    ts: int | None


def decide(last: LastState | None, report: Report, now: int) -> Decision:
    """Insert only when the report time advanced; never reject a report for being old.

    A report without a cloud time (ts_source 'poll') is stamped with `now` and inserted only
    when its values differ from the last stored row.
    """
    if report.ts_source == TS_SOURCE_POLL:
        if last is None or _values(last) != _values(report):
            return Decision(INSERT, now)
        return Decision(UNCHANGED, now)
    ts = report.ts
    if ts is None or not plausible_ts(ts, now):
        return Decision(IMPLAUSIBLE, None)
    if last is None or ts > last.ts:
        return Decision(INSERT, ts)
    if ts == last.ts:
        return Decision(UNCHANGED, ts)
    return Decision(REGRESSED, ts)


def _values(state: LastState | Report) -> tuple[float | int | None, ...]:
    return (state.t_c, state.rh, state.battery, state.flood)


@dataclass(frozen=True)
class ReadingRow:
    device_id: str
    ts: int
    ts_source: str
    t_c: float | None
    rh: float | None
    battery: float | None
    flood: int | None
    raw: str

    @classmethod
    def from_report(cls, report: Report, ts: int) -> ReadingRow:
        return cls(
            report.device_id,
            ts,
            report.ts_source,
            report.t_c,
            report.rh,
            report.battery,
            report.flood,
            report.raw,
        )


@dataclass
class PollRecord:
    ts: int
    ok: int
    http_status: int | None
    error: str | None
    n_devices: int | None
    n_new: int | None
    n_unchanged: int | None
    n_rejected: int | None
    retry_after_s: int | None
    latency_ms: int | None
    kind: str = "shelly"


def load_last_states(con: sqlite3.Connection) -> dict[str, LastState]:
    """Latest stored poll reading per device."""
    rows = con.execute(
        "SELECT r.device_id, r.ts, r.t_c, r.rh, r.battery, r.flood FROM reading r"
        " JOIN (SELECT device_id, MAX(ts) AS ts FROM reading WHERE source = 'poll'"
        "       GROUP BY device_id) m ON m.device_id = r.device_id AND m.ts = r.ts"
        " WHERE r.source = 'poll'"
    ).fetchall()
    return {row[0]: LastState(*row[1:]) for row in rows}


def write_cycle(con: sqlite3.Connection, poll: PollRecord, rows: Sequence[ReadingRow]) -> int:
    """Write the poll_log row, new readings and their raw JSON in one transaction.

    Returns the number of readings actually inserted. A row the primary key rejects is
    counted as unchanged in poll_log, never dropped silently.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        poll_id = con.execute(
            "INSERT INTO poll_log(ts, kind, ok, http_status, error, n_devices, n_new,"
            " n_unchanged, n_rejected, retry_after_s, latency_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                poll.ts,
                poll.kind,
                poll.ok,
                poll.http_status,
                poll.error,
                poll.n_devices,
                poll.n_new,
                poll.n_unchanged,
                poll.n_rejected,
                poll.retry_after_s,
                poll.latency_ms,
            ),
        ).lastrowid
        inserted = 0
        for row in rows:
            cur = con.execute(
                "INSERT INTO reading(device_id, ts, ts_source, t_c, rh, battery, flood,"
                " polled_at, poll_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT DO NOTHING",
                (
                    row.device_id,
                    row.ts,
                    row.ts_source,
                    row.t_c,
                    row.rh,
                    row.battery,
                    row.flood,
                    poll.ts,
                    poll_id,
                ),
            )
            if cur.rowcount == 1:
                inserted += 1
                con.execute(
                    "INSERT INTO raw_status(device_id, ts, body) VALUES (?, ?, ?)"
                    " ON CONFLICT DO NOTHING",
                    (row.device_id, row.ts, row.raw),
                )
        if poll.ok:
            unchanged = (poll.n_unchanged or 0) + (len(rows) - inserted)
            con.execute(
                "UPDATE poll_log SET n_new = ?, n_unchanged = ? WHERE id = ?",
                (inserted, unchanged, poll_id),
            )
            poll.n_new = inserted
            poll.n_unchanged = unchanged
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return inserted
