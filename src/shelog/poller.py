"""One poll cycle: fetch all_status once, parse, decide, write; plus the run loop.

Logs carry device NAMES, categories and counts only: never ids, host, URL or key.
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import threading
from dataclasses import dataclass, replace

from shelog import store
from shelog.clock import Clock, SystemClock
from shelog.config import Device, Settings
from shelog.db import connect_rw, migrate
from shelog.shelly import parse
from shelog.shelly.client import NO_DEVICES, Interrupted, Outcome, RateGuard, ShellyClient
from shelog.shelly.parse import ParseReject, Report

log = logging.getLogger(__name__)


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
                log.warning("device %s: parse error (%s)", dev.name, type(err).__name__)
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
        log.info(
            "poll ok: new=%d unchanged=%d rejected=%d latency_ms=%s",
            record.n_new,
            record.n_unchanged,
            record.n_rejected,
            record.latency_ms,
        )
        return CycleResult(outcome, record)


def main(settings: Settings, devices: tuple[Device, ...], *, once: bool) -> int:
    con = connect_rw(settings.db_path)
    migrate(con)
    clock = SystemClock()
    client = ShellyClient(settings.host, settings.auth_key, guard=RateGuard(clock), clock=clock)
    poller = Poller(settings, devices, con, client, clock)

    stop = threading.Event()

    def _request_stop(signum: int, frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    try:
        if once:
            try:
                result = poller.step(stop)
            except Interrupted:
                return 1
            return 0 if result.outcome.ok else 1
        while not stop.is_set():
            try:
                poller.step(stop)
            except Interrupted:
                break
            clock.wait(stop, settings.poll_interval_s)
        return 0
    finally:
        con.close()
