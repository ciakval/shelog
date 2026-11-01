"""Shared offline test fakes for the whole phase (no network, no real sleeping)."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from shelog.config import Device, Secret, Settings
from shelog.db import connect_rw, migrate
from shelog.shelly.transport import RawResponse

SENTINEL_KEY = "SENTINEL-KEY-0123456789abcdef"
TEST_HOST = "shelly-00-eu.shelly.cloud"
FIXTURES = Path(__file__).parent / "fixtures" / "shelly"
START_EPOCH = datetime(2026, 11, 1, 7, 0, 0, tzinfo=UTC).timestamp()


class FakeClock:
    """Deterministic clock: wait() advances time instead of sleeping."""

    def __init__(self, monotonic: float = 1000.0, time: float = START_EPOCH) -> None:
        self._monotonic = monotonic
        self._time = time

    def monotonic(self) -> float:
        return self._monotonic

    def time(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds
        self._time += seconds

    def wait(self, stop: threading.Event, timeout: float) -> bool:
        self.advance(timeout)
        return stop.is_set()


@dataclass(frozen=True)
class SendCall:
    url: str
    form: Mapping[str, str] | None
    json_body: Any
    headers: Mapping[str, str] | None
    timeout: float
    at: float  # fake monotonic time when the request was sent


class FakeSend:
    """Scripted stand-in for transport.send; records every call, fails when out of script."""

    def __init__(self, clock: FakeClock, *script: Any) -> None:
        self._clock = clock
        self._script: list[Any] = list(script)
        self.calls: list[SendCall] = []

    def queue(self, *items: Any) -> None:
        self._script.extend(items)

    def __call__(
        self,
        url: str,
        *,
        form: Mapping[str, str] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> RawResponse:
        call = SendCall(url, form, json_body, headers, timeout, self._clock.monotonic())
        self.calls.append(call)
        if not self._script:
            raise AssertionError("FakeSend script exhausted: unexpected extra request")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(call)
        return item


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sentinel_key() -> str:
    return SENTINEL_KEY


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    return Settings(
        auth_key=Secret(SENTINEL_KEY),
        host=TEST_HOST,
        db_path=tmp_path / "data" / "shelog.db",
        devices_file=tmp_path / "devices.toml",
        poll_interval_s=60.0,
        log_level="INFO",
    )


@pytest.fixture
def test_devices() -> tuple[Device, ...]:
    return (
        Device(id="020000000001", name="T-HT1", group="subfloor", model="ht_gen1"),
        Device(id="020000000002", name="T-HT3", group="reference", model="ht_gen3"),
        Device(id="020000000003", name="T-FLOOD", group="subfloor", model="flood_gen1"),
    )


@pytest.fixture
def tmp_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    con = connect_rw(tmp_path / "data" / "shelog.db")
    migrate(con)
    yield con
    con.close()


@pytest.fixture
def envelope() -> Callable[[str], Any]:
    def load(name: str) -> Any:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return load


@pytest.fixture(autouse=True)
def _no_real_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read real NTFY_* / ping settings, so nothing is ever delivered."""
    from shelog import alerts

    monkeypatch.setattr(
        alerts, "load_alert_settings", lambda *a, **k: alerts.AlertSettings(None, None, None)
    )
