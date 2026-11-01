"""One poll cycle: fetch all_status once, parse, decide, write; plus the run loop.

Logs carry device NAMES, categories and counts only: never ids, host, URL or key.
"""

from __future__ import annotations

import logging
import random
import signal
import sqlite3
import sys
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from shelog import alerts, backup, store, weather
from shelog.clock import Clock, SystemClock
from shelog.config import Device, Settings
from shelog.db import SchemaTooNew, connect_rw, get_meta, migrate, set_meta
from shelog.lock import AlreadyRunning, acquire_instance_lock
from shelog.shelly import parse, transport
from shelog.shelly.client import (
    AUTH,
    BAD_RESPONSE,
    INTERNAL,
    NO_DEVICES,
    OK,
    RATE_LIMITED,
    Interrupted,
    Outcome,
    RateGuard,
    ShellyClient,
)
from shelog.shelly.parse import ParseReject, Report

log = logging.getLogger(__name__)

CAP_S = 300.0  # backoff ceiling
MIN_GAP_S = 1.1  # account limit is 1 req/s; never closer than this
AUTH_FLOOR_S = 300.0  # a rejected key is retried no faster than this
AUTH_CONFIRMATIONS = 2  # consecutive auth outcomes before declaring the key rejected
STARTUP_FAIL_SLEEP_S = 30.0  # avoid a hot restart loop under `restart: unless-stopped`
NEVER_STORED_CYCLES = 10  # ok cycles with a device present but nothing ever stored
HOUSEKEEPING_EVERY_S = 300.0  # backups/alerts run at most this often (monotonic)

AUTH_OK = "ok"
AUTH_REJECTED = "rejected"
META_AUTH = "shelly_auth"
META_AUTH_SINCE = "shelly_auth_since"


def next_delay(
    interval: float,
    consecutive_failures: int,
    category: str,
    retry_after: float | None,
    rng: random.Random,
) -> float:
    """Seconds to wait after a cycle: jittered interval on ok, capped equal-jitter backoff else."""
    if category == OK:
        delay = interval * rng.uniform(0.9, 1.1)
    else:
        exponent = min(max(consecutive_failures, 1) - 1, 30)
        base = min(CAP_S, interval * 2**exponent)
        delay = base / 2 + rng.uniform(0, base / 2)
        if category == RATE_LIMITED and retry_after is not None:
            delay = max(delay, retry_after)
        elif category == AUTH:
            delay = max(delay, AUTH_FLOOR_S)
    return max(delay, MIN_GAP_S)


def where(err: BaseException, depth: int = 4) -> str:
    """Exception type plus the innermost call sites (file:line function), no message text.

    Exception messages can embed payload keys such as device ids, so only the type and code
    locations are logged: enough to find a bug weeks into an unattended run.
    """
    frames = traceback.extract_tb(err.__traceback__)[-depth:]
    trail = " < ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames))
    return f"{type(err).__name__} at {trail}" if trail else type(err).__name__


@dataclass(frozen=True)
class CycleResult:
    outcome: Outcome
    record: store.PollRecord


class Poller:
    def __init__(
        self,
        settings: Settings,
        devices: tuple[Device, ...],
        con: sqlite3.Connection,
        client: ShellyClient,
        clock: Clock,
    ) -> None:
        self._settings = settings
        self._devices = devices
        self._con = con
        self._client = client
        self._clock = clock
        self._last_states = store.load_last_states(con)
        self._never_stored: dict[str, int] = {}
        self._auth_streak = 0
        self._auth_state = get_meta(con, META_AUTH)
        self._next_housekeeping = 0.0
        self.hooks: list[Callable[[], None]] = [self._daily_backup]

    def _daily_backup(self) -> None:
        backup.daily(self._con, self._settings.db_path.parent / "backup", self._clock.time())

    def housekeeping(self) -> None:
        """Backups and alerts. Rate-limited; a failing hook is logged and never raises."""
        now = self._clock.monotonic()
        if now < self._next_housekeeping:
            return
        self._next_housekeeping = now + HOUSEKEEPING_EVERY_S
        for hook in self.hooks:
            try:
                hook()
            except Exception as err:
                log.error("housekeeping failed: %s", where(err))

    @property
    def interval(self) -> float:
        return self._settings.poll_interval_s

    def cycle(self, stop: threading.Event) -> tuple[str, float | None]:
        """One attempt wrapped for the loop boundary: never raises except Interrupted.

        Returns ``(category, retry_after_s)``. A bug of ours is recorded as 'internal'.
        """
        try:
            result = self.step(stop)
        except Interrupted:
            raise
        except Exception as err:
            log.error("internal error in poll cycle: %s", where(err))
            self._record_internal()
            return INTERNAL, None
        self._observe(result.outcome)
        return result.outcome.category, result.outcome.retry_after_s

    def _record_internal(self) -> None:
        record = store.PollRecord(
            ts=int(self._clock.time()),
            ok=0,
            http_status=None,
            error=INTERNAL,
            n_devices=None,
            n_new=None,
            n_unchanged=None,
            n_rejected=None,
            retry_after_s=None,
            latency_ms=None,
        )
        try:
            store.write_cycle(self._con, record, [])
        except Exception as err:
            log.error("could not record internal error: %s", where(err))

    def _observe(self, outcome: Outcome) -> None:
        """Auth state machine: 'rejected' after consecutive auth outcomes, cleared by an ok."""
        try:
            if outcome.category == AUTH:
                self._auth_streak += 1
                if self._auth_streak >= AUTH_CONFIRMATIONS and self._auth_state != AUTH_REJECTED:
                    self._set_auth(AUTH_REJECTED)
                    log.error("Shelly Cloud rejected the auth key; retrying every 300 s")
                return
            self._auth_streak = 0
            if outcome.ok:
                if self._auth_state == AUTH_REJECTED:
                    log.info("Shelly Cloud accepted the auth key again")
                if self._auth_state != AUTH_OK:
                    self._set_auth(AUTH_OK)
        except Exception as err:
            log.error("could not persist auth state: %s", type(err).__name__)

    def _set_auth(self, state: str) -> None:
        set_meta(self._con, META_AUTH, state)
        set_meta(self._con, META_AUTH_SINCE, str(int(self._clock.time())))
        self._auth_state = state

    def _watch_never_stored(self, known: dict[str, object]) -> None:
        """Warn once if a configured device keeps showing up but nothing is ever stored."""
        for dev in self._devices:
            if dev.id not in known:
                continue
            if dev.id in self._last_states:
                self._never_stored.pop(dev.id, None)
                continue
            count = self._never_stored.get(dev.id, 0) + 1
            self._never_stored[dev.id] = count
            if count == NEVER_STORED_CYCLES:
                log.warning(
                    "device %s: present in %d consecutive ok cycles but no reading ever stored"
                    " (check the timestamp source)",
                    dev.name,
                    count,
                )

    def step(self, stop: threading.Event) -> CycleResult:
        now = int(self._clock.time())
        outcome = self._client.fetch_all_status(stop)
        if outcome.ok:
            known = {key.lower(): value for key, value in (outcome.devices or {}).items()}
            if not any(dev.id in known for dev in self._devices):
                outcome = replace(outcome, category=NO_DEVICES, devices=None)
        if not outcome.ok:
            record = store.PollRecord(
                ts=now,
                ok=0,
                http_status=outcome.http_status,
                error=outcome.category,
                n_devices=None,
                n_new=None,
                n_unchanged=None,
                n_rejected=None,
                retry_after_s=(
                    round(outcome.retry_after_s) if outcome.retry_after_s is not None else None
                ),
                latency_ms=outcome.latency_ms,
            )
            store.write_cycle(self._con, record, [])
            log.warning("poll failed: %s", outcome.category)
            if outcome.category == BAD_RESPONSE and outcome.body_shape is not None:
                log.warning("unexpected response body: %s", outcome.body_shape)
            return CycleResult(outcome, record)

        rows: list[store.ReadingRow] = []
        reports: dict[str, tuple[Device, Report]] = {}
        n_devices = n_unchanged = n_rejected = 0
        for dev in self._devices:
            if dev.id not in known:
                continue
            n_devices += 1
            try:
                parsed = parse.parse_device(dev.model, dev.id, known[dev.id])
            except Exception as err:
                n_rejected += 1
                log.warning("device %s: parse error (%s)", dev.name, where(err))
                continue
            if isinstance(parsed, ParseReject):
                n_rejected += 1
                log.warning("device %s: rejected (%s)", dev.name, parsed.reason)
                continue
            n_rejected += parsed.field_rejects
            decision = store.decide(self._last_states.get(dev.id), parsed, now)
            if decision.action == store.INSERT and decision.ts is not None:
                rows.append(store.ReadingRow.from_report(parsed, decision.ts))
                reports[dev.id] = (dev, parsed)
            elif decision.action == store.UNCHANGED:
                n_unchanged += 1
            else:
                n_rejected += 1
                log.warning("device %s: report %s", dev.name, decision.action)

        record = store.PollRecord(
            ts=now,
            ok=1,
            http_status=outcome.http_status,
            error=None,
            n_devices=n_devices,
            n_new=len(rows),
            n_unchanged=n_unchanged,
            n_rejected=n_rejected,
            retry_after_s=None,
            latency_ms=outcome.latency_ms,
        )
        n_new = store.write_cycle(self._con, record, rows)
        if n_new == len(rows):
            for row in rows:
                self._last_states[row.device_id] = store.LastState(
                    row.ts, row.t_c, row.rh, row.battery, row.flood
                )
            for dev, report in reports.values():
                for warning in report.warnings:
                    log.warning("device %s: %s", dev.name, warning)
        else:
            self._last_states = store.load_last_states(self._con)
        self._watch_never_stored(known)
        log.info(
            "poll ok: new=%d unchanged=%d rejected=%d latency_ms=%s",
            record.n_new,
            record.n_unchanged,
            record.n_rejected,
            record.latency_ms,
        )
        return CycleResult(outcome, record)


def run(poller: Poller, stop: threading.Event, clock: Clock, rng: random.Random) -> int:
    """Poll until ``stop`` is set. Deadline-based: ok cycles do not drift or burst."""
    failures = 0
    deadline = clock.monotonic()
    while not stop.is_set():
        remaining = deadline - clock.monotonic()
        if remaining > 0 and clock.wait(stop, remaining):
            break
        scheduled = deadline
        try:
            category, retry_after = poller.cycle(stop)
        except Interrupted:
            break
        failures = 0 if category == OK else failures + 1
        poller.housekeeping()
        delay = next_delay(poller.interval, failures, category, retry_after, rng)
        now = clock.monotonic()
        if category == OK:
            deadline = scheduled + delay
            if deadline < now:  # fell behind (long outage): resume, never catch up with a burst
                deadline = now + MIN_GAP_S
        else:
            deadline = now + delay
    return 0


def main(
    settings: Settings,
    devices: tuple[Device, ...],
    *,
    once: bool,
    clock: Clock | None = None,
    send: Callable[..., transport.RawResponse] | None = None,
    rng: random.Random | None = None,
) -> int:
    clock = clock if clock is not None else SystemClock()
    rng = rng if rng is not None else random.Random()  # noqa: S311  # jitter, not crypto
    stop = threading.Event()

    def _request_stop(signum: int, frame: object) -> None:
        stop.set()

    previous: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, _request_stop)
    lock_handle = None
    con: sqlite3.Connection | None = None
    try:
        try:
            lock_handle = acquire_instance_lock(settings.db_path.parent / "poller.lock")
        except AlreadyRunning:
            print("another shelog poll/probe is running on this data directory", file=sys.stderr)
            return 3
        except OSError as err:
            return _startup_failed(err, once, stop, clock)
        try:
            con = connect_rw(settings.db_path)
            migrate(con)
        except (OSError, sqlite3.Error, SchemaTooNew) as err:
            return _startup_failed(err, once, stop, clock)

        client = ShellyClient(
            settings.host,
            settings.auth_key,
            send=send if send is not None else transport.send,
            guard=RateGuard(clock, MIN_GAP_S),
            clock=clock,
        )
        try:
            poller = Poller(settings, devices, con, client, clock)
            alerter = alerts.Alerter(con, devices, alerts.load_alert_settings())
            poller.hooks.append(lambda: alerter.run(int(clock.time())))
            poller.hooks.append(lambda: alerter.ping(int(clock.time())))
            outdoor = weather.Weather(con, weather.load_location())
            poller.hooks.append(lambda: outdoor.run(int(clock.time())))
        except sqlite3.Error as err:  # reads last states + auth meta: corrupt/locked DB
            return _startup_failed(err, once, stop, clock)
        if once:
            try:
                category, _ = poller.cycle(stop)
            except Interrupted:
                return 1
            return 0 if category == OK else 1
        # RateGuard is per process: after a restart, wait one gap so the previous
        # process's last request and our first are never closer than 1.1 s.
        if clock.wait(stop, MIN_GAP_S):
            return 0
        return run(poller, stop, clock, rng)
    finally:
        if con is not None:
            con.close()
        if lock_handle is not None:
            lock_handle.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)  # type: ignore[arg-type]


def _startup_failed(err: Exception, once: bool, stop: threading.Event, clock: Clock) -> int:
    log.error("startup failed: %s", type(err).__name__)
    if not once:
        clock.wait(stop, STARTUP_FAIL_SLEEP_S)
    return 1
