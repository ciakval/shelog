"""Alert transitions: raise once, retry failed delivery, recover, survive restarts."""

from __future__ import annotations

import pytest

from shelog import alerts
from shelog.alerts import Alerter, AlertSettings, active_alerts
from shelog.config import Secret

NOW = 1_800_000_000
REAL_LOAD = alerts.load_alert_settings  # captured before the autouse stub replaces it
TOPIC_URL = "https://ntfy.example/topic-SECRET123"


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, str]]] = []
        self.fail = False

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        if self.fail:
            raise OSError("down")
        self.sent.append((url, body.decode(), headers))
        return 200


def _settings(ping: bool = False) -> AlertSettings:
    return AlertSettings(
        Secret(TOPIC_URL), None, Secret("https://hc.example/ping/x") if ping else None
    )


def _poll(con, ts: int, ok: int = 1) -> None:
    con.execute("INSERT INTO poll_log(ts, kind, ok) VALUES (?, 'shelly', ?)", (ts, ok))


def _reading(con, dev: str, ts: int, battery: float = 90.0) -> None:
    con.execute(
        "INSERT INTO reading(device_id, ts, ts_source, t_c, battery, polled_at)"
        " VALUES (?, ?, 'cloud', 20.0, ?, ?)",
        (dev, ts, battery, ts),
    )


def _healthy(con, devices) -> None:
    _poll(con, NOW - 60)
    for d in devices:
        _reading(con, d.id, NOW - 60)


def test_healthy_raises_nothing(tmp_db, test_devices):
    _healthy(tmp_db, test_devices)
    assert active_alerts(tmp_db, test_devices, NOW) == []


def test_auth_rejected_raises_once_then_recovers(tmp_db, test_devices):
    _healthy(tmp_db, test_devices)
    out = Outbox()
    alerter = Alerter(tmp_db, test_devices, _settings(), out)
    tmp_db.execute("INSERT INTO meta(key, value) VALUES ('shelly_auth', 'rejected')")
    alerter.run(NOW)
    alerter.run(NOW + 300)
    # A restart builds a new Alerter on the same DB: still no repeat.
    Alerter(tmp_db, test_devices, _settings(), out).run(NOW + 600)
    assert [s[2]["Title"] for s in out.sent] == ["shelog: Shelly key rejected"]
    assert out.sent[0][2]["Priority"] == "high"
    tmp_db.execute("UPDATE meta SET value='ok' WHERE key='shelly_auth'")
    alerter.run(NOW + 900)
    assert out.sent[-1][2]["Title"] == "shelog: resolved auth rejected"
    assert tmp_db.execute("SELECT COUNT(*) FROM alert_state").fetchone()[0] == 0


def test_failed_delivery_is_retried(tmp_db, test_devices):
    _healthy(tmp_db, test_devices)
    out = Outbox()
    out.fail = True
    alerter = Alerter(tmp_db, test_devices, _settings(), out)
    tmp_db.execute("INSERT INTO meta(key, value) VALUES ('shelly_auth', 'rejected')")
    alerter.run(NOW)  # must not raise
    out.fail = False
    alerter.run(NOW + 300)
    assert len(out.sent) == 1


def test_poll_failing_silent_and_battery(tmp_db, test_devices):
    _poll(tmp_db, NOW - 7200)
    _poll(tmp_db, NOW - 60, ok=0)
    _reading(tmp_db, test_devices[0].id, NOW - 60_000)  # past the 54000 s default
    _reading(tmp_db, test_devices[1].id, NOW - 60, battery=12.0)
    keys = {a.key for a in active_alerts(tmp_db, test_devices, NOW)}
    # Device 3 never reported, but the poller has only run for 2 h (< its 15 h limit).
    assert keys == {
        "poll_failing",
        f"device_silent:{test_devices[0].name}",
        f"battery_low:{test_devices[1].name}",
    }


def test_messages_never_contain_ids(tmp_db, test_devices):
    _poll(tmp_db, NOW - 100_000)
    out = Outbox()
    Alerter(tmp_db, test_devices, _settings(), out).run(NOW)
    assert out.sent
    blob = repr(out.sent).replace(TOPIC_URL, "")
    for d in test_devices:
        assert d.id not in blob


def test_ping_only_when_fresh(tmp_db, test_devices):
    out = Outbox()
    alerter = Alerter(tmp_db, test_devices, _settings(ping=True), out)
    _poll(tmp_db, NOW - 3600)
    alerter.ping(NOW)
    assert out.sent == []
    _poll(tmp_db, NOW - 30)
    alerter.ping(NOW)
    assert [s[0] for s in out.sent] == ["https://hc.example/ping/x"]


@pytest.mark.parametrize("topic", ["", "CHANGE_ME"])
def test_placeholder_topic_disables_ntfy(topic, tmp_path):
    assert REAL_LOAD({"NTFY_TOPIC": topic}, tmp_path / "none").ntfy_url is None
    assert repr(REAL_LOAD({"NTFY_TOPIC": "abc"}, tmp_path / "none").ntfy_url) == "***"
