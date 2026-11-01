"""Offline contract for the probe step matrix and fixture export (PROBE-01/02/04/05, D-05).

Nothing here touches the network: a scripted FakeSend answers each request and records it.
"""

from __future__ import annotations

import itertools
import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from conftest import SENTINEL_KEY, TEST_HOST, FakeClock, FakeSend, SendCall

from shelog import probe
from shelog.config import Device, Secret, Settings
from shelog.lock import acquire_instance_lock
from shelog.probe import DUMMY_KEY, UNKNOWN_ID, run_probe, sibling_host
from shelog.shelly.client import RateGuard, ShellyClient
from shelog.shelly.transport import RawResponse, TransportError

INDEX_KEYS = {
    "step",
    "http_status",
    "category",
    "retry_after_s",
    "latency_ms",
    "bytes",
    "key_placement",
}
PLACEMENTS = {"form", "json", "query", "header", "none", "dummy"}
DEFAULT_STEPS = ["v1_all_status", "v1_all_status_info", "v2_get_status", "v2_get_settings"]
ERROR_STEPS = [
    "v2_key_in_body",
    "v2_key_in_form",
    "v2_key_in_header",
    "v1_device_settings",
    "wrong_key_v1",
    "wrong_key_v2",
    "unknown_id_v2",
]
FULL_STEPS = [*DEFAULT_STEPS, *ERROR_STEPS, "wrong_host"]
SIBLING = "shelly-01-eu.shelly.cloud"

# Fabricated identifiers that only ever appear in raw (never exported) bodies.
FAB_MAC = "3c6105aabbcc"
FAB_IP = "10.20.30.40"
FAB_SSID = "OurHomeWifi"


def _stamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d %H:%M:%S")


def _json_response(status: int, doc: object, **headers: str) -> RawResponse:
    h = {"content-type": "application/json", **headers}
    return RawResponse(status, h, json.dumps(doc).encode())


def _v1_doc(clock: FakeClock, devices: tuple[Device, ...], *, info: bool) -> dict:
    updated = _stamp(clock.time() - 600)
    status: dict[str, dict] = {}
    for dev in devices:
        entry: dict = {
            "_updated": updated,
            "mac": FAB_MAC,
            "wifi_sta": {"ssid": FAB_SSID, "ip": FAB_IP},
        }
        if dev.model == "ht_gen3":
            entry["temperature:0"] = {"tC": 20.0}
            entry["sys"] = {"wakeup_period": 7200}
        elif dev.model == "flood_gen1":
            entry["tmp"] = {"tC": 19.0}
            entry["flood"] = False
        else:
            entry["tmp"] = {"tC": 21.5}
            entry["hum"] = {"value": 50}
        if info:
            entry["_dev_info"] = {"id": dev.id, "gen": 3 if dev.model == "ht_gen3" else 1}
            entry["_dev_info"].update({"code": f"CODE-{dev.model}", "online": False})
        status[dev.id] = entry
    return {"isok": True, "data": {"devices_status": status}}


def _v2_doc(clock: FakeClock, devices: tuple[Device, ...], *, settings: bool) -> list[dict]:
    updated = _stamp(clock.time() - 600)
    entries: list[dict] = []
    for dev in devices:
        if dev.model == "flood_gen1":
            continue  # absent from v2: the summary must say so
        entry: dict = {
            "id": dev.id,
            "type": "x",
            "code": f"CODE-{dev.model}",
            "gen": 3 if dev.model == "ht_gen3" else 1,
            "online": False,
            "status": {"sys": {"wakeup_period": 7200}} if dev.model == "ht_gen3" else {},
        }
        if dev.model == "ht_gen1":
            entry["status"]["_updated"] = updated
        if settings:
            entry["settings"] = {
                "sleep_mode": {"period": 12, "unit": "h"},
                "sensors": {"temperature_threshold": 1.0, "humidity_threshold": 5},
                "temperature_offset": 0,
                "humidity_offset": 0,
                "external_power": 0,
                "wifi_sta": {"ssid": FAB_SSID},
            }
        entries.append(entry)
    return entries


def matrix_responses(clock: FakeClock, devices: tuple[Device, ...]) -> list[object]:
    """One scripted response per FULL_STEPS entry, in step order."""
    first = devices[0].id
    return [
        _json_response(200, _v1_doc(clock, devices, info=False)),
        _json_response(200, _v1_doc(clock, devices, info=True)),
        _json_response(200, _v2_doc(clock, devices, settings=False)),
        _json_response(200, _v2_doc(clock, devices, settings=True)),
        _json_response(401, {"isok": False, "errors": ["invalid auth"]}),  # v2_key_in_body
        _json_response(429, {"error": "slow down"}, **{"retry-after": "30"}),  # v2_key_in_form
        _json_response(401, {"error": "no header auth"}),  # v2_key_in_header
        RawResponse(404, {"content-type": "text/html"}, f"<html>no route {FAB_IP}</html>".encode()),
        _json_response(401, {"isok": False, "errors": ["wrong key"]}),  # wrong_key_v1
        _json_response(401, {"error": f"bad key {DUMMY_KEY}"}),  # wrong_key_v2 echoes the key
        _json_response(200, [{"id": first, "code": 200}, {"id": UNKNOWN_ID, "code": 404}]),
        TransportError("network"),  # wrong_host
    ]


def _client(settings: Settings, send: FakeSend, clock: FakeClock) -> ShellyClient:
    return ShellyClient(
        settings.host, settings.auth_key, send=send, guard=RateGuard(clock), clock=clock
    )


def _run(
    settings: Settings,
    devices: tuple[Device, ...],
    clock: FakeClock,
    out_root: Path,
    *,
    errors: bool = False,
    wrong_host: bool = False,
    responses: list[object] | None = None,
    **kwargs: object,
) -> tuple[int, FakeSend]:
    script = responses if responses is not None else matrix_responses(clock, devices)
    count = len(DEFAULT_STEPS) + (len(ERROR_STEPS) if errors else 0) + (1 if wrong_host else 0)
    send = FakeSend(clock, *script[: len(DEFAULT_STEPS)])
    if errors:
        send.queue(*script[len(DEFAULT_STEPS) : len(DEFAULT_STEPS) + len(ERROR_STEPS)])
    if wrong_host:
        send.queue(script[len(DEFAULT_STEPS) + len(ERROR_STEPS)])
    assert count == len(send._script)
    code = run_probe(
        settings,
        devices,
        _client(settings, send, clock),
        clock,
        out_root=out_root,
        errors=errors,
        wrong_host=wrong_host,
        **kwargs,
    )
    return code, send


def _only_run_dir(out_root: Path) -> Path:
    (run_dir,) = list(out_root.iterdir())
    return run_dir


def _call_text(call: SendCall) -> str:
    return json.dumps([call.url, call.form, call.json_body, dict(call.headers or {})], default=str)


# ---------------------------------------------------------------------------
# sibling_host and constants
# ---------------------------------------------------------------------------


def test_sibling_host_table():
    assert sibling_host("shelly-12-eu.shelly.cloud") == "shelly-13-eu.shelly.cloud"
    assert sibling_host("shelly-99-eu.shelly.cloud") == "shelly-98-eu.shelly.cloud"
    assert sibling_host("shelly-00-eu.shelly.cloud") == "shelly-01-eu.shelly.cloud"
    assert sibling_host("example.org") is None
    assert sibling_host("shelly-1-eu.shelly.cloud") is None


def test_constants():
    assert DUMMY_KEY == "probe-invalid-key-000000000000000000"
    assert UNKNOWN_ID == "fffffffffffe"
    assert int(UNKNOWN_ID[:2], 16) & 1  # multicast bit: no real device can own it


# ---------------------------------------------------------------------------
# Step order and request shapes
# ---------------------------------------------------------------------------


def test_default_steps_and_request_shapes(test_settings, test_devices, fake_clock, tmp_path):
    code, send = _run(test_settings, test_devices, fake_clock, tmp_path / "probe")

    assert code == 0
    index = json.loads((_only_run_dir(tmp_path / "probe") / "index.json").read_text())
    assert [e["step"] for e in index] == DEFAULT_STEPS
    v1, info, v2s, v2set = send.calls
    ids = [d.id for d in test_devices]

    assert urlsplit(v1.url).path == "/device/all_status"
    assert urlsplit(v1.url).query == ""
    assert v1.form == {"auth_key": SENTINEL_KEY}

    assert urlsplit(info.url).path == "/device/all_status"
    assert parse_qs(urlsplit(info.url).query) == {"show_info": ["true"], "no_shared": ["true"]}
    assert info.form == {"auth_key": SENTINEL_KEY}

    for call, select in ((v2s, ["status"]), (v2set, ["status", "settings"])):
        assert urlsplit(call.url).netloc == TEST_HOST
        assert urlsplit(call.url).path == "/v2/devices/api/get"
        assert parse_qs(urlsplit(call.url).query) == {"auth_key": [SENTINEL_KEY]}
        assert call.json_body == {"ids": ids, "select": select}
        assert call.form is None


def test_error_group_order_and_key_placements(test_settings, test_devices, fake_clock, tmp_path):
    _code, send = _run(test_settings, test_devices, fake_clock, tmp_path / "probe", errors=True)

    index = json.loads((_only_run_dir(tmp_path / "probe") / "index.json").read_text())
    assert [e["step"] for e in index] == [*DEFAULT_STEPS, *ERROR_STEPS]
    by_step = {e["step"]: e["key_placement"] for e in index}
    assert by_step == {
        "v1_all_status": "form",
        "v1_all_status_info": "form",
        "v2_get_status": "query",
        "v2_get_settings": "query",
        "v2_key_in_body": "json",
        "v2_key_in_form": "form",
        "v2_key_in_header": "header",
        "v1_device_settings": "form",
        "wrong_key_v1": "dummy",
        "wrong_key_v2": "dummy",
        "unknown_id_v2": "query",
    }
    calls = dict(zip([e["step"] for e in index], send.calls, strict=True))
    body_call = calls["v2_key_in_body"]
    assert urlsplit(body_call.url).query == ""
    assert body_call.json_body["auth_key"] == SENTINEL_KEY
    form_call = calls["v2_key_in_form"]
    assert form_call.json_body is None
    assert form_call.form is not None and form_call.form["auth_key"] == SENTINEL_KEY
    assert set(form_call.form) == {"ids", "select", "auth_key"}
    header_call = calls["v2_key_in_header"]
    assert SENTINEL_KEY in (header_call.headers or {}).get("Authorization", "")
    assert SENTINEL_KEY not in header_call.url
    settings_call = calls["v1_device_settings"]
    assert urlsplit(settings_call.url).path == "/device/settings"
    assert settings_call.form == {"id": test_devices[0].id, "auth_key": SENTINEL_KEY}
    unknown = calls["unknown_id_v2"]
    assert unknown.json_body["ids"] == [test_devices[0].id, UNKNOWN_ID]


def test_wrong_host_goes_to_the_sibling_with_the_dummy_key(
    test_settings, test_devices, fake_clock, tmp_path
):
    _code, send = _run(
        test_settings, test_devices, fake_clock, tmp_path / "probe", errors=True, wrong_host=True
    )

    last = send.calls[-1]
    assert last.url == f"https://{SIBLING}/device/all_status"
    assert last.form == {"auth_key": DUMMY_KEY}
    index = json.loads((_only_run_dir(tmp_path / "probe") / "index.json").read_text())
    assert [e["step"] for e in index] == FULL_STEPS
    assert index[-1]["key_placement"] == "dummy"
    assert index[-1]["category"] == "network"


def test_wrong_host_is_opt_in(test_settings, test_devices, fake_clock, tmp_path):
    _code, send = _run(test_settings, test_devices, fake_clock, tmp_path / "probe", errors=True)

    assert all(urlsplit(call.url).netloc == TEST_HOST for call in send.calls)


def test_wrong_host_refused_without_a_sibling_before_any_request(
    test_settings, test_devices, fake_clock, tmp_path, capsys
):
    settings = Settings(
        auth_key=test_settings.auth_key,
        host="example.org",
        db_path=test_settings.db_path,
        devices_file=test_settings.devices_file,
        poll_interval_s=60.0,
        log_level="INFO",
    )
    send = FakeSend(fake_clock)

    code = run_probe(
        settings,
        test_devices,
        _client(settings, send, fake_clock),
        fake_clock,
        out_root=tmp_path / "probe",
        wrong_host=True,
    )

    captured = capsys.readouterr()
    assert code == 2
    assert send.calls == []
    assert not (tmp_path / "probe").exists()
    assert "example.org" not in captured.out + captured.err


# ---------------------------------------------------------------------------
# Rate guard and key confinement
# ---------------------------------------------------------------------------


def test_full_matrix_spacing_is_at_least_1_1_s(test_settings, test_devices, fake_clock, tmp_path):
    _code, send = _run(
        test_settings, test_devices, fake_clock, tmp_path / "probe", errors=True, wrong_host=True
    )

    assert len(send.calls) == len(FULL_STEPS)
    starts = [call.at for call in send.calls]
    assert all(later - earlier >= 1.1 for earlier, later in itertools.pairwise(starts))


def test_dummy_key_steps_never_carry_the_real_key(
    test_settings, test_devices, fake_clock, tmp_path
):
    _code, send = _run(
        test_settings, test_devices, fake_clock, tmp_path / "probe", errors=True, wrong_host=True
    )

    calls = dict(zip(FULL_STEPS, send.calls, strict=True))
    for step in ("wrong_key_v1", "wrong_key_v2", "wrong_host"):
        text = _call_text(calls[step])
        assert DUMMY_KEY in text
        assert SENTINEL_KEY not in text
    for call in send.calls:
        if urlsplit(call.url).netloc != TEST_HOST:
            assert SENTINEL_KEY not in _call_text(call)


def test_real_key_goes_only_to_the_configured_host(
    test_settings, test_devices, fake_clock, tmp_path
):
    _code, send = _run(
        test_settings, test_devices, fake_clock, tmp_path / "probe", errors=True, wrong_host=True
    )

    carrying = [c for c in send.calls if SENTINEL_KEY in _call_text(c)]
    assert carrying
    assert {urlsplit(c.url).netloc for c in carrying} == {TEST_HOST}


# ---------------------------------------------------------------------------
# Outputs carry no identifiers
# ---------------------------------------------------------------------------


def test_index_stdout_stderr_hold_no_url_host_key_or_id(
    test_settings, test_devices, fake_clock, tmp_path, capsys
):
    _run(test_settings, test_devices, fake_clock, tmp_path / "probe", errors=True, wrong_host=True)

    captured = capsys.readouterr()
    index_text = (_only_run_dir(tmp_path / "probe") / "index.json").read_text()
    index = json.loads(index_text)
    assert all(set(entry) == INDEX_KEYS for entry in index)
    assert {e["key_placement"] for e in index} <= PLACEMENTS
    forbidden = (
        SENTINEL_KEY,
        TEST_HOST,
        SIBLING,
        "shelly.cloud",
        "https://",
        "auth_key",
        "0200000000",
        DUMMY_KEY,
        UNKNOWN_ID,
        FAB_MAC,
        FAB_IP,
        FAB_SSID,
    )
    for text in (index_text, captured.out, captured.err):
        for item in forbidden:
            assert item not in text


def test_sentinel_absent_even_with_a_url_encoded_key(test_devices, fake_clock, tmp_path, capsys):
    odd_key = "key with+odd/chars=1&x"
    settings = Settings(
        auth_key=Secret(odd_key),
        host=TEST_HOST,
        db_path=tmp_path / "data" / "shelog.db",
        devices_file=tmp_path / "devices.toml",
        poll_interval_s=60.0,
        log_level="INFO",
    )
    _run(settings, test_devices, fake_clock, tmp_path / "probe", errors=True)

    captured = capsys.readouterr()
    index_text = (_only_run_dir(tmp_path / "probe") / "index.json").read_text()
    for text in (index_text, captured.out, captured.err):
        assert odd_key not in text
        assert "odd/chars" not in text
        assert "odd%2Fchars" not in text


def test_summary_is_name_only_and_answers_the_open_questions(
    test_settings, test_devices, fake_clock, tmp_path, capsys
):
    _run(test_settings, test_devices, fake_clock, tmp_path / "probe", errors=True, wrong_host=True)

    out = capsys.readouterr().out
    assert "T-HT1 [v2]: present: yes, _updated in status: yes" in out
    assert "T-HT3 [v2]: present: yes, _updated in status: no" in out
    assert "T-FLOOD [v2]: present: no" in out
    assert "T-HT1 [info]: gen 1, code CODE-ht_gen1, online false" in out
    assert "T-HT3 [info]: gen 3, code CODE-ht_gen3, online false" in out
    ht1 = next(ln for ln in out.splitlines() if ln.startswith("T-HT1 [reporting]:"))
    for item in (
        "sleep_mode.period=12",
        "sleep_mode.unit=h",
        "sensors.temperature_threshold=1.0",
        "sensors.humidity_threshold=5",
        "temperature_offset=0",
        "humidity_offset=0",
        "external_power=0",
    ):
        assert item in ht1
    ht3 = next(ln for ln in out.splitlines() if ln.startswith("T-HT3 [reporting]:"))
    assert "sys.wakeup_period=7200" in ht3
    assert "wrong_key_v1: category auth, HTTP 401" in out
    assert "v1_device_settings: category endpoint_gone, HTTP 404" in out
    assert "v2_key_in_form: category rate_limited, HTTP 429" in out
    assert "wrong_host: category network, HTTP none" in out


def test_raw_bodies_go_to_a_private_directory(test_settings, test_devices, fake_clock, tmp_path):
    out_root = tmp_path / "probe"
    _run(test_settings, test_devices, fake_clock, out_root, errors=True)

    run_dir = _only_run_dir(out_root)
    assert stat.S_IMODE(out_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    raw = json.loads((run_dir / "v2_get_settings.json").read_text())
    assert raw[0]["id"] == test_devices[0].id  # verbatim, ids and all
    assert not (run_dir / "wrong_host.json").exists()


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------


def test_held_lock_makes_probe_return_3_without_sending(
    test_settings, test_devices, fake_clock, tmp_path, capsys
):
    send = FakeSend(fake_clock)
    handle = acquire_instance_lock(test_settings.db_path.parent / "poller.lock")
    try:
        code = probe.main(
            test_settings, test_devices, clock=fake_clock, send=send, out_root=tmp_path / "probe"
        )
    finally:
        handle.close()

    captured = capsys.readouterr()
    assert code == 3
    assert send.calls == []
    assert SENTINEL_KEY not in captured.out + captured.err


def test_probe_main_runs_when_the_lock_is_free(test_settings, test_devices, fake_clock, tmp_path):
    script = matrix_responses(fake_clock, test_devices)[: len(DEFAULT_STEPS)]
    send = FakeSend(fake_clock, *script)

    code = probe.main(
        test_settings, test_devices, clock=fake_clock, send=send, out_root=tmp_path / "probe"
    )

    assert code == 0
    assert len(send.calls) == len(DEFAULT_STEPS)
