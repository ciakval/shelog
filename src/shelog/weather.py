"""Hourly outdoor weather + soil from Open-Meteo into ``outdoor`` (upsert, model revisions win).

Runs from poller housekeeping. Coordinates come only from SHELOG_LAT / SHELOG_LON (rounded
to 2 decimals) and are never logged or stored. Failures get a 'weather' poll_log row with a
closed category and their own backoff; they never touch the Shelly schedule.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shelog.config import read_dotenv

log = logging.getLogger(__name__)

URL = "https://api.open-meteo.com/v1/forecast"
VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "precipitation",
    "surface_pressure",
    "shortwave_radiation",
    "wind_speed_10m",
    "cloud_cover",
    "soil_temperature_0cm",
    "soil_temperature_6cm",
    "soil_moisture_0_to_1cm",
    "soil_moisture_1_to_3cm",
    "soil_moisture_3_to_9cm",
)
REFRESH_S = 3600
RETRY_S = 900
MAX_PAST_DAYS = 92
ROUTINE_PAST_DAYS = 2
TIMEOUT_S = 15.0

Fetch = Callable[[str], tuple[int, bytes]]


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float


def load_location(
    environ: Mapping[str, str] | None = None, dotenv_path: Path = Path(".env")
) -> Location | None:
    env = dict(read_dotenv(dotenv_path))
    env.update(os.environ if environ is None else environ)
    raw_lat, raw_lon = env.get("SHELOG_LAT", "").strip(), env.get("SHELOG_LON", "").strip()
    if not raw_lat or not raw_lon:
        return None
    try:
        lat, lon = float(raw_lat), float(raw_lon)
    except ValueError:
        log.error("weather disabled: SHELOG_LAT/SHELOG_LON are not numbers")
        return None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        log.error("weather disabled: SHELOG_LAT/SHELOG_LON out of range")
        return None
    return Location(round(lat, 2), round(lon, 2))


def http_get(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "shelog"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as err:
        return int(err.code), b""


def build_url(loc: Location, past_days: int) -> str:
    query = {
        "latitude": f"{loc.lat:.2f}",
        "longitude": f"{loc.lon:.2f}",
        "hourly": ",".join(VARIABLES),
        "past_days": str(past_days),
        "forecast_days": "1",
        "timeformat": "unixtime",
        "timezone": "GMT",
    }
    return f"{URL}?{urllib.parse.urlencode(query)}"


def parse(body: bytes, now: int) -> list[tuple[int, str, float | None]]:
    doc: Any = json.loads(body)
    hourly = doc["hourly"]
    times = hourly["time"]
    rows: list[tuple[int, str, float | None]] = []
    for var in VARIABLES:
        values = hourly.get(var)
        if not isinstance(values, list) or len(values) != len(times):
            continue
        for ts, value in zip(times, values, strict=True):
            if not isinstance(ts, int) or ts > now:
                continue  # no future (forecast) rows
            ok = isinstance(value, int | float) and not isinstance(value, bool)
            rows.append((ts, var, float(value) if ok else None))
    return rows


class Weather:
    def __init__(
        self, con: sqlite3.Connection, loc: Location | None, fetch: Fetch = http_get
    ) -> None:
        self._con = con
        self._loc = loc
        self._fetch = fetch

    def _past_days(self, now: int) -> int:
        have = self._con.execute("SELECT MIN(ts) FROM outdoor").fetchone()[0]
        first = self._con.execute("SELECT MIN(ts) FROM reading").fetchone()[0]
        if first is None or (have is not None and have <= first):
            return ROUTINE_PAST_DAYS
        return max(ROUTINE_PAST_DAYS, min(MAX_PAST_DAYS, (now - first) // 86400 + 2))

    def due(self, now: int) -> bool:
        row = self._con.execute(
            "SELECT ts, ok FROM poll_log WHERE kind='weather' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return True
        return now - row[0] >= (REFRESH_S if row[1] else RETRY_S)

    def run(self, now: int | None = None) -> None:
        if self._loc is None:
            return
        now = int(time.time()) if now is None else now
        if not self.due(now):
            return
        started = time.monotonic()
        status: int | None = None
        category: str | None = None
        rows: list[tuple[int, str, float | None]] = []
        try:
            status, body = self._fetch(build_url(self._loc, self._past_days(now)))
            if status == 429:
                category = "rate_limited"
            elif status != 200:
                category = "http_other"
            else:
                rows = parse(body, now)
                if not rows:
                    category = "bad_response"
        except TimeoutError:
            category = "timeout"
        except (OSError, urllib.error.URLError):
            category = "network"
        except (ValueError, KeyError, TypeError):
            category = "bad_response"
        latency = int((time.monotonic() - started) * 1000)
        self._con.execute("BEGIN IMMEDIATE")
        try:
            self._con.executemany(
                "INSERT INTO outdoor(ts, variable, value, interval_s, fetched_at)"
                " VALUES (?, ?, ?, 3600, ?) ON CONFLICT(ts, variable) DO UPDATE SET"
                " value = excluded.value, fetched_at = excluded.fetched_at",
                [(ts, var, value, now) for ts, var, value in rows],
            )
            self._con.execute(
                "INSERT INTO poll_log(ts, kind, ok, http_status, error, n_new, latency_ms)"
                " VALUES (?, 'weather', ?, ?, ?, ?, ?)",
                (now, 0 if category else 1, status, category, len(rows), latency),
            )
            self._con.execute("COMMIT")
        except BaseException:
            self._con.execute("ROLLBACK")
            raise
        if category:
            log.warning("weather fetch failed: %s", category)
        else:
            log.info("weather ok: %d values", len(rows))
