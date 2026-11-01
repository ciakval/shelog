"""Scheduler, backoff, auth state, poll_log completeness and lifecycle (FakeClock, offline)."""

from __future__ import annotations

import dataclasses
import itertools
import json
import logging
import os
import random
import re
import signal
import sqlite3
import threading

import pytest
from conftest import SENTINEL_KEY, FakeClock, FakeSend

from shelog.db import get_meta, set_meta
from shelog.lock import AlreadyRunning, acquire_instance_lock
from shelog.poller import (
    AUTH_CONFIRMATIONS,
    AUTH_FLOOR_S,
    CAP_S,
    MIN_GAP_S,
    STARTUP_FAIL_SLEEP_S,
    Poller,
    main,
    next_delay,
    run,
    where,
)
from shelog.shelly.client import RateGuard, ShellyClient
from shelog.shelly.transport import RawResponse, TransportError

FIXTURE = "synthetic_envelope_all_models.json"
HT1 = "020000000001"


def _ok(doc: dict) -> RawResponse:
    return RawResponse(200, {"content-type": "application/json"}, json.dumps(doc).encode())


@pytest.fixture
def ok_response(envelope):
    return _ok(envelope(FIXTURE))


def _build(test_settings, test_devices, clock, con, send) -> Poller:
    client = ShellyClient(
        test_settings.host,
        test_settings.auth_key,
        send=send,
        guard=RateGuard(clock, MIN_GAP_S),
        clock=clock,
    )
    return Poller(test_settings, test_devices, con, client, clock)


@pytest.fixture
def rig(test_settings, test_devices, fake_clock, tmp_db):
    send = FakeSend(fake_clock)
    return _build(test_settings, test_devices, fake_clock, tmp_db, send), send, tmp_db, fake_clock


def _stopper(stop: threading.Event, response: RawResponse):
    def go(call):
        stop.set()
        return response

    return go


def test_constants():
    assert (CAP_S, MIN_GAP_S, AUTH_FLOOR_S, AUTH_CONFIRMATIONS) == (300.0, 1.1, 300.0, 2)
    assert STARTUP_FAIL_SLEEP_S == 30.0


def test_next_delay_ok_is_jittered_around_the_interval():
    rng = random.Random(1234)
    for _ in range(200):
        assert 54.0 <= next_delay(60, 0, "ok", None, rng) <= 66.0


def test_next_delay_failures_back_off_exponentially_with_equal_jitter_and_a_cap():
    rng = random.Random(1234)
    for n in range(1, 40):
        base = min(300.0, 60.0 * 2 ** (n - 1)) if n < 20 else 300.0
        for _ in range(20):
            delay = next_delay(60, n, "network", None, rng)
            assert base / 2 <= delay <= base


def test_next_delay_rate_limited_honours_retry_after():
    rng = random.Random(1234)
    for _ in range(50):
        assert next_delay(60, 1, "rate_limited", 120.0, rng) >= 120.0
    assert next_delay(60, 1, "rate_limited", 900.0, rng) >= 900.0


def test_next_delay_auth_has_a_floor():
    rng = random.Random(1234)
    for n in range(1, 8):
        assert next_delay(60, n, "auth", None, rng) >= AUTH_FLOOR_S


def test_next_delay_never_below_the_min_gap():
    rng = random.Random(1)
    assert next_delay(0.1, 0, "ok", None, rng) >= MIN_GAP_S
    assert next_delay(0.1, 1, "network", None, rng) >= MIN_GAP_S


def test_observed_failure_delay_sequence_for_seed_1234():
    rng = random.Random(1234)
    seq = [round(next_delay(60, n, "network", None, rng), 1) for n in range(1, 7)]
    assert seq == [59.0, 86.4, 120.9, 286.6, 290.9, 237.3]


class _TimedSend:
    """FakeSend-like sender driven by call index; stops the run after a simulated window."""

    def __init__(self, clock: FakeClock, stop: threading.Event, window_s: float, special, ok):
        self.clock = clock
        self.stop = stop
        self.window_s = window_s
        self.special = special
        self.ok = ok
        self.start = clock.monotonic()
        self.calls: list[float] = []

    def __call__(self, url, *, form=None, json_body=None, headers=None, timeout):
        index = len(self.calls)
        self.calls.append(self.clock.monotonic())
        if self.clock.monotonic() - self.start >= self.window_s:
            self.stop.set()
        item = self.special.get(index, self.ok)
        if isinstance(item, Exception):
            raise item
        return item


def test_six_hour_simulation_respects_gap_logs_every_attempt_and_honours_retry_after(
    test_settings, test_devices, fake_clock, tmp_db, ok_response, caplog
):
    caplog.set_level(logging.INFO)
    stop = threading.Event()
    special = {
        5: RawResponse(503, {}, b""),
        6: RawResponse(503, {}, b""),
        7: RawResponse(502, {}, b""),
        8: TransportError("timeout"),
        9: TransportError("network"),
        20: RawResponse(429, {"retry-after": "120"}, b""),
        30: RawResponse(200, {}, b"not json at all"),
        40: RawResponse(404, {}, b""),
        50: RuntimeError("boom"),
        60: RawResponse(418, {}, b""),
        70: _ok({"isok": True, "data": {"devices_status": {}}}),
        80: RawResponse(401, {}, b""),
        100: RawResponse(200, {}, json.dumps({"isok": False, "errors": {"max_req": "x"}}).encode()),
    }
    send = _TimedSend(fake_clock, stop, 6 * 3600, special, ok_response)
    poller = _build(test_settings, test_devices, fake_clock, tmp_db, send)

    assert run(poller, stop, fake_clock, random.Random(1234)) == 0

    gaps = [b - a for a, b in itertools.pairwise(send.calls)]
    assert len(send.calls) > 100
    assert min(gaps) >= MIN_GAP_S
    assert send.calls[21] - send.calls[20] >= 120.0

    rows = tmp_db.execute("SELECT ok, error FROM poll_log ORDER BY id").fetchall()
    assert len(rows) == len(send.calls)
    assert sum(1 for _, error in rows if error == "internal") == 1
    assert all(error is not None for ok, error in rows if ok == 0)
    errors = {error for _, error in rows if error}
    assert {"http_5xx", "timeout", "network", "rate_limited", "bad_response"} <= errors
    assert {"endpoint_gone", "http_other", "no_devices", "auth", "internal"} <= errors
    assert SENTINEL_KEY not in caplog.text


def test_ok_cadence_does_not_drift_and_recovery_has_no_burst(
    test_settings, test_devices, fake_clock, tmp_db, ok_response
):
    stop = threading.Event()
    special = {i: TransportError("network") for i in range(3, 13)}
    send = _TimedSend(fake_clock, stop, 3 * 3600, special, ok_response)
    poller = _build(test_settings, test_devices, fake_clock, tmp_db, send)
    run(poller, stop, fake_clock, random.Random(7))

    calls = send.calls
    # The first send after the failure streak is at least MIN_GAP_S after the last failure.
    assert calls[13] - calls[12] >= MIN_GAP_S
    # Steady state before the streak: 2 intervals, each within the +/-10 % jitter band.
    assert 2 * 54.0 <= calls[2] - calls[0] <= 2 * 66.0
    # And after recovery: N cadence steps inside N * 60 * [0.9, 1.1], no accumulated drift.
    tail = calls[14:]
    steps = len(tail) - 1
    assert steps > 100
    assert steps * 54.0 <= tail[-1] - tail[0] <= steps * 66.0


def test_auth_needs_two_consecutive_outcomes_to_become_rejected(
    test_settings, test_devices, fake_clock, tmp_db, ok_response, caplog
):
    caplog.set_level(logging.INFO)
    stop = threading.Event()
    seen: list[str | None] = []

    def probe(response, *, then_stop=False):
        def go(call):
            seen.append(get_meta(tmp_db, "shelly_auth"))
            if then_stop:
                stop.set()
            return response

        return go

    send = FakeSend(
        fake_clock,
        RawResponse(401, {}, b""),
        probe(ok_response),  # seen[0]: state after ONE auth outcome
        probe(RawResponse(401, {}, b"")),  # seen[1]: state after the recovery
        probe(RawResponse(403, {}, b"")),  # seen[2]: state after a lone auth following ok
        probe(ok_response, then_stop=True),  # seen[3]: state after two consecutive auths
    )
    poller = _build(test_settings, test_devices, fake_clock, tmp_db, send)
    run(poller, stop, fake_clock, random.Random(3))

    assert seen[0] != "rejected"
    assert seen[1] == "ok"
    assert seen[2] != "rejected"
    assert seen[3] == "rejected"
    assert get_meta(tmp_db, "shelly_auth") == "ok"  # the final ok cleared it
    assert get_meta(tmp_db, "shelly_auth_since") is not None
    at = [c.at for c in send.calls]
    assert at[3] - at[2] >= AUTH_FLOOR_S  # between the two consecutive auth outcomes
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "rejected the auth key" in errors[0].getMessage()
    assert sum("accepted the auth key again" in r.getMessage() for r in caplog.records) == 1
    assert SENTINEL_KEY not in caplog.text


def test_rejected_state_is_persisted_while_it_lasts(
    test_settings, test_devices, fake_clock, tmp_db, ok_response
):
    stop = threading.Event()
    states = []

    def last(call):
        states.append(get_meta(tmp_db, "shelly_auth"))
        stop.set()
        return RawResponse(401, {}, b"")

    send = FakeSend(fake_clock, RawResponse(401, {}, b""), RawResponse(401, {}, b""), last)
    poller = _build(test_settings, test_devices, fake_clock, tmp_db, send)
    run(poller, stop, fake_clock, random.Random(3))
    assert states == ["rejected"]
    assert get_meta(tmp_db, "shelly_auth") == "rejected"
    assert int(get_meta(tmp_db, "shelly_auth_since")) >= int(fake_clock.time()) - 3600


def test_first_ok_writes_auth_ok_when_meta_is_absent(rig, ok_response):
    poller, send, con, _ = rig
    send.queue(ok_response)
    assert get_meta(con, "shelly_auth") is None
    poller.cycle(threading.Event())
    assert get_meta(con, "shelly_auth") == "ok"


def test_device_never_stored_triggers_one_warning_by_name(rig, envelope, caplog):
    poller, send, _, _ = rig
    doc = envelope(FIXTURE)
    doc["data"]["devices_status"][HT1]["_updated"] = "2000-01-01 00:00:00"  # implausible
    send.queue(*[_ok(doc) for _ in range(12)])
    caplog.set_level(logging.WARNING)
    for _ in range(12):
        poller.step(threading.Event())

    hits = [r for r in caplog.records if "no reading ever stored" in r.getMessage()]
    assert len(hits) == 1
    assert "T-HT1" in hits[0].getMessage()
    assert HT1 not in caplog.text


def test_bad_response_logs_only_size_and_kind_and_stores_nothing_of_it(rig, caplog):
    poller, send, con, _ = rig
    body = json.dumps({"isok": False, "data": {"devices_status": {HT1: {"mac": "AABBCCDDEEFF"}}}})
    send.queue(RawResponse(200, {}, body.encode()))
    caplog.set_level(logging.WARNING)
    poller.step(threading.Event())

    text = " ".join(r.getMessage() for r in caplog.records)
    assert f"unexpected response body: {len(body)} bytes, json dict" in text
    assert HT1 not in caplog.text
    assert "AABBCCDDEEFF" not in caplog.text
    row = con.execute("SELECT ok, error FROM poll_log").fetchall()
    assert row == [(0, "bad_response")]


def test_stop_set_inside_a_send_ends_run_with_zero_and_no_further_sends(
    test_settings, test_devices, fake_clock, tmp_db, ok_response
):
    stop = threading.Event()
    send = FakeSend(fake_clock, _stopper(stop, ok_response))
    poller = _build(test_settings, test_devices, fake_clock, tmp_db, send)
    assert run(poller, stop, fake_clock, random.Random(1)) == 0
    assert len(send.calls) == 1


def test_internal_error_row_carries_only_the_category(rig, caplog):
    poller, send, con, _ = rig
    send.queue(RuntimeError(f"secret {SENTINEL_KEY}"))
    caplog.set_level(logging.INFO)
    category, _ = poller.cycle(threading.Event())
    assert category == "internal"
    assert con.execute("SELECT ok, error FROM poll_log").fetchall() == [(0, "internal")]
    assert SENTINEL_KEY not in caplog.text
    assert "RuntimeError" in caplog.text
    # the log says WHERE it broke (file:line function) so a weeks-old bug is diagnosable
    assert re.search(r"RuntimeError at \w+\.py:\d+ \w+", caplog.text)


def test_where_names_type_and_call_sites_but_never_the_message():
    def inner():
        raise KeyError(f"020000000001 {SENTINEL_KEY}")

    try:
        inner()
    except KeyError as err:
        text = where(err)
    assert text.startswith("KeyError at test_poller.py:")
    assert "inner" in text
    assert "020000000001" not in text
    assert SENTINEL_KEY not in text


def test_second_lock_on_the_same_path_raises_already_running(tmp_path):
    first = acquire_instance_lock(tmp_path / "d" / "poller.lock")
    try:
        with pytest.raises(AlreadyRunning):
            acquire_instance_lock(tmp_path / "d" / "poller.lock")
    finally:
        first.close()
    again = acquire_instance_lock(tmp_path / "d" / "poller.lock")
    again.close()


def test_main_returns_3_when_another_poller_holds_the_lock(
    test_settings, test_devices, fake_clock, capsys
):
    handle = acquire_instance_lock(test_settings.db_path.parent / "poller.lock")
    send = FakeSend(fake_clock)
    try:
        code = main(test_settings, test_devices, once=True, clock=fake_clock, send=send)
    finally:
        handle.close()
    assert code == 3
    assert send.calls == []
    assert "another shelog poll/probe is running" in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_main_once_with_an_unwritable_data_dir_returns_1_without_sending(
    test_settings, test_devices, fake_clock, tmp_path
):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        settings = dataclasses.replace(test_settings, db_path=ro / "data" / "shelog.db")
        send = FakeSend(fake_clock)
        assert main(settings, test_devices, once=True, clock=fake_clock, send=send) == 1
        assert send.calls == []
    finally:
        ro.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_main_loop_mode_sleeps_before_exiting_on_startup_failure(
    test_settings, test_devices, fake_clock, tmp_path
):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        settings = dataclasses.replace(test_settings, db_path=ro / "data" / "shelog.db")
        before = fake_clock.monotonic()
        send = FakeSend(fake_clock)
        assert main(settings, test_devices, once=False, clock=fake_clock, send=send) == 1
        assert fake_clock.monotonic() - before >= STARTUP_FAIL_SLEEP_S
        assert send.calls == []
    finally:
        ro.chmod(0o700)


def test_main_loop_mode_survives_a_db_error_while_constructing_the_poller(
    test_settings, test_devices, fake_clock, monkeypatch, caplog
):
    def boom(con):
        raise sqlite3.DatabaseError(f"database disk image is malformed {SENTINEL_KEY}")

    monkeypatch.setattr("shelog.poller.store.load_last_states", boom)
    caplog.set_level(logging.ERROR)
    before = fake_clock.monotonic()
    send = FakeSend(fake_clock)
    assert main(test_settings, test_devices, once=False, clock=fake_clock, send=send) == 1
    assert fake_clock.monotonic() - before >= STARTUP_FAIL_SLEEP_S  # no hot restart loop
    assert send.calls == []
    assert "startup failed: DatabaseError" in caplog.text
    assert SENTINEL_KEY not in caplog.text


def test_main_once_runs_one_cycle_and_reports_exit_code(
    test_settings, test_devices, fake_clock, ok_response
):
    send = FakeSend(fake_clock, ok_response)
    assert main(test_settings, test_devices, once=True, clock=fake_clock, send=send) == 0
    send = FakeSend(fake_clock, RawResponse(503, {}, b""))
    assert main(test_settings, test_devices, once=True, clock=fake_clock, send=send) == 1


def test_main_loop_stops_cleanly_on_sigterm(test_settings, test_devices, fake_clock, ok_response):
    def terminate(call):
        os.kill(os.getpid(), signal.SIGTERM)
        return ok_response

    previous = signal.getsignal(signal.SIGTERM)
    send = FakeSend(fake_clock, terminate)
    code = main(
        test_settings,
        test_devices,
        once=False,
        clock=fake_clock,
        send=send,
        rng=random.Random(1),
    )
    assert code == 0
    assert len(send.calls) == 1
    assert signal.getsignal(signal.SIGTERM) == previous  # handlers restored


def test_set_meta_upserts(tmp_db):
    set_meta(tmp_db, "k", "a")
    set_meta(tmp_db, "k", "b")
    assert get_meta(tmp_db, "k") == "b"
    assert get_meta(tmp_db, "missing") is None


def test_housekeeping_is_rate_limited_and_never_raises(rig, caplog):
    poller, _send, _con, clock = rig
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("secret-ish text")

    poller.hooks = [boom]
    poller.housekeeping()
    poller.housekeeping()  # within the window: skipped
    clock.advance(301)
    poller.housekeeping()
    assert len(calls) == 2
    assert "secret-ish text" not in caplog.text


def test_daily_backup_hook_writes_a_snapshot(rig, test_settings):
    poller, *_ = rig
    poller.housekeeping()
    snaps = list((test_settings.db_path.parent / "backup").glob("shelog-*.db"))
    assert len(snaps) == 1
