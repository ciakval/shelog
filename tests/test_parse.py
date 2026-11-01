"""Per-model parsing rules against the sanitized live capture and the synthetic cases."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from shelog.shelly.parse import ParseReject, Report, parse_device

FIXTURES = Path(__file__).parent / "fixtures" / "shelly"
UNIX = 1793515800  # 2026-11-01 06:50:00 UTC, the `_updated` of every synthetic baseline
DEVICE = "020000000001"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


LIVE = load("v1_all_status.json")["data"]["devices_status"]
CASES = load("synthetic_cases.json")


def infer_model(block: dict[str, Any]) -> str:
    if "temperature:0" in block:
        return "ht_gen3"
    if "flood" in block:
        return "flood_gen1"
    return "ht_gen1"


def parsed(model: str, case: str) -> Report:
    result = parse_device(model, DEVICE, copy.deepcopy(CASES[model][case]))
    assert isinstance(result, Report), result
    return result


# ---------------------------------------------------------------------------
# Sanitized live capture
# ---------------------------------------------------------------------------


def test_live_fixture_covers_all_three_models() -> None:
    models = {infer_model(block) for block in LIVE.values()}
    assert models == {"ht_gen1", "ht_gen3", "flood_gen1"}
    assert len(LIVE) == 7


@pytest.mark.parametrize("device_id", sorted(LIVE), ids=lambda d: f"dev-{d[-2:]}")
def test_every_live_block_parses_to_a_cloud_report(device_id: str) -> None:
    block = LIVE[device_id]
    model = infer_model(block)
    report = parse_device(model, device_id, block)

    assert isinstance(report, Report), report
    expected_ts = int(datetime.fromisoformat(block["_updated"] + "+00:00").timestamp())
    assert report.ts == expected_ts
    assert report.ts_source == "cloud"
    assert report.device_id == device_id
    assert json.loads(report.raw) == block
    if model == "ht_gen3":
        assert report.t_c == block["temperature:0"]["tC"]
        assert report.rh == block["humidity:0"]["rh"]
        assert report.battery == block["devicepower:0"]["battery"]["percent"]
        assert report.flood is None
    else:
        assert report.t_c == block["tmp"]["tC"]
        assert report.battery == block["bat"]["value"]
        if model == "flood_gen1":
            assert report.rh is None
            assert report.flood == int(block["flood"])
        else:
            assert report.rh == block["hum"]["value"]
            assert report.flood is None
    assert report.field_rejects == 0


# ---------------------------------------------------------------------------
# Synthetic cases
# ---------------------------------------------------------------------------

BASE_GEN1 = {"t_c": 24.5, "rh": 54.5, "battery": 90.0, "flood": None, "field_rejects": 0}
BASE_GEN3 = {"t_c": 25.2, "rh": 49.9, "battery": 90.0, "flood": None, "field_rejects": 0}
BASE_FLOOD = {"t_c": 24.6, "rh": None, "battery": 100.0, "flood": 0, "field_rejects": 0}

TABLE = [
    ("ht_gen1", "baseline", BASE_GEN1),
    ("ht_gen1", "sensor_error_1", {**BASE_GEN1, "t_c": None, "rh": None}),
    ("ht_gen1", "tmp_invalid", {**BASE_GEN1, "t_c": None}),
    ("ht_gen1", "hum_invalid", {**BASE_GEN1, "rh": None}),
    ("ht_gen1", "tmp_null", {**BASE_GEN1, "t_c": None}),
    ("ht_gen1", "hum_int_100", {**BASE_GEN1, "rh": 100.0}),
    ("ht_gen1", "rh_100_4", {**BASE_GEN1, "rh": 100.4}),  # stored raw, never clamped
    ("ht_gen1", "bat_null", {**BASE_GEN1, "battery": None}),
    ("ht_gen1", "tc_bool", {**BASE_GEN1, "t_c": None, "field_rejects": 1}),
    ("ht_gen1", "tc_nan", {**BASE_GEN1, "t_c": None, "field_rejects": 1}),
    ("ht_gen1", "tc_150", {**BASE_GEN1, "t_c": None, "field_rejects": 1}),
    ("ht_gen1", "hum_120", {**BASE_GEN1, "rh": None, "field_rejects": 1}),
    ("ht_gen1", "old_firmware", BASE_GEN1),
    ("ht_gen3", "baseline", BASE_GEN3),
    ("ht_gen3", "tc_null", {**BASE_GEN3, "t_c": None}),
    ("ht_gen3", "rh_null", {**BASE_GEN3, "rh": None}),
    ("ht_gen3", "rh_string", {**BASE_GEN3, "rh": None, "field_rejects": 1}),
    ("ht_gen3", "clock_skew_600s", BASE_GEN3),
    ("ht_gen3", "clock_within_tolerance", BASE_GEN3),
    ("flood_gen1", "baseline", BASE_FLOOD),
    ("flood_gen1", "flood_true", {**BASE_FLOOD, "flood": 1}),
    ("flood_gen1", "flood_yes", {**BASE_FLOOD, "flood": None, "field_rejects": 1}),
    ("flood_gen1", "tmp_invalid_value", {**BASE_FLOOD, "t_c": None, "field_rejects": 1}),
]


@pytest.mark.parametrize(
    ("model", "case", "expected"), TABLE, ids=lambda v: v if isinstance(v, str) else ""
)
def test_synthetic_case_values(model: str, case: str, expected: dict[str, Any]) -> None:
    report = parsed(model, case)
    assert report.ts == UNIX
    assert report.ts_source == "cloud"
    for field, value in expected.items():
        assert getattr(report, field) == value, field


def test_rh_is_a_float_even_when_the_device_sends_an_int() -> None:
    assert isinstance(parsed("ht_gen1", "hum_int_100").rh, float)


def test_absurd_values_add_a_warning_and_a_reject() -> None:
    report = parsed("ht_gen1", "tc_150")
    assert report.field_rejects == 1
    assert any("temperature" in w and "NULL" in w for w in report.warnings)
    humid = parsed("ht_gen1", "hum_120")
    assert any("humidity" in w for w in humid.warnings)


def test_ordinary_nulls_do_not_warn_or_count() -> None:
    for model, case in [
        ("ht_gen1", "tmp_invalid"),
        ("ht_gen3", "tc_null"),
        ("ht_gen1", "bat_null"),
    ]:
        report = parsed(model, case)
        assert report.warnings == ()
        assert report.field_rejects == 0


def test_old_firmware_without_getinfo_or_sensor_error_parses_normally() -> None:
    block = CASES["ht_gen1"]["old_firmware"]
    assert "getinfo" not in block
    assert "sensor_error" not in block
    assert block["unixtime"] == 0
    assert parsed("ht_gen1", "old_firmware").warnings == ()


def test_gen3_device_clock_skew_warns_but_ts_stays_the_cloud_time() -> None:
    report = parsed("ht_gen3", "clock_skew_600s")
    assert report.ts == UNIX
    assert any("device clock differs from cloud report time by 600 s" in w for w in report.warnings)
    assert parsed("ht_gen3", "clock_within_tolerance").warnings == ()


def test_gen1_device_clock_skew_warns_only_when_unixtime_is_positive() -> None:
    skewed = copy.deepcopy(CASES["ht_gen1"]["baseline"])
    skewed["unixtime"] = UNIX + 300
    report = parse_device("ht_gen1", DEVICE, skewed)
    assert isinstance(report, Report)
    assert report.ts == UNIX
    assert any("differs" in w for w in report.warnings)
    assert parsed("ht_gen1", "baseline").warnings == ()


def test_absent_updated_gives_a_poll_source_report_with_values() -> None:
    report = parsed("ht_gen1", "updated_absent")
    assert report.ts is None
    assert report.ts_source == "poll"
    assert report.t_c == 24.5


@pytest.mark.parametrize("case", ["updated_garbage", "updated_garbage_type"])
def test_present_but_garbage_updated_is_rejected(case: str) -> None:
    result = parse_device("ht_gen1", DEVICE, copy.deepcopy(CASES["ht_gen1"][case]))
    assert result == ParseReject(DEVICE, "bad_ts")


@pytest.mark.parametrize(
    ("model", "block"),
    [
        ("ht_gen1", {}),
        ("ht_gen1", "not a dict"),
        ("ht_gen1", None),
        ("ht_gen1", []),
        ("ht_gen3", {"_updated": "2026-11-01 06:50:00", "tmp": {}}),  # gen1 shape, gen3 model
        ("flood_gen1", {"_updated": "2026-11-01 06:50:00", "tmp": {"tC": 1}}),
        ("unknown_model", {"tmp": {}}),
    ],
)
def test_wrong_shapes_are_rejected_not_raised(model: str, block: Any) -> None:
    assert parse_device(model, DEVICE, block) == ParseReject(DEVICE, "shape")


@pytest.mark.parametrize(
    "block",
    [
        {"_updated": "2026-11-01 06:50:00", "tmp": 5},
        {"_updated": "2026-11-01 06:50:00", "tmp": [], "hum": "x", "bat": 3},
        {"_updated": "2026-11-01 06:50:00", "tmp": {"tC": {}, "is_valid": True}, "hum": None},
    ],
)
def test_junk_inside_the_anchor_never_raises(block: Any) -> None:
    result = parse_device("ht_gen1", DEVICE, block)
    assert isinstance(result, Report)
    assert result.t_c is None
    assert result.rh is None
