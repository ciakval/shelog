"""Offline contract for the probe baseline step (PROBE-01, D-05, D-09)."""

from __future__ import annotations

import itertools
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import SENTINEL_KEY, TEST_HOST, FakeClock, FakeSend

from shelog.config import Device, Secret, Settings
from shelog.probe import run_probe
from shelog.shelly.client import Interrupted, RateGuard, ShellyClient
from shelog.shelly.transport import RawResponse, TransportError

INDEX_KEYS = {"step", "http_status", "category", "retry_after_s", "latency_ms", "bytes"}
UNCONFIGURED_ID = "0200000000ff"


def _stamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d %H:%M:%S")


def _envelope(clock: FakeClock, *, drop: str | None = None) -> bytes:
    updated = _stamp(clock.time() - 600)
    devices = {
        "020000000001": {"_updated": updated, "tmp": {"tC": 21.5}, "hum": {"value": 50}},
        "020000000002": {"_updated": updated, "temperature:0": {"tC": 20.0}},
        "020000000003": {"_updated": updated, "tmp": {"tC": 19.0}, "flood": False},
        UNCONFIGURED_ID: {"_updated": updated, "tmp": {"tC": 18.0}},
    }
    if drop is not None:
        del devices[drop]
    return json.dumps({"isok": True, "data": {"devices_status": devices}}).encode()


def _client(settings: Settings, send: FakeSend, clock: FakeClock) -> ShellyClient:
    return ShellyClient(
        settings.host, settings.auth_key, send=send, guard=RateGuard(clock), clock=clock
    )


def _ok(body: bytes) -> RawResponse:
    return RawResponse(200, {"content-type": "application/json"}, body)


def _only_run_dir(out_root: Path) -> Path:
    (run_dir,) = list(out_root.iterdir())
    return run_dir


def test_raw_body_is_saved_verbatim_with_an_index(
    test_settings, test_devices, fake_clock, tmp_path
):
    body = _envelope(fake_clock)
    send = FakeSend(fake_clock, _ok(body))
    out_root = tmp_path / "probe"

    code = run_probe(
        test_settings,
        test_devices,
        _client(test_settings, send, fake_clock),
        fake_clock,
        out_root=out_root,
    )

    run_dir = _only_run_dir(out_root)
    assert code == 0
    assert run_dir.name == "20261101T070000Z"
    assert (run_dir / "v1_all_status.json").read_bytes() == body
    assert (run_dir / "index.json").is_file()


def test_index_has_exactly_the_agreed_keys_and_no_identifiers(
    test_settings, test_devices, fake_clock, tmp_path
):
    send = FakeSend(fake_clock, _ok(_envelope(fake_clock)))
    out_root = tmp_path / "probe"
    run_probe(
        test_settings,
        test_devices,
        _client(test_settings, send, fake_clock),
        fake_clock,
        out_root=out_root,
    )

    text = (_only_run_dir(out_root) / "index.json").read_text()
    index = json.loads(text)
    assert [set(entry) for entry in index] == [INDEX_KEYS]
    assert index[0]["step"] == "v1_all_status"
    assert index[0]["category"] == "ok"
    assert index[0]["http_status"] == 200
    for forbidden in (SENTINEL_KEY, TEST_HOST, "https://", "0200000000"):
        assert forbidden not in text


def test_stdout_names_devices_only(test_settings, test_devices, fake_clock, tmp_path, capsys):
    send = FakeSend(fake_clock, _ok(_envelope(fake_clock)))
    run_probe(
        test_settings,
        test_devices,
        _client(test_settings, send, fake_clock),
        fake_clock,
        out_root=tmp_path / "probe",
    )

    out = capsys.readouterr().out
    for name, model in (("T-HT1", "ht_gen1"), ("T-HT3", "ht_gen3"), ("T-FLOOD", "flood_gen1")):
        line = next(ln for ln in out.splitlines() if ln.startswith(f"{name}:"))
        assert model in line
        assert "present: yes" in line
        assert "2026-11-01 06:50:00 UTC" in line
        assert "age: 10 min" in line
    assert "unconfigured devices in response: 1" in out
    for forbidden in ("0200000000", TEST_HOST, SENTINEL_KEY, "https://"):
        assert forbidden not in out


def test_missing_configured_device_fails_the_probe(
    test_settings, test_devices, fake_clock, tmp_path, capsys
):
    send = FakeSend(fake_clock, _ok(_envelope(fake_clock, drop="020000000002")))
    code = run_probe(
        test_settings,
        test_devices,
        _client(test_settings, send, fake_clock),
        fake_clock,
        out_root=tmp_path / "probe",
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "T-HT3: model ht_gen3, present: no" in out
    assert "T-HT1: model ht_gen1, present: yes" in out


def test_transport_error_is_recorded_as_a_category(
    test_settings, test_devices, fake_clock, tmp_path
):
    send = FakeSend(fake_clock, TransportError("network"))
    out_root = tmp_path / "probe"
    code = run_probe(
        test_settings,
        test_devices,
        _client(test_settings, send, fake_clock),
        fake_clock,
        out_root=out_root,
    )

    run_dir = _only_run_dir(out_root)
    (entry,) = json.loads((run_dir / "index.json").read_text())
    assert code == 1
    assert set(entry) == INDEX_KEYS
    assert entry["category"] == "network"
    assert entry["http_status"] is None
    assert entry["bytes"] == 0
    assert not (run_dir / "v1_all_status.json").exists()


def test_key_travels_only_in_the_form_body(test_settings, test_devices, fake_clock, tmp_path):
    send = FakeSend(fake_clock, _ok(_envelope(fake_clock)))
    run_probe(
        test_settings,
        test_devices,
        _client(test_settings, send, fake_clock),
        fake_clock,
        out_root=tmp_path / "probe",
    )

    (call,) = send.calls
    assert call.url == f"https://{TEST_HOST}/device/all_status"
    assert call.form == {"auth_key": SENTINEL_KEY}
    assert SENTINEL_KEY not in call.url
    assert SENTINEL_KEY not in json.dumps(dict(call.headers or {}))


def test_consecutive_requests_start_at_least_1_1_s_apart(test_settings, fake_clock):
    send = FakeSend(fake_clock, _ok(b"{}"), _ok(b"{}"), _ok(b"{}"))
    client = _client(test_settings, send, fake_clock)

    for _ in range(3):
        client.request("/device/all_status", form={"auth_key": SENTINEL_KEY})

    starts = [call.at for call in send.calls]
    assert all(later - earlier >= 1.1 for earlier, later in itertools.pairwise(starts))


def test_request_raises_interrupted_when_stopped_while_waiting(test_settings, fake_clock):
    send = FakeSend(fake_clock, _ok(b"{}"))
    client = _client(test_settings, send, fake_clock)
    stop = threading.Event()
    client.request("/device/all_status", form={"auth_key": SENTINEL_KEY}, stop=stop)
    stop.set()

    with pytest.raises(Interrupted):
        client.request("/device/all_status", form={"auth_key": SENTINEL_KEY}, stop=stop)
    assert len(send.calls) == 1


def test_secret_and_settings_never_render_the_key(test_settings: Settings):
    secret = Secret(SENTINEL_KEY)
    rendered = [
        str(secret),
        repr(secret),
        f"{secret}",
        f"{secret!r}",
        "%s" % secret,  # noqa: UP031
        str(test_settings),
        repr(test_settings),
        f"{test_settings}",
    ]
    for text in rendered:
        assert SENTINEL_KEY not in text
    assert secret.reveal() == SENTINEL_KEY


def test_fixture_devices_use_pseudo_ids(test_devices: tuple[Device, ...]):
    assert [d.name for d in test_devices] == ["T-HT1", "T-HT3", "T-FLOOD"]
    assert all(d.id.startswith("0200000000") for d in test_devices)
