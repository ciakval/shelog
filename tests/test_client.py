"""Classifier table, Retry-After parsing, rate guard and request building (offline)."""

from __future__ import annotations

import itertools
import json
import re
import threading

import pytest
from conftest import FIXTURES, SENTINEL_KEY, TEST_HOST, FakeClock, FakeSend

from shelog.config import Secret
from shelog.shelly.client import (
    AUTH,
    BAD_RESPONSE,
    ENDPOINT_GONE,
    ERROR_CATEGORIES,
    HTTP_5XX,
    HTTP_OTHER,
    NETWORK,
    NO_DEVICES,
    OK,
    RATE_LIMITED,
    Interrupted,
    RateGuard,
    ShellyClient,
    classify,
    parse_retry_after,
)
from shelog.shelly.transport import RawResponse, TransportError

NOW = 1_800_000_000.0
JSON_HEADERS = {"content-type": "application/json"}


def _json(doc: object, status: int = 200) -> RawResponse:
    return RawResponse(status, JSON_HEADERS, json.dumps(doc).encode())


def _envelope(devices: object) -> dict:
    return {"isok": True, "data": {"devices_status": devices}}


CASES = [
    ("404", RawResponse(404, {}, b""), ENDPOINT_GONE),
    ("410", RawResponse(410, {}, b"gone"), ENDPOINT_GONE),
    ("html_200", RawResponse(200, {}, b"<!doctype html><html></html>"), ENDPOINT_GONE),
    ("401", RawResponse(401, {}, b""), AUTH),
    ("403", RawResponse(403, {}, b""), AUTH),
    ("429", RawResponse(429, {}, b""), RATE_LIMITED),
    ("500", RawResponse(500, {}, b""), HTTP_5XX),
    ("502", RawResponse(502, {}, b""), HTTP_5XX),
    ("503", RawResponse(503, {}, b""), HTTP_5XX),
    ("418", RawResponse(418, {}, b""), HTTP_OTHER),
    ("not_json", RawResponse(200, {}, b"plain text"), BAD_RESPONSE),
    ("json_array", RawResponse(200, {}, b"[1, 2]"), BAD_RESPONSE),
    ("json_nan", RawResponse(200, {}, b'{"isok": NaN}'), BAD_RESPONSE),
    ("isok_false_max_req", _json({"isok": False, "errors": {"max_req": "x"}}), RATE_LIMITED),
    ("isok_false_other", _json({"isok": False, "errors": {"foo": "x"}}), BAD_RESPONSE),
    ("isok_false_no_errors", _json({"isok": False}), BAD_RESPONSE),
    ("devices_empty_dict", _json(_envelope({})), NO_DEVICES),
    ("devices_empty_list", _json(_envelope([])), NO_DEVICES),
    ("devices_string", _json(_envelope("oops")), BAD_RESPONSE),
    ("valid", _json(_envelope({"020000000001": {"x": 1}})), OK),
]


HTML_BODY = b"<!doctype html><html><body>blocked</body></html>"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AUTH),
        (403, AUTH),
        (429, RATE_LIMITED),
        (500, HTTP_5XX),
        (502, HTTP_5XX),
        (503, HTTP_5XX),
        (418, HTTP_OTHER),
        (404, ENDPOINT_GONE),
        (410, ENDPOINT_GONE),
    ],
)
def test_html_error_pages_are_classified_by_status_first(status, expected):
    assert classify(RawResponse(status, {}, HTML_BODY), NOW).category == expected


def test_html_429_keeps_retry_after():
    outcome = classify(RawResponse(429, {"retry-after": "30"}, HTML_BODY), NOW)
    assert outcome.category == RATE_LIMITED
    assert outcome.retry_after_s == 30.0


@pytest.mark.parametrize(("raw", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_classify_table(raw, expected):
    assert classify(raw, NOW).category == expected


def test_classify_ok_carries_devices_and_status():
    outcome = classify(_json(_envelope({"020000000001": {"x": 1}})), NOW)
    assert outcome.ok
    assert outcome.http_status == 200
    assert outcome.devices == {"020000000001": {"x": 1}}


def test_429_carries_retry_after():
    outcome = classify(RawResponse(429, {"retry-after": "7"}, b""), NOW)
    assert outcome.category == RATE_LIMITED
    assert outcome.retry_after_s == 7.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7.0),
        ("0", 1.0),
        ("-5", 1.0),
        ("99999", 900.0),
        ("garbage", None),
        ("nan", None),
    ],
)
def test_parse_retry_after_values(value, expected):
    assert parse_retry_after({"retry-after": value}, NOW) == expected


def test_parse_retry_after_missing_header():
    assert parse_retry_after({}, NOW) is None


def test_parse_retry_after_http_date():
    from email.utils import formatdate

    assert parse_retry_after({"retry-after": formatdate(NOW + 30, usegmt=True)}, NOW) == 30.0


def test_error_categories_are_a_closed_slug_set():
    assert OK not in ERROR_CATEGORIES
    assert all(re.fullmatch(r"[a-z_0-9]+", c) for c in ERROR_CATEGORIES)


def test_bad_response_carries_only_size_and_kind_never_content():
    outcome = classify(RawResponse(200, {}, b"x" * 500), NOW)
    assert outcome.category == BAD_RESPONSE
    assert outcome.body_shape == "500 bytes, not json"
    assert "x" not in outcome.body_shape.replace("bytes", "")
    assert classify(RawResponse(200, {}, b"\xff\xfe not json"), NOW).body_shape is not None
    assert classify(RawResponse(200, {}, b"[1, 2]"), NOW).body_shape == "6 bytes, json list"
    leaky = _json({"isok": False, "data": {"devices_status": {"020000000001": {}}}})
    shape = classify(leaky, NOW).body_shape
    assert shape is not None
    assert "020000000001" not in shape
    assert "devices_status" not in shape
    assert classify(RawResponse(503, {}, b"body"), NOW).body_shape is None
    assert classify(_json(_envelope({"a": {}})), NOW).body_shape is None
    assert classify(RawResponse(429, {}, b"body"), NOW).body_shape is None


_RATE_CASES = json.loads((FIXTURES / "synthetic_rate_limit.json").read_text(encoding="utf-8"))[
    "cases"
]


@pytest.mark.parametrize("name", sorted(_RATE_CASES))
def test_synthetic_rate_limit_cases_classify_as_rate_limited(name):
    case = _RATE_CASES[name]
    body = case["body"]
    raw_body = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    outcome = classify(RawResponse(case["status"], case["headers"], raw_body), NOW)
    assert outcome.category == case["expected_category"] == RATE_LIMITED
    header = case["headers"].get("retry-after")
    assert outcome.retry_after_s == (min(900.0, max(1.0, float(header))) if header else None)


def test_rate_guard_spaces_five_acquires_by_at_least_min_gap():
    clock = FakeClock()
    guard = RateGuard(clock)
    stop = threading.Event()
    stamps = []
    for _ in range(5):
        assert guard.acquire(stop)
        stamps.append(clock.monotonic())
    assert all(b - a >= 1.1 - 1e-9 for a, b in itertools.pairwise(stamps))


def test_rate_guard_returns_false_when_stopped_during_the_wait():
    clock = FakeClock()
    guard = RateGuard(clock)
    stop = threading.Event()
    assert guard.acquire(stop)
    stop.set()
    assert guard.acquire(stop) is False


def _client(clock: FakeClock, send: FakeSend) -> ShellyClient:
    return ShellyClient(
        TEST_HOST, Secret(SENTINEL_KEY), send=send, guard=RateGuard(clock), clock=clock
    )


def test_request_raises_interrupted_when_stopped_in_the_guard():
    clock = FakeClock()
    send = FakeSend(clock, RawResponse(200, {}, b"{}"))
    client = _client(clock, send)
    stop = threading.Event()
    client.request("/x", stop=stop)
    stop.set()
    with pytest.raises(Interrupted):
        client.request("/x", stop=stop)
    assert len(send.calls) == 1


def test_request_builds_https_url_with_urlencoded_query():
    clock = FakeClock()
    send = FakeSend(clock, RawResponse(200, {}, b"{}"))
    _client(clock, send).request("/p", query={"a": "b c", "d": "1"})
    assert send.calls[0].url == f"https://{TEST_HOST}/p?a=b+c&d=1"


def test_fetch_all_status_maps_a_transport_error_to_its_category():
    clock = FakeClock()
    send = FakeSend(clock, TransportError("network"))
    outcome = _client(clock, send).fetch_all_status(threading.Event())
    assert outcome.category == NETWORK
    assert outcome.http_status is None
    assert SENTINEL_KEY not in repr(outcome)
