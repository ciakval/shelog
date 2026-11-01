"""Whole-system proof that the auth key leaves no trace (COLL-07, ROADMAP Phase 1 SC4).

The poll loop is driven through every outcome category (including an exception that embeds
the full request form, i.e. a leaky library) and the probe through every step group, all
with a sentinel key. Then stdout/stderr, the SQLite files and the probe capture directory
are searched for the sentinel in raw, quote and quote_plus form. Nothing touches the network.
"""

from __future__ import annotations

import io
import json
import logging
import random
import threading
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from conftest import SENTINEL_KEY, TEST_HOST, FakeClock, FakeSend
from test_probe import FULL_STEPS, matrix_responses

from shelog import check
from shelog.config import Device, Settings
from shelog.logging_setup import setup_logging
from shelog.poller import MIN_GAP_S, Poller, run
from shelog.probe import run_probe
from shelog.shelly.client import ERROR_CATEGORIES, RateGuard, ShellyClient
from shelog.shelly.transport import RawResponse, TransportError

ENVELOPE = "synthetic_envelope_all_models.json"
RETRY_AFTER_S = 120.0
FORMS = (
    SENTINEL_KEY,
    urllib.parse.quote(SENTINEL_KEY, safe=""),
    urllib.parse.quote_plus(SENTINEL_KEY),
)


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _json(status: int, doc: object, **headers: str) -> RawResponse:
    return RawResponse(
        status, {"content-type": "application/json", **headers}, json.dumps(doc).encode()
    )


def _leaks(data: bytes) -> list[str]:
    return [form for form in FORMS if form.encode() in data]


def _client(settings: Settings, send: FakeSend, clock: FakeClock) -> ShellyClient:
    return ShellyClient(
        settings.host,
        settings.auth_key,
        send=send,
        guard=RateGuard(clock, MIN_GAP_S),
        clock=clock,
    )


def test_every_poll_outcome_and_the_probe_leave_no_trace_of_the_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    test_settings: Settings,
    test_devices: tuple[Device, ...],
    tmp_db,
    envelope,
) -> None:
    monkeypatch.chdir(tmp_path)
    setup_logging("DEBUG", [SENTINEL_KEY])
    clock = FakeClock()
    stop = threading.Event()
    ok = _json(200, envelope(ENVELOPE))

    def leaky_library(call) -> RawResponse:
        stop.set()  # last scripted request: the loop ends after recording it
        raise RuntimeError(f"upstream library failed for {call.url} with form={call.form}")

    send = FakeSend(
        clock,
        ok,  # 0: new readings
        ok,  # 1: same report again -> unchanged
        RawResponse(401, {}, b'{"isok": false}'),  # 2: auth
        RawResponse(403, {}, b""),  # 3: auth -> rejected
        RawResponse(429, {"retry-after": str(int(RETRY_AFTER_S))}, b""),  # 4: rate_limited
        _json(200, {"isok": False, "errors": {"max_req": "Request limit reached!"}}),  # 5
        RawResponse(503, {}, b""),  # 6: http_5xx
        RawResponse(418, {}, b""),  # 7: http_other
        TransportError("timeout"),  # 8
        TransportError("network"),  # 9
        RawResponse(200, {}, b"<html><body>moved</body></html>"),  # 10: endpoint_gone
        RawResponse(200, {}, b'{"isok": true, "data": {"devices_status": NaN}}'),  # 11
        _json(200, {"isok": True, "data": {"devices_status": []}}),  # 12: no_devices
        leaky_library,  # 13: internal
    )
    poller = Poller(test_settings, test_devices, tmp_db, _client(test_settings, send, clock), clock)

    assert run(poller, stop, clock, random.Random(2024)) == 0
    assert len(send.calls) == 14

    # --- poll_log: closed categories, no free text, auth state, Retry-After honoured ------
    rows = tmp_db.execute("SELECT ok, error FROM poll_log ORDER BY id").fetchall()
    assert len(rows) == len(send.calls)
    assert all(error is None or error in ERROR_CATEGORIES for _, error in rows)
    assert all(error is not None for ok_flag, error in rows if ok_flag == 0)
    assert {error for _, error in rows if error} == ERROR_CATEGORIES
    auth_state = tmp_db.execute("SELECT value FROM meta WHERE key = 'shelly_auth'").fetchone()
    assert auth_state == ("rejected",)
    assert send.calls[5].at - send.calls[4].at >= RETRY_AFTER_S
    gaps = [b.at - a.at for a, b in zip(send.calls, send.calls[1:], strict=False)]
    assert min(gaps) >= MIN_GAP_S

    # --- probe through every step group, same sentinel -------------------------------------
    probe_root = tmp_path / "data" / "probe"
    probe_send = FakeSend(clock, *matrix_responses(clock, test_devices))
    probe_code = run_probe(
        test_settings,
        test_devices,
        _client(test_settings, probe_send, clock),
        clock,
        out_root=probe_root,
        stdout=io.StringIO(),
        errors=True,
        wrong_host=True,
    )
    assert probe_code in (0, 1)
    assert len(probe_send.calls) == len(FULL_STEPS)
    for call in probe_send.calls:
        host = urlsplit(call.url).hostname
        if host != TEST_HOST:
            assert _leaks(json.dumps([call.url, call.form, call.json_body]).encode()) == []

    # --- the sentinel is nowhere: console, SQLite files, probe captures --------------------
    captured = capfd.readouterr()
    assert _leaks((captured.out + captured.err).encode()) == []
    assert "poll ok" in captured.err  # the loop really logged

    db = test_settings.db_path
    for path in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        if path.exists():
            assert _leaks(path.read_bytes()) == [], path.name
    probe_files = [p for p in probe_root.rglob("*") if p.is_file()]
    assert probe_files
    for path in probe_files:
        assert _leaks(path.read_bytes()) == [], path.name

    # --- the shipped scanner agrees ---------------------------------------------------------
    out = io.StringIO()
    code = check.run_check(test_settings, test_devices, data_dir=tmp_path / "data", stdout=out)
    assert code == 0
    assert "0 hits" in out.getvalue()
    assert _leaks(out.getvalue().encode()) == []
