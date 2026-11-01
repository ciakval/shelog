"""Derived metrics, step semantics, breaks, downsampling, stats and API privacy."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from shelog import api, series
from shelog.metrics import abs_humidity, dew_point, emc
from shelog.web import WebSettings, create_app

T0 = 1_800_000_000


@pytest.mark.parametrize(
    ("t", "rh", "want"), [(21, 50, 9.25), (0, 50, 9.52), (40, 60, 10.20), (21, 65, 11.97)]
)
def test_emc_reference_values(t, rh, want):
    assert emc(t, rh) == pytest.approx(want, abs=0.01)


def test_dew_point_and_abs_humidity():
    assert dew_point(20, 50) == pytest.approx(9.26, abs=0.01)
    assert dew_point(20, 100) == pytest.approx(20.0, abs=0.01)
    assert abs_humidity(20, 50) == pytest.approx(8.62, abs=0.01)
    assert dew_point(20, 0) is None
    assert 25 < emc(20, 100) < 35  # finite at saturation (K*h < 1)
    assert emc(20, 101) is None


def _add(con, dev, ts, t, rh=None, battery=90.0):
    con.execute(
        "INSERT INTO reading(device_id, ts, ts_source, t_c, rh, battery, polled_at)"
        " VALUES (?, ?, 'cloud', ?, ?, ?, ?)",
        (dev, ts, t, rh, battery, ts),
    )


def _polls(con, start, end, every=60):
    con.executemany(
        "INSERT INTO poll_log(ts, kind, ok) VALUES (?, 'shelly', 1)",
        [(ts,) for ts in range(start, end + 1, every)],
    )


def test_calibration_applies_at_read_time(tmp_db, test_devices):
    dev = test_devices[0]
    _add(tmp_db, dev.id, T0, 20.0, 50.0)
    plain = series.load_reports(tmp_db, dev, T0, T0 + 10)[0].values
    shifted = series.load_reports(tmp_db, replace(dev, t_offset=-1.0, rh_offset=5.0), T0, T0 + 10)[
        0
    ].values
    assert (plain["t"], plain["rh"]) == (20.0, 50.0)
    assert (shifted["t"], shifted["rh"]) == (19.0, 55.0)
    assert shifted["emc"] == pytest.approx(emc(19.0, 55.0), abs=0.01)


def test_flood_has_no_rh_metrics(tmp_db, test_devices):
    flood = test_devices[2]
    _add(tmp_db, flood.id, T0, 15.0, 40.0)
    values = series.load_reports(tmp_db, flood, T0, T0 + 1)[0].values
    assert values["rh"] is None and values["emc"] is None and values["t"] == 15.0


def test_forward_fill_starts_from_held_value_and_breaks(tmp_db, test_devices):
    dev = replace(test_devices[0], max_silence_s=3600)
    _polls(tmp_db, T0 - 7200, T0 + 4 * 3600)
    _add(tmp_db, dev.id, T0 - 1800, 20.0, 50.0)  # held into the range
    _add(tmp_db, dev.id, T0 + 600, 21.0, 50.0)  # then silent > 1 h
    body = api.readings_body(
        tmp_db, (dev,), {"from": str(T0), "to": str(T0 + 3 * 3600), "fields": "t"}, T0 + 99999
    )
    x, col = body["x"], body["fields"]["t"][0]
    assert x[0] == T0 and col[0] == 20.0  # value held from before the range
    assert col[x.index(T0 + 600)] == 21.0
    brk = x.index(T0 + 600 + 3600 + 1)
    assert col[brk] is None  # explicit break once silent past the threshold
    # Resampled grid: forward fill, never interpolated.
    body = api.readings_body(
        tmp_db,
        (dev,),
        {"from": str(T0), "to": str(T0 + 1800), "fields": "t", "resample": "5m"},
        T0 + 99999,
    )
    assert body["fields"]["t"][0] == [20.0, 20.0, 21.0, 21.0, 21.0, 21.0, 21.0]


def test_poller_outage_is_a_break(tmp_db, test_devices):
    dev = test_devices[0]
    _polls(tmp_db, T0, T0 + 600)
    _polls(tmp_db, T0 + 7200, T0 + 9000)
    _add(tmp_db, dev.id, T0, 20.0, 50.0)
    body = api.readings_body(
        tmp_db, (dev,), {"from": str(T0), "to": str(T0 + 9000), "fields": "t"}, T0 + 99999
    )
    x, col = body["x"], body["fields"]["t"][0]
    assert body["outages"] == [[T0 + 600 + 300, T0 + 7200]]
    assert col[x.index(T0 + 900)] is None
    assert col[x.index(T0 + 7200)] == 20.0


def test_downsample_keeps_spikes_and_breaks():
    xs = list(range(0, 30_000))
    col = [1.0] * len(xs)
    col[12_345] = 99.0
    col[20_000] = None
    out_x, (out,) = series.downsample(xs, [col], max_points=300)
    assert len(out_x) <= 300
    assert out_x == sorted(set(out_x))
    assert 99.0 in out and None in out


def test_time_weighted_stats_with_uneven_intervals(tmp_db, test_devices):
    dev = test_devices[0]
    _polls(tmp_db, T0 - 60, T0 + 4000)
    _add(tmp_db, dev.id, T0, 10.0, 30.0)  # held 1000 s
    _add(tmp_db, dev.id, T0 + 1000, 20.0, 50.0)  # held 3000 s
    body = api.stats_body(tmp_db, (dev,), {"from": str(T0), "to": str(T0 + 4000)}, T0 + 4000)
    row = body["devices"][0]
    assert row["t"] == {"min": 10.0, "max": 20.0, "mean": 17.5}
    assert row["rh"]["mean"] == 45.0
    assert row["rh_outside_band_pct"] == 25.0
    want_emc = (emc(10, 30) * 1000 + emc(20, 50) * 3000) / 4000
    assert math.isclose(row["emc"]["mean"], want_emc, abs_tol=0.01)
    assert row["coverage_pct"] == 100.0


def test_bad_params_are_400_and_no_ids_anywhere(tmp_path, tmp_db, test_devices):
    _polls(tmp_db, T0 - 600, T0)
    for d in test_devices:
        _add(tmp_db, d.id, T0 - 300, 20.0, 50.0)
    tmp_db.execute(
        "INSERT INTO outdoor(ts, variable, value, fetched_at)"
        " VALUES (?, 'temperature_2m', 5.0, ?), (?, 'relative_humidity_2m', 80.0, ?)",
        (T0 - 3600, T0, T0 - 3600, T0),
    )
    settings = WebSettings(tmp_path / "data" / "shelog.db", tmp_path / "x.toml", "INFO")
    client = TestClient(create_app(settings, test_devices, now=lambda: T0))
    blobs = []
    for url in (
        "/api/devices",
        "/api/readings?from=all",
        "/api/readings?devices=subfloor&resample=15m",
        "/api/stats",
        "/api/outdoor?vars=temperature_2m,abs_humidity",
        "/api/export.csv",
    ):
        resp = client.get(url)
        assert resp.status_code == 200, url
        blobs.append(resp.text)
    for d in test_devices:
        assert all(d.id not in b for b in blobs)
    out = client.get("/api/outdoor?vars=abs_humidity").json()
    assert out["vars"]["abs_humidity"] == [pytest.approx(abs_humidity(5.0, 80.0), abs=0.01)]
    for bad in (
        "/api/readings?from=x",
        "/api/readings?resample=2m",
        "/api/readings?devices=nope",
        "/api/readings?fields=secret",
        f"/api/readings?from={T0 - 500 * 86400}",
        "/api/stats?band_lo=70&band_hi=60",
        "/api/outdoor?vars=latitude",
    ):
        assert client.get(bad).status_code == 400, bad
