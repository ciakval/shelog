"""`shelog probe`: live API checks with a raw dump on disk and a name-only summary.

Raw response bodies contain MACs, SSIDs, IPs and real device ids, so they go only to the
gitignored data/probe/ directory. The index file and the console summary carry device
NAMES only: no ids, host, key or URL.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from shelog.clock import Clock, SystemClock
from shelog.config import Device, Settings
from shelog.shelly.client import Outcome, RateGuard, ShellyClient, classify
from shelog.shelly.transport import RawResponse, TransportError

ANCHOR_KEYS = {"ht_gen1": "tmp", "ht_gen3": "temperature:0", "flood_gen1": "flood"}


@dataclass(frozen=True)
class StepResult:
    raw: RawResponse | None
    outcome: Outcome


@dataclass(frozen=True)
class ProbeStep:
    name: str
    run: Callable[[ShellyClient, Clock], StepResult]


def _latency_ms(clock: Clock, started: float) -> int:
    return round((clock.monotonic() - started) * 1000)


def _step_v1_all_status(client: ShellyClient, clock: Clock) -> StepResult:
    started = clock.monotonic()
    try:
        raw = client.request("/device/all_status", form={"auth_key": client.key.reveal()})
    except TransportError as err:
        return StepResult(None, Outcome(err.category, None, latency_ms=_latency_ms(clock, started)))
    outcome = classify(raw, clock.time())
    return StepResult(raw, replace(outcome, latency_ms=_latency_ms(clock, started)))


STEPS: tuple[ProbeStep, ...] = (ProbeStep("v1_all_status", _step_v1_all_status),)


def _age_minutes(updated: object, now: float) -> str:
    """Age of an `_updated` UTC string in whole minutes, or 'unparseable'."""
    if not isinstance(updated, str):
        return "unparseable"
    try:
        when = datetime.fromisoformat(updated + "+00:00")
    except ValueError:
        return "unparseable"
    return str(round((now - when.timestamp()) / 60))


def _summarize(outcome: Outcome, devices: tuple[Device, ...], now: float, stdout: TextIO) -> bool:
    """Print the name-only summary; return True when every configured device is present."""
    print(f"outcome: {outcome.category}", file=stdout)
    status = outcome.devices if outcome.devices is not None else {}
    known = {k.lower(): v for k, v in status.items()}
    all_present = True
    for dev in devices:
        entry = known.get(dev.id)
        if not isinstance(entry, dict):
            all_present = False
            print(f"{dev.name}: model {dev.model}, present: no", file=stdout)
            continue
        updated = entry.get("_updated")
        age = _age_minutes(updated, now)
        anchor = "yes" if ANCHOR_KEYS[dev.model] in entry else "no"
        age_text = age if age == "unparseable" else f"{age} min"
        print(
            f"{dev.name}: model {dev.model}, present: yes, last report: {updated} UTC, "
            f"age: {age_text}, anchor key: {anchor}",
            file=stdout,
        )
    configured = {d.id for d in devices}
    extra = sum(1 for key in known if key not in configured)
    print(f"unconfigured devices in response: {extra}", file=stdout)
    return all_present


def run_probe(
    settings: Settings,
    devices: tuple[Device, ...],
    client: ShellyClient,
    clock: Clock,
    *,
    out_root: Path = Path("data/probe"),
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    stamp = datetime.fromtimestamp(clock.time(), UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = out_root / stamp
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    index: list[dict[str, object]] = []
    baseline: Outcome | None = None
    for step in STEPS:
        result = step.run(client, clock)
        body = result.raw.body if result.raw is not None else b""
        if result.raw is not None:
            (out_dir / f"{step.name}.json").write_bytes(body)
        index.append(
            {
                "step": step.name,
                "http_status": result.outcome.http_status,
                "category": result.outcome.category,
                "retry_after_s": result.outcome.retry_after_s,
                "latency_ms": result.outcome.latency_ms,
                "bytes": len(body),
            }
        )
        if baseline is None:
            baseline = result.outcome
    (out_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    all_present = _summarize(baseline, devices, clock.time(), out)
    return 0 if baseline.ok and all_present else 1


def main(settings: Settings, devices: tuple[Device, ...]) -> int:
    clock = SystemClock()
    client = ShellyClient(settings.host, settings.auth_key, guard=RateGuard(clock), clock=clock)
    return run_probe(settings, devices, client, clock)
