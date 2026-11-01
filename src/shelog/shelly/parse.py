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
    except ValueError:
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


def _valid_value(block: Any, key: str, field: str) -> float | None:
    """Gen1 sub-object value, only when the device flags it valid."""
    if _get(block, key, "is_valid") is not True:
        return None
    return _num(_get(block, key, field))


def _flood_flag(value: Any) -> int | None:
    return int(value) if isinstance(value, bool) else None


def parse_device(model: str, device_id: str, block: Any) -> Report | ParseReject:
    anchor = ANCHOR_KEYS.get(model)
    if anchor is None or not isinstance(block, dict) or anchor not in block:
        return ParseReject(device_id, "shape")
    ts = parse_cloud_ts(block.get("_updated"))
    if ts is None:
        return ParseReject(device_id, "no_ts")

    t_c: float | None
    rh: float | None
    flood: int | None = None
    if model == "ht_gen1":
        t_c = _valid_value(block, "tmp", "tC")
        rh = _valid_value(block, "hum", "value")
        battery = _num(_get(block, "bat", "value"))
    elif model == "ht_gen3":
        t_c = _num(_get(block, "temperature:0", "tC"))
        rh = _num(_get(block, "humidity:0", "rh"))
        battery = _num(_get(block, "devicepower:0", "battery", "percent"))
    else:  # flood_gen1
        t_c = _num(_get(block, "tmp", "tC"))
        rh = None
        battery = _num(_get(block, "bat", "value"))
        flood = _flood_flag(block.get("flood"))

    return Report(
        device_id=device_id,
        ts=ts,
        ts_source=TS_SOURCE_CLOUD,
        t_c=t_c,
        rh=rh,
        battery=battery,
        flood=flood,
        raw=json.dumps(block, separators=(",", ":")),
    )
