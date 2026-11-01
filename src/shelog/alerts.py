"""ntfy alerts on transitions (raised / cleared) plus an optional healthchecks.io ping.

State lives in ``alert_state`` so restarts and deploys never repeat an alert. A failed
delivery is retried on the next housekeeping run and never raises into the poll loop.
Messages and logs carry device NAMES and alert kinds only: never ids, URLs or the topic.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from shelog.config import Device, Secret, read_dotenv

log = logging.getLogger(__name__)

POLL_FAILING_AFTER_S = 3600
DEFAULT_MAX_SILENCE_S = 54_000
BATTERY_LOW_PCT = 20.0
PING_FRESH_S = 600  # only ping the dead-man's switch if a poll succeeded this recently
HTTP_TIMEOUT_S = 10.0

Sender = Callable[[str, bytes, dict[str, str]], int]


@dataclass(frozen=True)
class AlertSettings:
    ntfy_url: Secret | None  # server/topic: the topic is a password
    ntfy_token: Secret | None
    ping_url: Secret | None


def load_alert_settings(
    environ: Mapping[str, str] | None = None, dotenv_path: Path = Path(".env")
) -> AlertSettings:
    env = dict(read_dotenv(dotenv_path))
    env.update(os.environ if environ is None else environ)
    topic = env.get("NTFY_TOPIC", "").strip()
    server = (env.get("NTFY_SERVER", "").strip() or "https://ntfy.sh").rstrip("/")
    token = env.get("NTFY_TOKEN", "").strip()
    ping = env.get("HEALTHCHECK_PING_URL", "").strip()
    usable_topic = topic and topic != "CHANGE_ME"
    return AlertSettings(
        ntfy_url=Secret(f"{server}/{topic}") if usable_topic else None,
        ntfy_token=Secret(token) if token else None,
        ping_url=Secret(ping) if ping.startswith("https://") else None,
    )


def http_post(url: str, body: bytes, headers: dict[str, str]) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
        return int(resp.status)


@dataclass(frozen=True)
class Alert:
    key: str
    title: str
    message: str
    priority: str = "default"


def active_alerts(con: sqlite3.Connection, devices: tuple[Device, ...], now: int) -> list[Alert]:
    alerts: list[Alert] = []
    auth = con.execute("SELECT value FROM meta WHERE key='shelly_auth'").fetchone()
    if auth and auth[0] == "rejected":
        alerts.append(
            Alert(
                "auth_rejected",
                "shelog: Shelly key rejected",
                "Shelly Cloud rejects the auth key (password change or host move?)."
                " Update SHELLY_AUTH_KEY / SHELLY_HOST in the server .env.",
                "high",
            )
        )
    first_poll, last_ok = con.execute(
        "SELECT MIN(ts), MAX(CASE WHEN ok=1 THEN ts END) FROM poll_log WHERE kind='shelly'"
    ).fetchone()
    reference = last_ok if last_ok is not None else first_poll
    if reference is not None and now - reference > POLL_FAILING_AFTER_S:
        alerts.append(
            Alert(
                "poll_failing",
                "shelog: polling failing",
                "No successful Shelly poll for over an hour.",
                "high",
            )
        )
    latest = {
        dev: (ts, battery)
        for dev, ts, battery in con.execute(
            "SELECT r.device_id, r.ts, r.battery FROM reading r JOIN"
            " (SELECT device_id, MAX(ts) ts FROM reading GROUP BY device_id) m"
            " ON r.device_id = m.device_id AND r.ts = m.ts"
        )
    }
    for dev in devices:
        ts, battery = latest.get(dev.id, (None, None))
        limit = dev.max_silence_s or DEFAULT_MAX_SILENCE_S
        since = ts if ts is not None else first_poll
        if since is not None and now - since > limit:
            alerts.append(
                Alert(
                    f"device_silent:{dev.name}",
                    f"shelog: {dev.name} silent",
                    f"{dev.name} has not reported for over {limit // 3600} h.",
                )
            )
        if battery is not None and battery < BATTERY_LOW_PCT:
            alerts.append(
                Alert(
                    f"battery_low:{dev.name}",
                    f"shelog: {dev.name} battery low",
                    f"{dev.name} battery is at {battery:.0f} %.",
                )
            )
    return alerts


class Alerter:
    def __init__(
        self,
        con: sqlite3.Connection,
        devices: tuple[Device, ...],
        settings: AlertSettings,
        send: Sender = http_post,
    ) -> None:
        self._con = con
        self._devices = devices
        self._settings = settings
        self._send = send

    def _notify(self, title: str, message: str, priority: str, tags: str) -> bool:
        if self._settings.ntfy_url is None:
            return False
        headers = {"Title": title, "Priority": priority, "Tags": tags}
        if self._settings.ntfy_token is not None:
            headers["Authorization"] = f"Bearer {self._settings.ntfy_token.reveal()}"
        try:
            status = self._send(self._settings.ntfy_url.reveal(), message.encode(), headers)
        except Exception as err:
            log.warning("ntfy delivery failed: %s", type(err).__name__)
            return False
        if not 200 <= status < 300:
            log.warning("ntfy delivery failed: http %d", status)
            return False
        return True

    def run(self, now: int) -> None:
        active = {a.key: a for a in active_alerts(self._con, self._devices, now)}
        stored = {
            key: notified
            for key, notified in self._con.execute("SELECT key, notified_at FROM alert_state")
        }
        for key, alert in active.items():
            if key not in stored:
                self._con.execute(
                    "INSERT INTO alert_state(key, since, notified_at) VALUES (?, ?, NULL)",
                    (key, now),
                )
                log.warning("alert raised: %s", key)
            if stored.get(key) is None and self._notify(
                alert.title, alert.message, alert.priority, "warning"
            ):
                self._con.execute("UPDATE alert_state SET notified_at=? WHERE key=?", (now, key))
        for key, notified in stored.items():
            if key in active:
                continue
            title, recovery = _recovery_text(key)
            # Never notified about it: clear silently. Otherwise keep until the recovery lands.
            if notified is None or self._notify(title, recovery, "default", "white_check_mark"):
                self._con.execute("DELETE FROM alert_state WHERE key=?", (key,))
                log.info("alert cleared: %s", key)

    def ping(self, now: int) -> None:
        if self._settings.ping_url is None:
            return
        row = self._con.execute(
            "SELECT MAX(ts) FROM poll_log WHERE kind='shelly' AND ok=1"
        ).fetchone()
        if row[0] is None or now - row[0] > PING_FRESH_S:
            return  # stale: stay silent so the dead-man's switch fires
        try:
            self._send(self._settings.ping_url.reveal(), b"", {})
        except Exception as err:
            log.warning("healthcheck ping failed: %s", type(err).__name__)


def _recovery_text(key: str) -> tuple[str, str]:
    kind, _, name = key.partition(":")
    texts = {
        "auth_rejected": "Shelly Cloud accepts the auth key again.",
        "poll_failing": "Shelly polling works again.",
        "device_silent": f"{name} is reporting again.",
        "battery_low": f"{name} battery is fine again.",
    }
    return f"shelog: resolved {kind.replace('_', ' ')}", texts.get(kind, "Resolved.")


def drill(settings: AlertSettings, send: Sender = http_post) -> bool:
    """Send one test notification (no DB involved)."""
    alerter = Alerter(None, (), settings, send)  # type: ignore[arg-type]
    return alerter._notify(
        "shelog: alert drill",
        "Test notification from shelog. No action needed.",
        "default",
        "test_tube",
    )
