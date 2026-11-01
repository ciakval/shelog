"""Open-Meteo ingest: upsert, no future rows, failure categories, backfill window."""

from __future__ import annotations

import json

from shelog import weather
from shelog.weather import Location, Weather

NOW = 1_800_000_000
HOUR = 3600
LOC = Location(50.0, 14.0)
REAL_LOAD = weather.load_location


def _body(times, temp):
    hourly = {"time": times, **{v: [1.0] * len(times) for v in weather.VARIABLES}}
    hourly["temperature_2m"] = temp
    return json.dumps({"hourly": hourly}).encode()


def test_upsert_drops_future_and_keeps_nulls(tmp_db):
    times = [NOW - 2 * HOUR, NOW - HOUR, NOW + HOUR]
    urls = []

    def fetch(url):
        urls.append(url)
        return 200, _body(times, [5.0, None, 9.0])

    w = Weather(tmp_db, LOC, fetch)
    w.run(NOW)
    rows = tmp_db.execute(
        "SELECT ts, value FROM outdoor WHERE variable='temperature_2m' ORDER BY ts"
    ).fetchall()
    assert rows == [(NOW - 2 * HOUR, 5.0), (NOW - HOUR, None)]
    assert "past_days=2" in urls[0]
    # Not due again within the hour; a revision later overwrites.
    w.run(NOW + 60)
    assert len(urls) == 1
    w2 = Weather(tmp_db, LOC, lambda u: (200, _body(times, [6.0, 7.0, 9.0])))
    w2.run(NOW + HOUR)
    rows = tmp_db.execute(
        "SELECT value FROM outdoor WHERE variable='temperature_2m' ORDER BY ts"
    ).fetchall()
    assert rows == [(6.0,), (7.0,), (9.0,)]  # NOW + HOUR is no longer future


def test_failure_logged_with_category_and_retried_sooner(tmp_db):
    def boom(url):
        raise TimeoutError

    w = Weather(tmp_db, LOC, boom)
    w.run(NOW)
    assert tmp_db.execute("SELECT ok, error FROM poll_log WHERE kind='weather'").fetchall() == [
        (0, "timeout")
    ]
    assert not w.due(NOW + 600)
    assert w.due(NOW + weather.RETRY_S)
    Weather(tmp_db, LOC, lambda u: (429, b"")).run(NOW + weather.RETRY_S)
    assert tmp_db.execute(
        "SELECT error FROM poll_log WHERE kind='weather' ORDER BY id DESC"
    ).fetchone() == ("rate_limited",)


def test_backfill_reaches_first_reading(tmp_db):
    tmp_db.execute(
        "INSERT INTO reading(device_id, ts, ts_source, polled_at) VALUES ('a', ?, 'cloud', ?)",
        (NOW - 10 * 86400, NOW - 10 * 86400),
    )
    assert Weather(tmp_db, LOC)._past_days(NOW) == 12
    tmp_db.execute(
        "INSERT INTO reading(device_id, ts, ts_source, polled_at) VALUES ('a', ?, 'cloud', ?)",
        (NOW - 400 * 86400, NOW),
    )
    assert Weather(tmp_db, LOC)._past_days(NOW) == weather.MAX_PAST_DAYS


def test_disabled_without_location(tmp_db, tmp_path):
    Weather(tmp_db, None, lambda u: (_ for _ in ()).throw(AssertionError)).run(NOW)
    assert REAL_LOAD({}, tmp_path / "x") is None
    assert REAL_LOAD({"SHELOG_LAT": "50.0761", "SHELOG_LON": "14.4378"}, tmp_path / "x") == (
        Location(50.08, 14.44)
    )
    assert REAL_LOAD({"SHELOG_LAT": "x", "SHELOG_LON": "1"}, tmp_path / "x") is None


def test_network_error_category(tmp_db):
    import urllib.error

    def down(url):
        raise urllib.error.URLError("dns")

    Weather(tmp_db, LOC, down).run(NOW)
    assert tmp_db.execute("SELECT error FROM poll_log WHERE kind='weather'").fetchone() == (
        "network",
    )
