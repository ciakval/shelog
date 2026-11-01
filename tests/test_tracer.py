"""Offline end-to-end poll cycle: fetch, parse, dedup and the single-transaction write."""

from __future__ import annotations

import copy
import json
import threading

import pytest
from conftest import SENTINEL_KEY, TEST_HOST, FakeSend

from shelog.db import SchemaTooNew, connect_rw, migrate
from shelog.poller import Poller
from shelog.shelly.client import RateGuard, ShellyClient
from shelog.shelly.parse import parse_device
from shelog.shelly.transport import RawResponse

HT1, HT3, FLOOD = "020000000001", "020000000002", "020000000003"
FIXTURE = "synthetic_envelope_all_models.json"


def _ok(doc: dict) -> RawResponse:
    return RawResponse(200, {"content-type": "application/json"}, json.dumps(doc).encode())


@pytest.fixture
def rig(test_settings, test_devices, fake_clock, tmp_db):
    send = FakeSend(fake_clock)
    client = ShellyClient(
        test_settings.host,
        test_settings.auth_key,
        send=send,
        guard=RateGuard(fake_clock),
        clock=fake_clock,
    )
    poller = Poller(test_settings, test_devices, tmp_db, client, fake_clock)
    return poller, send, tmp_db, fake_clock


def _poll_rows(con):
    return con.execute(
        "SELECT ok, error, http_status, n_devices, n_new, n_unchanged, n_rejected"
        " FROM poll_log ORDER BY id"
    ).fetchall()


def test_first_cycle_stores_exact_values_raw_json_and_poll_log(rig, envelope):
    poller, send, con, _ = rig
    send.queue(_ok(envelope(FIXTURE)))

    result = poller.step(threading.Event())

    assert result.outcome.ok
    readings = {
        r[0]: r[1:]
        for r in con.execute(
            "SELECT device_id, ts_source, t_c, rh, battery, flood FROM reading ORDER BY device_id"
        )
    }
    assert readings == {
        HT1: ("cloud", 21.5, 48.5, 97.0, None),
        HT3: ("cloud", 20.3, 55.2, 88.0, None),
        FLOOD: ("cloud", 19.0, None, 100.0, 0),
    }
    assert con.execute("SELECT count(*) FROM raw_status").fetchone()[0] == 3
    body = con.execute("SELECT body FROM raw_status WHERE device_id = ?", (HT1,)).fetchone()[0]
    assert json.loads(body)["tmp"]["tC"] == 21.5
    assert _poll_rows(con) == [(1, None, 200, 3, 3, 0, 0)]


def test_reading_ts_is_the_cloud_updated_time_in_utc(rig, envelope):
    poller, send, con, _ = rig
    send.queue(_ok(envelope(FIXTURE)))
    poller.step(threading.Event())
    ts = con.execute("SELECT ts FROM reading WHERE device_id = ?", (HT1,)).fetchone()[0]
    assert ts == 1793515800  # 2026-11-01 06:50:00 UTC


def test_replaying_an_unchanged_response_inserts_nothing(rig, envelope):
    poller, send, con, _ = rig
    send.queue(_ok(envelope(FIXTURE)), _ok(envelope(FIXTURE)))
    poller.step(threading.Event())
    poller.step(threading.Event())

    assert con.execute("SELECT count(*) FROM reading").fetchone()[0] == 3
    assert con.execute("SELECT count(*) FROM raw_status").fetchone()[0] == 3
    assert _poll_rows(con)[1] == (1, None, 200, 3, 0, 3, 0)


def test_one_advanced_report_inserts_exactly_one_row(rig, envelope):
    poller, send, con, _ = rig
    advanced = copy.deepcopy(envelope(FIXTURE))
    advanced["data"]["devices_status"][HT3]["_updated"] = "2026-11-01 06:55:00"
    advanced["data"]["devices_status"][HT3]["temperature:0"]["tC"] = 20.8
    send.queue(_ok(envelope(FIXTURE)), _ok(advanced))
    poller.step(threading.Event())
    poller.step(threading.Event())

    assert _poll_rows(con)[1] == (1, None, 200, 3, 1, 2, 0)
    assert (
        con.execute("SELECT count(*) FROM reading WHERE device_id = ?", (HT3,)).fetchone()[0] == 2
    )


def test_one_request_per_cycle_with_the_key_only_in_the_form_body(rig, envelope):
    poller, send, _, _ = rig
    send.queue(_ok(envelope(FIXTURE)))
    poller.step(threading.Event())

    assert len(send.calls) == 1
    call = send.calls[0]
    assert call.url == f"https://{TEST_HOST}/device/all_status"
    assert call.form == {"auth_key": SENTINEL_KEY}
    assert SENTINEL_KEY not in call.url
    assert SENTINEL_KEY not in json.dumps(call.headers or {})


def test_http_503_writes_a_failure_row_and_no_readings(rig):
    poller, send, con, _ = rig
    send.queue(RawResponse(503, {}, b""))

    result = poller.step(threading.Event())

    assert not result.outcome.ok
    assert con.execute("SELECT count(*) FROM reading").fetchone()[0] == 0
    assert _poll_rows(con) == [(0, "http_5xx", 503, None, None, None, None)]


def test_response_with_no_configured_device_is_a_no_devices_failure(rig, envelope):
    poller, send, con, _ = rig
    only_unknown = {
        "isok": True,
        "data": {
            "devices_status": {
                "0200000000ff": envelope(FIXTURE)["data"]["devices_status"]["0200000000ff"]
            }
        },
    }
    send.queue(_ok(only_unknown))
    poller.step(threading.Event())

    assert _poll_rows(con) == [(0, "no_devices", 200, None, None, None, None)]


def test_migrate_is_idempotent_and_sets_user_version_1(tmp_db):
    assert tmp_db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert migrate(tmp_db) == 1
    tables = {
        r[0]
        for r in tmp_db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {"reading", "raw_status", "outdoor", "poll_log", "alert_state", "meta"} <= tables


def test_migrate_refuses_a_database_from_the_future(tmp_path):
    con = connect_rw(tmp_path / "future.db")
    con.execute("PRAGMA user_version = 99")
    with pytest.raises(SchemaTooNew):
        migrate(con)
    con.close()


def test_one_raising_parser_does_not_abort_the_cycle(rig, envelope, monkeypatch):
    poller, send, con, _ = rig

    def flaky(model, device_id, block):
        if device_id == HT3:
            raise RuntimeError("boom")
        return parse_device(model, device_id, block)

    monkeypatch.setattr("shelog.shelly.parse.parse_device", flaky)
    send.queue(_ok(envelope(FIXTURE)))
    poller.step(threading.Event())

    stored = {r[0] for r in con.execute("SELECT device_id FROM reading")}
    assert stored == {HT1, FLOOD}
    assert _poll_rows(con) == [(1, None, 200, 3, 2, 0, 1)]


def test_a_failed_poll_log_row_never_carries_exception_text(rig):
    from shelog.shelly.transport import TransportError

    poller, send, con, _ = rig
    send.queue(TransportError("timeout"))
    poller.step(threading.Event())
    assert _poll_rows(con) == [(0, "timeout", None, None, None, None, None)]
