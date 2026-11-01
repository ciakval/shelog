"""Pure per-model parsers for the v1 all_status device blocks.

The report time is always the cloud's `_updated` (UTC). Device clocks and `tmp.value`
(which follows the configured unit) are never used.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

TS_SOURCE_CLOUD = "cloud"
TS_SOURCE_POLL = "poll"

T_BOUNDS = (-50.0, 100.0)  # plausible range, deg C
RH_BOUNDS = (-5.0, 105.0)  # plausible range, % (never clamped: 100.4 is stored as 100.4)
CLOCK_TOLERANCE_S = 120

ANCHOR_KEYS = {"ht_gen1": "tmp", "ht_gen3": "temperature:0", "flood_gen1": "flood"}


@dataclass(frozen=True)
class Report:
    device_id: str
    ts: int | None
    ts_source: str
    t_c: float | None
    rh: float | None
    battery: float | None
    flood: int | None
    raw: str
    field_rejects: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParseReject:
    device_id: str
    reason: str


def parse_cloud_ts(value: Any) -> int | None:
    """`_updated` ("YYYY-MM-DD HH:MM:SS", UTC) to unix seconds, or None."""
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value + "+00:00").timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def _num(value: Any) -> float | None:
    """A finite int/float as float; bool, None, strings and NaN/inf become None (never 0)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _get(block: Any, *path: str) -> Any:
    node = block
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


class _Tally:
    """Collects the field-level rejects and warnings of one device block."""

    def __init__(self) -> None:
        self.rejects = 0
        self.warnings: list[str] = []

    def field(
        self, value: Any, label: str, bounds: tuple[float, float] | None = None
    ) -> float | None:
        """Guarded numeric field: None is routine; garbage or out-of-range is counted."""
        if value is None:
            return None
        number = _num(value)
        if number is None:
            self.rejects += 1
            self.warnings.append(f"{label} is not a finite number, stored as NULL")
            return None
        if bounds is not None and not bounds[0] <= number <= bounds[1]:
            self.rejects += 1
            self.warnings.append(f"{label} out of range, stored as NULL")
            return None
        return number


def _valid_value(block: Any, key: str, field: str) -> Any:
    """Gen1 sub-object value, only when the device flags it valid."""
    if _get(block, key, "is_valid") is not True:
        return None
    return _get(block, key, field)


def _sensor_failed(block: dict[str, Any]) -> bool:
    """Gen1 `sensor_error` (absent on older firmware) set and non-zero."""
    flag = block.get("sensor_error")
    return flag is not None and flag != 0


def _device_unixtime(model: str, block: dict[str, Any]) -> int | None:
    """The device's own clock, used only for a cross-check, never as a timestamp."""
    raw = _get(block, "sys", "unixtime") if model == "ht_gen3" else block.get("unixtime")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw > 0 else None


def parse_device(model: str, device_id: str, block: Any) -> Report | ParseReject:
    anchor = ANCHOR_KEYS.get(model)
    if anchor is None or not isinstance(block, dict) or anchor not in block:
        return ParseReject(device_id, "shape")

    ts: int | None
    if "_updated" in block:
        ts = parse_cloud_ts(block["_updated"])
        if ts is None:
            return ParseReject(device_id, "bad_ts")
        ts_source = TS_SOURCE_CLOUD
    else:
        ts, ts_source = None, TS_SOURCE_POLL

    tally = _Tally()
    t_c: float | None
    rh: float | None
    flood: int | None = None
    if model == "ht_gen3":
        t_c = tally.field(_get(block, "temperature:0", "tC"), "temperature", T_BOUNDS)
        rh = tally.field(_get(block, "humidity:0", "rh"), "humidity", RH_BOUNDS)
        battery = tally.field(_get(block, "devicepower:0", "battery", "percent"), "battery")
    else:
        failed = _sensor_failed(block)
        t_c = (
            None
            if failed
            else tally.field(_valid_value(block, "tmp", "tC"), "temperature", T_BOUNDS)
        )
        battery = tally.field(_get(block, "bat", "value"), "battery")
        if model == "ht_gen1":
            rh = (
                None
                if failed
                else tally.field(_valid_value(block, "hum", "value"), "humidity", RH_BOUNDS)
            )
        else:  # flood_gen1: no humidity sensor, `flood` must be a bool
            rh = None
            raw_flood = block.get("flood")
            if isinstance(raw_flood, bool):
                flood = int(raw_flood)
            elif raw_flood is not None:
                tally.rejects += 1
                tally.warnings.append("flood flag is not a boolean, stored as NULL")

    device_clock = _device_unixtime(model, block)
    if ts is not None and device_clock is not None and abs(device_clock - ts) > CLOCK_TOLERANCE_S:
        tally.warnings.append(
            f"device clock differs from cloud report time by {abs(device_clock - ts)} s"
        )

    return Report(
        device_id=device_id,
        ts=ts,
        ts_source=ts_source,
        t_c=t_c,
        rh=rh,
        battery=battery,
        flood=flood,
        raw=json.dumps(block, separators=(",", ":")),
        field_rejects=tally.rejects,
        warnings=tuple(tally.warnings),
    )
