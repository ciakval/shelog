"""The acceptance report passes a healthy synthetic database, fails planted defects, and
never prints a device id."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collection_report.py"
NOW = 1_800_000_000
SINCE = NOW - 3600
IDS = ("020000000001", "020000000002", "020000000003")


@pytest.fixture(scope="module")
def report():
    spec = importlib.util.spec_from_file_location("collection_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["collection_report"] = module
    spec.loader.exec_module(module)
    return module


def _poll(con: sqlite3.Connection, ts: int, ok: int = 1, error: str | None = None) -> None:
    con.execute(
        "INSERT INTO poll_log (ts, kind, ok, http_status, error, n_rejected) "
        "VALUES (?, 'shelly', ?, 200, ?, 0)",
        (ts, ok, error),
    )


def _reading(
    con: sqlite3.Connection, device: str, ts: int, source: str = "poll", ts_source="cloud"
):
    con.execute(
        "INSERT INTO reading (device_id, ts, ts_source, t_c, rh, polled_at, source) "
        "VALUES (?, ?, ?, 20.0, 50.0, ?, ?)",
        (device, ts, ts_source, ts, source),
    )
    con.execute(
        "INSERT OR IGNORE INTO raw_status (device_id, ts, source, body) VALUES (?, ?, ?, '{}')",
        (device, ts, source),
    )


def _healthy(con: sqlite3.Connection) -> None:
    for i in range(60):
        _poll(con, SINCE + i * 60)
    for n, device in enumerate(IDS):
        for i in range(3):
            _reading(con, device, SINCE + 100 + n * 7 + i * 900)
    con.commit()


def _run(report, db_path: Path, capsys, devices: int = 3, **extra: str) -> tuple[int, str]:
    argv = ["--db", str(db_path), "--since", str(SINCE), "--expect-devices", str(devices)]
    argv += ["--now", str(NOW)]
    for key, value in extra.items():
        argv += [f"--{key.replace('_', '-')}", value]
    code = report.main(argv)
    return code, capsys.readouterr().out


def _lines(text: str) -> dict[str, tuple[str, str]]:
    out = {}
    for line in text.splitlines():
        name, value, verdict = (part.strip() for part in line.split("|"))
        out[name] = (value, verdict)
    return out


def test_healthy_database_passes_every_check(report, tmp_db, tmp_path, capsys):
    _healthy(tmp_db)
    code, text = _run(report, tmp_path / "data" / "shelog.db", capsys)
    assert code == 0
    rows = _lines(text)
    assert rows["attempts"] == ("60", "PASS")
    assert rows["ok_ratio"] == ("1.000", "PASS")
    assert rows["mean_gap_s"] == ("60.0", "PASS")
    assert rows["same_second_pairs"][1] == "PASS"
    assert rows["distinct_devices"] == ("3", "PASS")
    assert rows["readings_vs_raw_status"] == ("9/9", "PASS")
    assert rows["ts_source_not_cloud"] == ("0", "info")
    assert rows["verdict"][1] == "PASS"
    for device in IDS:
        assert device not in text


def test_two_requests_in_the_same_second_fail(report, tmp_db, tmp_path, capsys):
    _healthy(tmp_db)
    tmp_db.execute("UPDATE poll_log SET ts = (SELECT ts FROM poll_log WHERE id = 1) WHERE id = 2")
    tmp_db.commit()
    code, text = _run(report, tmp_path / "data" / "shelog.db", capsys)
    assert code == 1
    rows = _lines(text)
    assert rows["same_second_pairs"][1] == "FAIL"
    assert rows["verdict"][1] == "FAIL"


def test_failed_attempt_without_category_fails(report, tmp_db, tmp_path, capsys):
    _healthy(tmp_db)
    _poll(tmp_db, SINCE + 3600 - 30, ok=0, error=None)
    tmp_db.commit()
    code, text = _run(report, tmp_path / "data" / "shelog.db", capsys)
    assert code == 1
    assert _lines(text)["failed_without_category"] == ("1", "FAIL")


def test_low_ok_ratio_fails(report, tmp_db, tmp_path, capsys):
    _healthy(tmp_db)
    tmp_db.execute("UPDATE poll_log SET ok = 0, error = 'network' WHERE id <= 10")
    tmp_db.commit()
    code, text = _run(report, tmp_path / "data" / "shelog.db", capsys)
    assert code == 1
    assert _lines(text)["ok_ratio"][1] == "FAIL"


def test_wrong_device_count_fails(report, tmp_db, tmp_path, capsys):
    _healthy(tmp_db)
    code, text = _run(report, tmp_path / "data" / "shelog.db", capsys, devices=7)
    assert code == 1
    assert _lines(text)["distinct_devices"] == ("3", "FAIL")


def test_implausible_timestamps_fail(report, tmp_db, tmp_path, capsys):
    _healthy(tmp_db)
    _reading(tmp_db, IDS[0], 5)
    _reading(tmp_db, IDS[1], NOW + 10_000)
    tmp_db.commit()
    code, text = _run(report, tmp_path / "data" / "shelog.db", capsys)
    assert code == 1
    assert _lines(text)["implausible_ts"] == ("2", "FAIL")


def test_duplicate_device_ts_across_sources_fails(report, tmp_db, tmp_path, capsys):
    _healthy(tmp_db)
    ts = SINCE + 100
    _reading(tmp_db, IDS[0], ts, source="cloud_stats")
    tmp_db.commit()
    code, text = _run(report, tmp_path / "data" / "shelog.db", capsys)
    assert code == 1
    assert _lines(text)["duplicate_device_ts"] == ("1", "FAIL")


def test_reading_without_raw_status_fails(report, tmp_db, tmp_path, capsys):
    _healthy(tmp_db)
    tmp_db.execute(
        "DELETE FROM raw_status WHERE (device_id, ts) = "
        "(SELECT device_id, ts FROM raw_status ORDER BY ts LIMIT 1)"
    )
    tmp_db.commit()
    code, text = _run(report, tmp_path / "data" / "shelog.db", capsys)
    assert code == 1
    assert _lines(text)["readings_vs_raw_status"] == ("9/8", "FAIL")


def test_non_cloud_ts_source_is_reported_but_not_failed(report, tmp_db, tmp_path, capsys):
    _healthy(tmp_db)
    _reading(tmp_db, IDS[0], SINCE + 2000, ts_source="poll")
    tmp_db.commit()
    code, text = _run(report, tmp_path / "data" / "shelog.db", capsys)
    assert code == 0
    assert _lines(text)["ts_source_not_cloud"] == ("1", "info")


def test_window_without_attempts_fails_and_database_is_untouched(report, tmp_db, tmp_path, capsys):
    db = tmp_path / "data" / "shelog.db"
    tmp_db.commit()
    before = db.read_bytes()
    code, text = _run(report, db, capsys)
    assert code == 1
    assert _lines(text)["attempts"] == ("0", "FAIL")
    assert db.read_bytes() == before


def test_missing_database_exits_2(report, tmp_path, capsys):
    code = report.main(["--db", str(tmp_path / "none.db"), "--since", "1", "--expect-devices", "1"])
    assert code == 2
    assert capsys.readouterr().out == ""


def test_help_lists_the_arguments(report, capsys):
    with pytest.raises(SystemExit) as exit_info:
        report.main(["--help"])
    assert exit_info.value.code == 0
    text = capsys.readouterr().out
    for flag in ("--db", "--since", "--expect-devices"):
        assert flag in text
