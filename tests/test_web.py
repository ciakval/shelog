"""Web service: coarse health, read-only DB access, no ids or key in responses."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from starlette.testclient import TestClient

from shelog.web import WebSettings, create_app, health, open_ro

NOW = 1_793_600_000


def _seed(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO poll_log(ts, kind, ok, n_devices, n_new) VALUES (?, 'shelly', 1, 3, 1)",
        (NOW - 60,),
    )
    con.execute("INSERT INTO meta(key, value) VALUES ('shelly_auth', 'ok')")
    rows = [
        ("020000000001", NOW - 600, 21.5, 55.0, 80.0, None),
        ("020000000002", NOW - 600, 20.0, 50.0, 10.0, None),  # battery low
        # 020000000003 never reported -> silent
    ]
    for dev, ts, t, rh, bat, flood in rows:
        con.execute(
            "INSERT INTO reading(device_id, ts, ts_source, t_c, rh, battery, flood, polled_at)"
            " VALUES (?, ?, 'cloud', ?, ?, ?, ?, ?)",
            (dev, ts, t, rh, bat, flood, ts),
        )


def test_health_without_db(test_devices):
    body = health(None, test_devices, NOW)
    assert body["status"] == "starting"


def test_health_reports_silent_and_battery(tmp_db, test_devices):
    _seed(tmp_db)
    body = health(tmp_db, test_devices, NOW)
    assert body["status"] == "degraded"
    assert body["auth"] == "ok"
    by_name = {d["name"]: d for d in body["devices"]}
    assert by_name["T-HT1"] == {
        "name": "T-HT1",
        "group": "subfloor",
        "last_report": NOW - 600,
        "silent": False,
        "battery_low": False,
    }
    assert by_name["T-HT3"]["battery_low"] is True
    assert by_name["T-FLOOD"]["silent"] is True


def test_health_down_when_stale_or_rejected(tmp_db, test_devices):
    _seed(tmp_db)
    assert health(tmp_db, test_devices, NOW + 3600)["status"] == "down"
    tmp_db.execute("UPDATE meta SET value='rejected' WHERE key='shelly_auth'")
    assert health(tmp_db, test_devices, NOW)["status"] == "down"


def test_open_ro_is_query_only_and_never_creates(tmp_path: Path, tmp_db):
    assert open_ro(tmp_path / "missing.db") is None
    assert not (tmp_path / "missing.db").exists()
    con = open_ro(tmp_path / "data" / "shelog.db")
    assert con is not None
    try:
        con.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")
    except sqlite3.OperationalError:
        pass
    else:  # pragma: no cover
        raise AssertionError("write succeeded on a query_only connection")
    finally:
        con.close()


def test_api_health_has_no_ids(tmp_path: Path, tmp_db, test_devices):
    _seed(tmp_db)
    settings = WebSettings(tmp_path / "data" / "shelog.db", tmp_path / "devices.toml", "INFO")
    client = TestClient(create_app(settings, test_devices, now=lambda: NOW))
    assert client.get("/healthz").text == "ok"
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=30"
    for dev in test_devices:
        assert dev.id not in resp.text


def test_serve_does_not_load_shelly_settings(monkeypatch):
    """`shelog serve` must run with no SHELLY_* variables at all."""
    import shelog.cli as cli

    called = {}

    def fake_main(host, port):
        called["ok"] = (host, port)
        return 0

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("serve loaded the Shelly settings")

    monkeypatch.setattr("shelog.web.main", fake_main)
    monkeypatch.setattr(cli, "load_settings", boom)
    assert cli.main(["serve", "--port", "8123"]) == 0
    assert called["ok"] == ("0.0.0.0", 8123)  # noqa: S104
