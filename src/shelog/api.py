"""Public JSON API bodies. Pure functions over a read-only connection.

Devices are addressed by display NAME only. Nothing here returns ids, MACs, coordinates,
raw payloads or error text.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from collections.abc import Mapping
from typing import Any

from shelog import metrics, series, weather
from shelog.config import Device

MAX_RANGE_S = 400 * 86400
DEFAULT_RANGE_S = 86400
DEFAULT_FIELDS = ("t", "rh", "emc", "dew_point")
OUTDOOR_DEFAULT = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "soil_temperature_6cm",
    "soil_moisture_3_to_9cm",
)
ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0), model data, not measured at the flat"


class BadRequest(Exception):
    pass


def _int(params: Mapping[str, str], key: str) -> int | None:
    raw = params.get(key, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise BadRequest(f"{key} must be unix seconds") from None


def time_range(
    con: sqlite3.Connection | None, params: Mapping[str, str], now: int
) -> tuple[int, int]:
    end = _int(params, "to") or now
    if params.get("from", "").strip() == "all":
        first = con.execute("SELECT MIN(ts) FROM reading").fetchone()[0] if con else None
        start = first if first is not None else end - DEFAULT_RANGE_S
    else:
        start = _int(params, "from") or end - DEFAULT_RANGE_S
    end = min(end, now)
    if start >= end:
        raise BadRequest("from must be before to")
    if end - start > MAX_RANGE_S:
        raise BadRequest("range too long (max 400 days)")
    return start, end


def select_devices(devices: tuple[Device, ...], params: Mapping[str, str]) -> list[Device]:
    raw = params.get("devices", "").strip()
    if not raw:
        return list(devices)
    wanted = [n.strip() for n in raw.split(",") if n.strip()]
    by_name = {d.name: d for d in devices}
    groups = {d.group for d in devices}
    out: list[Device] = []
    for name in wanted:
        if name in groups:
            out.extend(d for d in devices if d.group == name and d not in out)
        elif name in by_name:
            if by_name[name] not in out:
                out.append(by_name[name])
        else:
            raise BadRequest("unknown device")
    return out


def devices_body(devices: tuple[Device, ...]) -> dict[str, Any]:
    return {
        "devices": [
            {
                "name": d.name,
                "group": d.group,
                "model": d.model,
                "has_rh": series.has_rh(d),
                "max_silence_s": d.max_silence_s or series.DEFAULT_MAX_SILENCE_S,
            }
            for d in devices
        ]
    }


def _load(con: sqlite3.Connection, devs: list[Device], start: int, end: int):
    data = [series.DeviceData(d, series.load_reports(con, d, start, end)) for d in devs]
    return data, series.outages(con, start, end)


def readings_body(
    con: sqlite3.Connection | None,
    devices: tuple[Device, ...],
    params: Mapping[str, str],
    now: int,
) -> dict[str, Any]:
    start, end = time_range(con, params, now)
    devs = select_devices(devices, params)
    resample = params.get("resample", "none").strip() or "none"
    if resample not in series.RESAMPLE:
        raise BadRequest("resample must be none, 5m, 15m or 1h")
    fields = [f.strip() for f in params.get("fields", ",".join(DEFAULT_FIELDS)).split(",")]
    if not fields or any(f not in series.FIELDS for f in fields):
        raise BadRequest("unknown field")
    body: dict[str, Any] = {
        "from": start,
        "to": end,
        "resample": resample,
        "devices": [d.name for d in devs],
        "x": [],
        "fields": {f: [[] for _ in devs] for f in fields},
        "outages": [],
    }
    if con is None or not devs:
        return body
    data, windows = _load(con, devs, start, end)
    step = series.RESAMPLE[resample]
    if step and (end - start) // step > series.MAX_POINTS * 4:
        raise BadRequest("too many points for this resample; pick a coarser one")
    xs = series.grid(data, start, end, step, windows)
    cols = [series.column(d, f, xs, windows) for f in fields for d in data]
    xs, cols = series.downsample(xs, cols)
    n = len(devs)
    body["x"] = xs
    body["fields"] = {f: cols[i * n : (i + 1) * n] for i, f in enumerate(fields)}
    body["outages"] = [[a, b] for a, b in windows]
    return body


def stats_body(
    con: sqlite3.Connection | None,
    devices: tuple[Device, ...],
    params: Mapping[str, str],
    now: int,
) -> dict[str, Any]:
    start, end = time_range(con, params, now)
    devs = select_devices(devices, params)
    try:
        lo = float(params.get("band_lo", "40"))
        hi = float(params.get("band_hi", "60"))
    except ValueError:
        raise BadRequest("band must be numbers") from None
    if not 0 <= lo < hi <= 100:
        raise BadRequest("band must satisfy 0 <= lo < hi <= 100")
    if con is None:
        return {"from": start, "to": end, "band": [lo, hi], "devices": []}
    data, windows = _load(con, devs, start, end)
    last = series.latest(con, devs)
    rows = []
    for d in data:
        row = series.stats(d, start, end, windows, (lo, hi))
        ts, battery = last.get(d.dev.id, (None, None))
        row.update({"group": d.dev.group, "last_report": ts, "battery": battery})
        rows.append(row)
    return {"from": start, "to": end, "band": [lo, hi], "devices": rows}


def outdoor_body(
    con: sqlite3.Connection | None, params: Mapping[str, str], now: int
) -> dict[str, Any]:
    start, end = time_range(con, params, now)
    raw = params.get("vars", "").strip()
    wanted = [v.strip() for v in raw.split(",")] if raw else list(OUTDOOR_DEFAULT)
    allowed = set(weather.VARIABLES) | {"abs_humidity"}
    if any(v not in allowed for v in wanted):
        raise BadRequest("unknown variable")
    body: dict[str, Any] = {
        "from": start,
        "to": end,
        "attribution": ATTRIBUTION,
        "x": [],
        "vars": {v: [] for v in wanted},
    }
    if con is None:
        return body
    need = set(wanted) - {"abs_humidity"}
    if "abs_humidity" in wanted:
        need |= {"temperature_2m", "relative_humidity_2m"}
    marks = ",".join("?" * len(need))
    table: dict[int, dict[str, float | None]] = {}
    for ts, var, value in con.execute(
        f"SELECT ts, variable, value FROM outdoor WHERE ts>=? AND ts<=?"  # noqa: S608
        f" AND variable IN ({marks}) ORDER BY ts",
        (start, end, *sorted(need)),
    ):
        table.setdefault(ts, {})[var] = value
    xs = sorted(table)
    body["x"] = xs
    for v in wanted:
        if v == "abs_humidity":
            col = []
            for x in xs:
                t, rh = table[x].get("temperature_2m"), table[x].get("relative_humidity_2m")
                col.append(
                    None if t is None or rh is None else round(metrics.abs_humidity(t, rh), 2)
                )
            body["vars"][v] = col
        else:
            body["vars"][v] = [table[x].get(v) for x in xs]
    return body


def readings_csv(body: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    fields = list(body["fields"])
    w.writerow(["ts_utc"] + [f"{name}:{f}" for f in fields for name in body["devices"]])
    for i, x in enumerate(body["x"]):
        w.writerow([x] + [col[i] for f in fields for col in body["fields"][f]])
    return buf.getvalue()
