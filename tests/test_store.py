"""decide() boundaries, the poll-source rule, write_cycle and the envelope edge cases."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeSend

from shelog import store
from shelog.poller import Poller
from shelog.shelly.client import RateGuard, ShellyClient
from shelog.shelly.parse import Report
from shelog.shelly.transport import RawResponse
from shelog.store import (
    IMPLAUSIBLE,
    INSERT,
    REGRESSED,
    UNCHANGED,
    LastState,
    PollRecord,
    ReadingRow,
    decide,
)

NOW = 1_793_520_000
DEVICE = "020000000001"


def report(
    ts: int | None,
    *,
    source: str = "cloud",
    t_c: float | None = 21.0,
    rh: float | None = 50.0,
    battery: float | None = 90.0,
    flood: int | None = None,
) -> Report:
    return Report(DEVICE, ts, source, t_c, rh, battery, flood, raw="{}")


def last(ts: int, **overrides: Any) -> LastState:
    values = {"t_c": 21.0, "rh": 50.0, "battery": 90.0, "flood": None, **overrides}
    return LastState(ts, **values)


# ---------------------------------------------------------------------------
# decide(): cloud-source timestamps
# ---------------------------------------------------------------------------


def test_constants_are_the_documented_ones() -> None:
    assert (store.MIN_PLAUSIBLE_TS, store.MAX_FUTURE_SKEW_S) == (1_577_836_800, 120)


@pytest.mark.parametrize(
    ("ts", "action"),
    [
        (NOW + 120, INSERT),
        (NOW + 121, IMPLAUSIBLE),
        (1_577_836_799, IMPLAUSIBLE),
        (1_577_836_800, INSERT),
        (0, IMPLAUSIBLE),
        (-5, IMPLAUSIBLE),
    ],
)
def test_plausibility_boundaries(ts: int, action: str) -> None:
    assert decide(None, report(ts), NOW).action == action


def test_equal_is_unchanged_and_lower_is_regressed_and_higher_inserts() -> None:
    base = NOW - 600
    assert decide(last(base), report(base), NOW) == store.Decision(UNCHANGED, base)
    assert decide(last(base), report(base - 1), NOW).action == REGRESSED
    assert decide(last(base), report(base + 1), NOW) == store.Decision(INSERT, base + 1)


def test_three_week_old_report_is_not_rejected_for_age() -> None:
    old = NOW - 21 * 86400
    assert decide(None, report(old), NOW) == store.Decision(INSERT, old)
    assert decide(last(old - 10), report(old), NOW).action == INSERT
    assert decide(last(old), report(old), NOW).action == UNCHANGED


def test_cloud_report_without_a_timestamp_is_implausible() -> None:
    assert decide(None, report(None), NOW).action == IMPLAUSIBLE


def test_values_are_irrelevant_for_cloud_source() -> None:
    assert decide(last(NOW - 5), report(NOW - 5, t_c=30.0), NOW).action == UNCHANGED


# ---------------------------------------------------------------------------
# decide(): poll-source fallback
# ---------------------------------------------------------------------------


def test_poll_source_first_report_inserts_with_now() -> None:
    assert decide(None, report(None, source="poll"), NOW) == store.Decision(INSERT, NOW)


def test_poll_source_identical_values_are_unchanged() -> None:
    assert decide(last(NOW - 60), report(None, source="poll"), NOW).action == UNCHANGED


@pytest.mark.parametrize(
    "change",
    [{"t_c": 22.0}, {"rh": 51.0}, {"battery": 89.0}, {"flood": 1}, {"t_c": None}],
)
def test_poll_source_changed_value_inserts_with_now(change: dict[str, Any]) -> None:
    decision = decide(last(NOW - 60), report(None, source="poll", **change), NOW)
    assert decision == store.Decision(INSERT, NOW)


def test_poll_source_ignores_the_age_of_the_last_row() -> None:
    assert decide(last(NOW - 90 * 86400), report(None, source="poll"), NOW).action == UNCHANGED


# ---------------------------------------------------------------------------
# write_cycle on a real tmp database
# ---------------------------------------------------------------------------


def ok_poll(ts: int = NOW, n_unchanged: int = 0) -> PollRecord:
    return PollRecord(ts, 1, 200, None, 2, 0, n_unchanged, 0, None, 80)


def row(device: str, ts: int, **overrides: Any) -> ReadingRow:
    values = {
        "ts_source": "cloud",
        "t_c": 21.0,
        "rh": 50.0,
        "battery": 90.0,
        "flood": None,
        "raw": json.dumps({"d": device}),
        **overrides,
    }
    return ReadingRow(device, ts, **values)


def test_replaying_rows_inserts_nothing_and_counts_them_unchanged(
    tmp_db: sqlite3.Connection,
) -> None:
    rows = [row("a", NOW - 100), row("b", NOW - 90)]
    assert store.write_cycle(tmp_db, ok_poll(), rows) == 2
    replay = ok_poll(NOW + 60)
    assert store.write_cycle(tmp_db, replay, rows) == 0
    assert (replay.n_new, replay.n_unchanged) == (0, 2)
    logged = tmp_db.execute("SELECT n_new, n_unchanged FROM poll_log ORDER BY id").fetchall()
    assert logged == [(2, 0), (0, 2)]


def test_raw_status_mirrors_reading_rows(tmp_db: sqlite3.Connection) -> None:
    rows = [row("a", NOW - 100), row("b", NOW - 90), row("a", NOW - 40)]
    store.write_cycle(tmp_db, ok_poll(), rows)
    store.write_cycle(tmp_db, ok_poll(NOW + 60), rows)
    readings = tmp_db.execute("SELECT device_id, ts FROM reading ORDER BY 1, 2").fetchall()
    raws = tmp_db.execute("SELECT device_id, ts FROM raw_status ORDER BY 1, 2").fetchall()
    assert readings == raws
    assert len(readings) == 3
    body = tmp_db.execute("SELECT body FROM raw_status WHERE device_id = 'b'").fetchone()[0]
    assert json.loads(body) == {"d": "b"}


def test_null_rh_reads_back_as_null_not_zero(tmp_db: sqlite3.Connection) -> None:
    store.write_cycle(tmp_db, ok_poll(), [row("a", NOW - 5, rh=None, t_c=None, battery=None)])
    stored = tmp_db.execute("SELECT t_c, rh, battery FROM reading").fetchone()
    assert stored == (None, None, None)


def test_text_in_a_real_column_is_rejected_by_strict(tmp_db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.write_cycle(tmp_db, ok_poll(), [row("a", NOW - 5, t_c="warm")])  # type: ignore[arg-type]


def test_ts_zero_is_rejected_by_check(tmp_db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.write_cycle(tmp_db, ok_poll(), [row("a", 0)])


def test_exception_mid_cycle_leaves_no_poll_log_row(tmp_db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.write_cycle(tmp_db, ok_poll(), [row("a", NOW - 5), row("b", 0)])
    assert tmp_db.execute("SELECT count(*) FROM poll_log").fetchone()[0] == 0
    assert tmp_db.execute("SELECT count(*) FROM reading").fetchone()[0] == 0
    assert tmp_db.execute("SELECT count(*) FROM raw_status").fetchone()[0] == 0
    assert not tmp_db.in_transaction


def test_failure_record_stores_ok_zero_with_only_a_category(tmp_db: sqlite3.Connection) -> None:
    failed = PollRecord(NOW, 0, None, "timeout", None, None, None, None, None, 15000)
    assert store.write_cycle(tmp_db, failed, []) == 0
    stored = tmp_db.execute(
        "SELECT ok, error, http_status, n_new, n_unchanged, n_rejected FROM poll_log"
    ).fetchall()
    assert stored == [(0, "timeout", None, None, None, None)]


# ---------------------------------------------------------------------------
# Whole-envelope edge cases through the poller
# ---------------------------------------------------------------------------

ENVELOPES = json.loads(
    (Path(__file__).parent / "fixtures" / "shelly" / "synthetic_cases.json").read_text("utf-8")
)["envelopes"]


@pytest.fixture
def rig(
    test_settings: Any, test_devices: Any, fake_clock: Any, tmp_db: sqlite3.Connection
) -> Callable[[dict[str, Any] | list[Any]], tuple[Any, sqlite3.Connection]]:
    def build(envelope: Any) -> tuple[Any, sqlite3.Connection]:
        send = FakeSend(fake_clock)
        body = json.dumps(copy.deepcopy(envelope)).encode()
        send.queue(RawResponse(200, {"content-type": "application/json"}, body))
        client = ShellyClient(
            test_settings.host,
            test_settings.auth_key,
            send=send,
            guard=RateGuard(fake_clock),
            clock=fake_clock,
        )
        poller = Poller(test_settings, test_devices, tmp_db, client, fake_clock)
        return poller, tmp_db

    return build


def _poll_row(con: sqlite3.Connection) -> tuple[Any, ...]:
    return con.execute(
        "SELECT ok, error, n_devices, n_new, n_unchanged, n_rejected FROM poll_log"
    ).fetchone()


def test_a_sleeping_device_missing_from_the_response_is_skipped_quietly(rig: Any) -> None:
    poller, con = rig(ENVELOPES["absent_device"])
    assert poller.step(threading.Event()).outcome.ok
    assert _poll_row(con) == (1, None, 2, 2, 0, 0)
    assert con.execute("SELECT count(*) FROM reading").fetchone()[0] == 2


def test_an_empty_device_block_is_rejected_and_counted(rig: Any) -> None:
    poller, con = rig(ENVELOPES["empty_block"])
    assert poller.step(threading.Event()).outcome.ok
    assert _poll_row(con) == (1, None, 3, 2, 0, 1)


def test_an_empty_device_list_is_a_failed_poll_with_no_readings(rig: Any) -> None:
    poller, con = rig(ENVELOPES["empty_list"])
    assert not poller.step(threading.Event()).outcome.ok
    assert _poll_row(con) == (0, "no_devices", None, None, None, None)
    assert con.execute("SELECT count(*) FROM reading").fetchone()[0] == 0


def test_out_of_range_values_are_counted_in_n_rejected(rig: Any) -> None:
    envelope = copy.deepcopy(ENVELOPES["absent_device"])
    envelope["data"]["devices_status"]["020000000001"]["tmp"]["tC"] = 150
    poller, con = rig(envelope)
    poller.step(threading.Event())
    assert _poll_row(con) == (1, None, 2, 2, 0, 1)
    assert con.execute("SELECT t_c FROM reading WHERE device_id = '020000000001'").fetchone() == (
        None,
    )
