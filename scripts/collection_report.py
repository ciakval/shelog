"""Aggregate-only acceptance report over the collector database (Phase 1 SC3).

Usage: uv run python scripts/collection_report.py --db PATH --since UNIX_TS --expect-devices N

Opens the database read-only and prints one line per check as ``name | value | verdict``,
then a final verdict. Only counts and ratios are printed: never device ids, raw JSON, the
host or the key. Exit status 0 when every check passes, 1 otherwise, 2 when the database
cannot be opened. Reused in Phase 2 to verify the production database.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

PASS = "PASS"  # noqa: S105  # verdict label, not a credential
FAIL = "FAIL"
INFO = "info"

MIN_OK_RATIO = 0.9
MEAN_GAP_RANGE_S = (50.0, 70.0)
EARLIEST_PLAUSIBLE_TS = 1577836800  # 2020-01-01T00:00:00Z
FUTURE_SLACK_S = 120

Q_ATTEMPTS = "SELECT count(*) FROM poll_log WHERE kind = 'shelly' AND ts >= ?"
Q_OK = "SELECT count(*) FROM poll_log WHERE kind = 'shelly' AND ts >= ? AND ok = 1"
Q_GAPS = (
    "SELECT avg(gap), sum(gap < 1) FROM ("
    " SELECT ts - lag(ts) OVER (ORDER BY id) AS gap FROM poll_log"
    " WHERE kind = 'shelly' AND ts >= ?)"
)
Q_UNCATEGORISED = (
    "SELECT count(*) FROM poll_log"
    " WHERE kind = 'shelly' AND ts >= ? AND ok = 0 AND (error IS NULL OR error = '')"
)
Q_REJECTED = "SELECT coalesce(sum(n_rejected), 0) FROM poll_log WHERE kind = 'shelly' AND ts >= ?"
Q_DUPLICATES = (
    "SELECT count(*) FROM (SELECT 1 FROM reading GROUP BY device_id, ts HAVING count(*) > 1)"
)


@dataclass(frozen=True)
class Check:
    name: str
    value: str
    verdict: str  # PASS | FAIL | INFO


def _open_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{urllib.parse.quote(str(path.resolve()))}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def _scalar(con: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> object:
    return con.execute(sql, params).fetchone()[0]


def _verdict(passed: bool) -> str:
    return PASS if passed else FAIL


def compute_checks(
    con: sqlite3.Connection,
    *,
    since: int,
    expect_devices: int,
    now: float,
    min_attempts: int = 2,
) -> list[Check]:
    checks: list[Check] = []
    attempts = int(_scalar(con, Q_ATTEMPTS, (since,)))
    checks.append(Check("attempts", str(attempts), _verdict(attempts >= min_attempts)))

    ok_count = int(_scalar(con, Q_OK, (since,)))
    ratio = ok_count / attempts if attempts else 0.0
    checks.append(
        Check("ok_ratio", f"{ratio:.3f}", _verdict(attempts > 0 and ratio >= MIN_OK_RATIO))
    )

    mean_gap, same_second = con.execute(Q_GAPS, (since,)).fetchone()
    lo, hi = MEAN_GAP_RANGE_S
    if mean_gap is None:
        checks.append(Check("mean_gap_s", "n/a", FAIL))
    else:
        checks.append(Check("mean_gap_s", f"{mean_gap:.1f}", _verdict(lo <= mean_gap <= hi)))
    checks.append(Check("same_second_pairs", str(int(same_second or 0)), _verdict(not same_second)))

    uncategorised = int(_scalar(con, Q_UNCATEGORISED, (since,)))
    checks.append(
        Check("failed_without_category", str(uncategorised), _verdict(uncategorised == 0))
    )

    n_rejected = int(_scalar(con, Q_REJECTED, (since,)))
    checks.append(Check("n_rejected_sum", str(n_rejected), INFO))

    devices = int(_scalar(con, "SELECT count(DISTINCT device_id) FROM reading"))
    checks.append(Check("distinct_devices", str(devices), _verdict(devices == expect_devices)))

    implausible = int(
        _scalar(
            con,
            "SELECT count(*) FROM reading WHERE ts < ? OR ts > ?",
            (EARLIEST_PLAUSIBLE_TS, int(now) + FUTURE_SLACK_S),
        )
    )
    checks.append(Check("implausible_ts", str(implausible), _verdict(implausible == 0)))

    duplicates = int(
        _scalar(
            con,
            Q_DUPLICATES,
        )
    )
    checks.append(Check("duplicate_device_ts", str(duplicates), _verdict(duplicates == 0)))

    readings = int(_scalar(con, "SELECT count(*) FROM reading"))
    raw = int(_scalar(con, "SELECT count(*) FROM raw_status"))
    checks.append(Check("readings_vs_raw_status", f"{readings}/{raw}", _verdict(readings == raw)))

    not_cloud = int(_scalar(con, "SELECT count(*) FROM reading WHERE ts_source != 'cloud'"))
    checks.append(Check("ts_source_not_cloud", str(not_cloud), INFO))
    return checks


def render(checks: Sequence[Check]) -> tuple[str, bool]:
    lines = [f"{c.name} | {c.value} | {c.verdict}" for c in checks]
    passed = all(c.verdict != FAIL for c in checks)
    lines.append(f"verdict | {'all checks pass' if passed else 'FAILED'} | {_verdict(passed)}")
    return "\n".join(lines), passed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collection_report",
        description="Aggregate-only acceptance report over the collector database.",
    )
    parser.add_argument("--db", type=Path, required=True, help="path to the SQLite database")
    parser.add_argument(
        "--since",
        type=int,
        required=True,
        help="unix time; only poll_log rows at or after it count",
    )
    parser.add_argument(
        "--expect-devices", type=int, required=True, help="number of distinct devices expected"
    )
    parser.add_argument(
        "--min-attempts", type=int, default=2, help="minimum poll_log attempts in the window"
    )
    parser.add_argument("--now", type=float, default=None, help="override the current unix time")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.db.is_file():
        print("database not found", file=sys.stderr)
        return 2
    try:
        con = _open_ro(args.db)
        try:
            checks = compute_checks(
                con,
                since=args.since,
                expect_devices=args.expect_devices,
                now=args.now if args.now is not None else time.time(),
                min_attempts=args.min_attempts,
            )
        finally:
            con.close()
    except sqlite3.Error as err:
        print(f"database error: {type(err).__name__}", file=sys.stderr)
        return 2
    text, passed = render(checks)
    print(text)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
