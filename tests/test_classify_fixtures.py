"""classify() pinned to the captured real envelopes and the synthetic rate-limit cases.

errors.json comes from the one live `shelog probe --errors --wrong-host` run (plan 01-07);
synthetic_rate_limit.json is the only rate-limit evidence, because the real Shelly throttle
envelope was never provoked (no deliberate bursts).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import FIXTURES

from shelog.shelly.client import (
    AUTH,
    BAD_RESPONSE,
    ERROR_CATEGORIES,
    HTTP_OTHER,
    OK,
    RATE_LIMITED,
    classify,
)
from shelog.shelly.transport import RawResponse

NOW = 1_800_000_000.0

ERRORS: dict[str, dict[str, Any]] = json.loads((FIXTURES / "errors.json").read_text())
SYNTHETIC: dict[str, dict[str, Any]] = json.loads(
    (FIXTURES / "synthetic_rate_limit.json").read_text()
)["cases"]

# Category per captured step, written down here from the evidence (not read back from the
# fixture), so a change to either the classifier or the fixture fails the test.
EVIDENCE = {
    # HTTP 401 UNAUTHORIZED / invalid_token: same body for v1, v2, the header key, the sibling host.
    "wrong_key_v1": AUTH,
    "wrong_key_v2": AUTH,
    "v2_key_in_header": AUTH,
    "wrong_host": AUTH,
    # HTTP 400 VALIDATION_ERRORS: v2 does not read a form-encoded body.
    "v2_key_in_form": HTTP_OTHER,
    # HTTP 200 with a v2 list or a v1 settings document: not a v1 all_status envelope.
    "v2_key_in_body": BAD_RESPONSE,
    "unknown_id_v2": BAD_RESPONSE,
    "v1_device_settings": BAD_RESPONSE,
}


def _raw(status: int, has_retry_after: bool, body: Any) -> RawResponse:
    if isinstance(body, dict) and set(body) == {"_non_json_body"}:
        payload = str(body["_non_json_body"]).encode()
    elif isinstance(body, str):
        payload = body.encode()
    else:
        payload = json.dumps(body).encode()
    headers = {"retry-after": "1"} if has_retry_after else {}
    return RawResponse(status, headers, payload)


def test_errors_json_covers_every_evidence_step_with_the_documented_shape():
    assert set(EVIDENCE) <= set(ERRORS)
    for entry in ERRORS.values():
        assert set(entry) == {
            "http_status",
            "has_retry_after",
            "key_placement",
            "expected_category",
            "body",
        }


@pytest.mark.parametrize("step", sorted(ERRORS))
def test_classify_matches_the_recorded_category(step):
    entry = ERRORS[step]
    if entry["http_status"] is None:
        pytest.skip("no response was received for this step")
    raw = _raw(entry["http_status"], entry["has_retry_after"], entry["body"])
    assert classify(raw, NOW).category == entry["expected_category"]
    assert entry["expected_category"] in ERROR_CATEGORIES | {OK}


@pytest.mark.parametrize("step", sorted(EVIDENCE))
def test_captured_envelopes_have_the_category_from_the_evidence(step):
    entry = ERRORS[step]
    raw = _raw(entry["http_status"], entry["has_retry_after"], entry["body"])
    assert classify(raw, NOW).category == EVIDENCE[step]
    assert entry["expected_category"] == EVIDENCE[step]


def test_the_wrong_host_answers_exactly_like_a_wrong_key():
    assert ERRORS["wrong_host"]["http_status"] == ERRORS["wrong_key_v1"]["http_status"] == 401
    assert ERRORS["wrong_host"]["body"] == ERRORS["wrong_key_v1"]["body"]


def test_a_captured_401_never_looks_like_a_throttle():
    for step in ("wrong_key_v1", "wrong_key_v2", "wrong_host", "v2_key_in_header"):
        assert not ERRORS[step]["has_retry_after"]
        assert "max_req" not in json.dumps(ERRORS[step]["body"])


def test_the_real_v1_info_capture_classifies_ok_with_all_devices():
    doc = (FIXTURES / "v1_all_status_info.json").read_bytes()
    outcome = classify(RawResponse(200, {}, doc), NOW)
    assert outcome.category == OK
    assert outcome.devices is not None and len(outcome.devices) == 7


@pytest.mark.parametrize("fixture", ["v2_get_status.json", "v2_get_settings.json"])
def test_v2_success_lists_are_not_v1_envelopes(fixture):
    # v2 is probe-only (no poll path), so a v2 list is deliberately not an OK poll response.
    outcome = classify(RawResponse(200, {}, (FIXTURES / fixture).read_bytes()), NOW)
    assert outcome.category == BAD_RESPONSE


@pytest.mark.parametrize("case", sorted(SYNTHETIC))
def test_synthetic_rate_limit_cases(case):
    entry = SYNTHETIC[case]
    raw = _raw(entry["status"], False, entry["body"])
    raw = RawResponse(raw.status, dict(entry["headers"]), raw.body)
    outcome = classify(raw, NOW)
    assert outcome.category == entry["expected_category"] == RATE_LIMITED
    if "retry-after" in entry["headers"]:
        assert outcome.retry_after_s == pytest.approx(float(entry["headers"]["retry-after"]))
