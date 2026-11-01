"""Shelly Cloud client: rate guard, outcome classification and the all_status call.

Nothing here logs URLs, request bodies or exception text: the auth key travels in the
POST body and must never reach a log line.
"""

from __future__ import annotations

import email.utils
import json
import threading
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from shelog.clock import Clock
from shelog.config import Secret
from shelog.shelly import transport
from shelog.shelly.transport import RawResponse, TransportError

OK = "ok"
AUTH = "auth"
RATE_LIMITED = "rate_limited"
HTTP_5XX = "http_5xx"
HTTP_OTHER = "http_other"
TIMEOUT = "timeout"
NETWORK = "network"
BAD_RESPONSE = "bad_response"
NO_DEVICES = "no_devices"
ENDPOINT_GONE = "endpoint_gone"
INTERNAL = "internal"

ERROR_CATEGORIES = frozenset(
    {
        AUTH,
        RATE_LIMITED,
        HTTP_5XX,
        HTTP_OTHER,
        TIMEOUT,
        NETWORK,
        BAD_RESPONSE,
        NO_DEVICES,
        ENDPOINT_GONE,
        INTERNAL,
    }
)


@dataclass(frozen=True)
class Outcome:
    category: str
    http_status: int | None
    latency_ms: int | None = None
    retry_after_s: float | None = None
    devices: Mapping[str, Any] | None = None
    body_excerpt: str | None = None  # bad_response only; log-only, never stored

    @property
    def ok(self) -> bool:
        return self.category == OK


class Interrupted(Exception):
    """Raised by request() when the stop event is set while waiting on the rate guard."""


def parse_retry_after(headers: Mapping[str, str], now: float) -> float | None:
    """Parse Retry-After (delta-seconds or HTTP-date), clamped to [1, 900]."""
    value = headers.get("retry-after")
    if value is None:
        return None
    value = value.strip()
    seconds: float
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = when.timestamp() - now
    if seconds != seconds:  # NaN
        return None
    return min(900.0, max(1.0, seconds))


def _reject_constant(name: str) -> Any:
    raise ValueError(name)


def _safe_json(body: bytes) -> dict[str, Any] | None:
    try:
        doc = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


EXCERPT_CHARS = 200


def _bad_response(raw: RawResponse) -> Outcome:
    excerpt = raw.body[: EXCERPT_CHARS * 4].decode("utf-8", errors="replace")[:EXCERPT_CHARS]
    return Outcome(BAD_RESPONSE, raw.status, body_excerpt=excerpt)


def classify(raw: RawResponse, now: float) -> Outcome:
    """Map a raw response to a closed outcome category."""
    status = raw.status
    # Status decides first: a CDN/WAF HTML page on 401/403/429/5xx is still an auth, rate
    # or server problem. Only a 200 that is HTML means the endpoint no longer speaks JSON.
    if status in (404, 410) or (status == 200 and raw.body.lstrip().startswith(b"<")):
        return Outcome(ENDPOINT_GONE, status)
    if status == 429:
        return Outcome(RATE_LIMITED, status, retry_after_s=parse_retry_after(raw.headers, now))
    if status in (401, 403):
        return Outcome(AUTH, status)
    if 500 <= status < 600:
        return Outcome(HTTP_5XX, status)
    if status != 200:
        return Outcome(HTTP_OTHER, status)
    doc = _safe_json(raw.body)
    if doc is None:
        return _bad_response(raw)
    if doc.get("isok") is not True:
        errors = doc.get("errors")
        if isinstance(errors, dict) and "max_req" in errors:
            return Outcome(RATE_LIMITED, status)
        return _bad_response(raw)
    data = doc.get("data")
    devices = data.get("devices_status") if isinstance(data, dict) else None
    if devices == {} or devices == []:
        return Outcome(NO_DEVICES, status)
    if not isinstance(devices, dict):
        return _bad_response(raw)
    return Outcome(OK, status, devices=devices)


class RateGuard:
    """Spaces requests at least ``min_gap`` seconds apart (account limit is 1 req/s)."""

    def __init__(self, clock: Clock, min_gap: float = 1.1) -> None:
        self._clock = clock
        self._min_gap = min_gap
        self._last: float | None = None

    def acquire(self, stop: threading.Event) -> bool:
        if self._last is not None:
            remaining = self._last + self._min_gap - self._clock.monotonic()
            if remaining > 0 and self._clock.wait(stop, remaining):
                return False  # stopping
        self._last = self._clock.monotonic()
        return True


class ShellyClient:
    def __init__(
        self,
        host: str,
        key: Secret,
        *,
        send: Callable[..., RawResponse] = transport.send,
        guard: RateGuard,
        clock: Clock,
        timeout: float = 15.0,
    ) -> None:
        self._host = host
        self._key = key
        self._send = send
        self._guard = guard
        self._clock = clock
        self._timeout = timeout

    @property
    def key(self) -> Secret:
        return self._key

    def for_host(self, host: str, key: Secret) -> ShellyClient:
        """A client for another host that shares this client's rate guard and transport.

        The probe uses it for its wrong-host step; sharing the guard keeps the 1 req/s
        spacing across both hosts (the limit is account-wide).
        """
        return ShellyClient(
            host,
            key,
            send=self._send,
            guard=self._guard,
            clock=self._clock,
            timeout=self._timeout,
        )

    def request(
        self,
        path: str,
        *,
        form: Mapping[str, str] | None = None,
        json_body: Any = None,
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        stop: threading.Event | None = None,
    ) -> RawResponse:
        """POST to the Shelly host. Always passes through the rate guard first."""
        if not self._guard.acquire(stop if stop is not None else threading.Event()):
            raise Interrupted
        url = f"https://{self._host}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return self._send(
            url, form=form, json_body=json_body, headers=headers, timeout=self._timeout
        )

    def fetch_all_status(self, stop: threading.Event) -> Outcome:
        started = self._clock.monotonic()
        try:
            raw = self.request(
                "/device/all_status", form={"auth_key": self._key.reveal()}, stop=stop
            )
        except TransportError as err:
            return Outcome(err.category, None, latency_ms=self._latency_ms(started))
        outcome = classify(raw, self._clock.time())
        return replace(outcome, latency_ms=self._latency_ms(started))

    def _latency_ms(self, started: float) -> int:
        return round((self._clock.monotonic() - started) * 1000)
