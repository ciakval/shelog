"""Read-side series: calibration, derived metrics, step-correct alignment, stats.

Readings are sample-and-hold: a value holds from its report until the next one. Series are
forward-filled onto one shared x grid (uPlot AlignedData). A value is NOT held (null, a
break in the chart) once a device has been silent longer than its ``max_silence_s`` or while
the poller itself was down. Derived metrics are computed per report, before resampling.
"""

from __future__ import annotations

import bisect
import itertools
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from shelog import metrics
from shelog.config import Device

DEFAULT_MAX_SILENCE_S = 54_000
OUTAGE_S = 1800  # no ok poll for this long = poller outage (values unknown)
OUTAGE_GRACE_S = 300
MAX_POINTS = 5000
RESAMPLE = {"none": 0, "5m": 300, "15m": 900, "1h": 3600}
FIELDS = ("t", "rh", "dew_point", "abs_humidity", "emc", "battery")


@dataclass(frozen=True)
class Report:
    ts: int
    values: dict[str, float | None]


def has_rh(dev: Device) -> bool:
    return dev.model != "flood_gen1"


def derive(dev: Device, t_raw: float | None, rh_raw: float | None, battery: float | None):
    t = None if t_raw is None else t_raw + dev.t_offset
    rh = None
    if rh_raw is not None and has_rh(dev):
        rh = min(100.0, max(0.0, rh_raw + dev.rh_offset))
    both = t is not None and rh is not None
    return {
        "t": metrics.round_or_none(t),
        "rh": metrics.round_or_none(rh),
        "dew_point": metrics.round_or_none(metrics.dew_point(t, rh)) if both else None,
        "abs_humidity": metrics.round_or_none(metrics.abs_humidity(t, rh)) if both else None,
        "emc": metrics.round_or_none(metrics.emc(t, rh)) if both else None,
        "battery": battery,
    }


def load_reports(con: sqlite3.Connection, dev: Device, start: int, end: int) -> list[Report]:
    """Reports in [start, end] plus the one held at ``start`` (if any)."""
    rows = con.execute(
        "SELECT ts, t_c, rh, battery FROM ("
        " SELECT ts, t_c, rh, battery FROM reading WHERE device_id=? AND ts<?"
        " ORDER BY ts DESC LIMIT 1)"
        " UNION ALL SELECT ts, t_c, rh, battery FROM reading"
        " WHERE device_id=? AND ts>=? AND ts<=? ORDER BY ts",
        (dev.id, start, dev.id, start, end),
    ).fetchall()
    return [Report(ts, derive(dev, t, rh, bat)) for ts, t, rh, bat in rows]


def outages(con: sqlite3.Connection, start: int, end: int) -> list[tuple[int, int]]:
    """Windows (a, b) where the poller had no successful poll for longer than OUTAGE_S."""
    polls = [
        r[0]
        for r in con.execute(
            "SELECT ts FROM poll_log WHERE kind='shelly' AND ok=1 AND ts>=? AND ts<=? ORDER BY ts",
            (start - OUTAGE_S, end + OUTAGE_S),
        )
    ]
    windows = []
    for a, b in itertools.pairwise(polls):
        if b - a > OUTAGE_S:
            windows.append((a + OUTAGE_GRACE_S, b))
    return windows


def _in_outage(x: int, windows: Sequence[tuple[int, int]]) -> bool:
    return any(a <= x < b for a, b in windows)


def _value_at(
    reports: Sequence[Report], times: Sequence[int], x: int, field: str, limit: int
) -> float | None:
    i = bisect.bisect_right(times, x) - 1
    if i < 0:
        return None
    rep = reports[i]
    if x - rep.ts > limit:
        return None
    return rep.values[field]


@dataclass(frozen=True)
class DeviceData:
    dev: Device
    reports: list[Report]

    @property
    def limit(self) -> int:
        return self.dev.max_silence_s or DEFAULT_MAX_SILENCE_S


def grid(
    data: Sequence[DeviceData],
    start: int,
    end: int,
    step: int,
    windows: Sequence[tuple[int, int]],
) -> list[int]:
    """Shared x grid: fixed step, or every report time plus explicit break points."""
    if step:
        first = start - start % step
        return list(range(first if first >= start else first + step, end + 1, step))
    points: set[int] = {start}
    for d in data:
        for rep in d.reports:
            if start <= rep.ts <= end:
                points.add(rep.ts)
        for rep, nxt in zip(d.reports, [*d.reports[1:], None], strict=True):
            silent_at = rep.ts + d.limit + 1
            if (nxt is None or nxt.ts > silent_at) and start <= silent_at <= end:
                points.add(silent_at)  # explicit break point
    for a, b in windows:
        points.update(x for x in (a, b) if start <= x <= end)
    return sorted(points)


def column(
    d: DeviceData, field: str, xs: Sequence[int], windows: Sequence[tuple[int, int]]
) -> list[float | None]:
    """Forward-filled values of ``field`` at each x (null when unknown)."""
    times = [r.ts for r in d.reports]
    return [
        None if _in_outage(x, windows) else _value_at(d.reports, times, x, field, d.limit)
        for x in xs
    ]


def downsample(
    xs: list[int], cols: list[list[float | None]], max_points: int = MAX_POINTS
) -> tuple[list[int], list[list[float | None]]]:
    """Step-preserving min/max per bucket (3 points: min, max, break-if-any)."""
    if len(xs) <= max_points:
        return xs, cols
    n_buckets = max_points // 3
    span = xs[-1] - xs[0] + 1
    width = max(3, -(-span // n_buckets))
    out_x: list[int] = []
    out_cols: list[list[float | None]] = [[] for _ in cols]
    i = 0
    while i < len(xs):
        b0 = xs[0] + (xs[i] - xs[0]) // width * width
        j = i
        while j < len(xs) and xs[j] < b0 + width:
            j += 1
        out_x.extend([b0, b0 + width // 3, b0 + 2 * width // 3])
        for c, col in enumerate(cols):
            chunk = col[i:j]
            vals = [v for v in chunk if v is not None]
            lo = min(vals) if vals else None
            hi = max(vals) if vals else None
            brk = None if any(v is None for v in chunk) else (hi if hi is not None else None)
            out_cols[c].extend([lo, hi, brk])
        i = j
    return out_x, out_cols


def stats(
    d: DeviceData,
    start: int,
    end: int,
    windows: Sequence[tuple[int, int]],
    band: tuple[float, float] = (40.0, 60.0),
) -> dict[str, object]:
    """Time-weighted (held-value) stats over [start, end]; unknown time is excluded."""
    out: dict[str, object] = {"name": d.dev.name}
    # Segment boundaries: reports, silence cut-offs and outage edges inside the range.
    cuts = {start, end}
    for rep in d.reports:
        cuts.update(x for x in (rep.ts, rep.ts + d.limit) if start < x < end)
    for a, b in windows:
        cuts.update(x for x in (a, b) if start < x < end)
    edges = sorted(cuts)
    times = [r.ts for r in d.reports]
    fields = ("t", "rh", "emc") if has_rh(d.dev) else ("t",)
    acc = {f: [0.0, 0, None, None] for f in fields}  # weighted sum, seconds, min, max
    outside = 0
    rh_seconds = 0
    for a, b in itertools.pairwise(edges):
        if _in_outage(a, windows):
            continue
        for f in fields:
            v = _value_at(d.reports, times, a, f, d.limit)
            if v is None:
                continue
            slot = acc[f]
            slot[0] += v * (b - a)
            slot[1] += b - a
            slot[2] = v if slot[2] is None else min(slot[2], v)
            slot[3] = v if slot[3] is None else max(slot[3], v)
            if f == "rh":
                rh_seconds += b - a
                if not band[0] <= v <= band[1]:
                    outside += b - a
    for f, (total, secs, lo, hi) in acc.items():
        out[f] = {
            "min": lo,
            "max": hi,
            "mean": round(total / secs, 2) if secs else None,
        }
    if has_rh(d.dev):
        out["rh_outside_band_pct"] = round(100 * outside / rh_seconds, 1) if rh_seconds else None
    out["coverage_pct"] = (
        round(100 * acc["t"][1] / (end - start), 1) if end > start and acc["t"][1] else 0.0
    )
    return out


def latest(con: sqlite3.Connection, devices: Iterable[Device]) -> dict[str, tuple[int, float]]:
    rows = con.execute(
        "SELECT r.device_id, r.ts, r.battery FROM reading r JOIN"
        " (SELECT device_id, MAX(ts) ts FROM reading GROUP BY device_id) m"
        " ON r.device_id = m.device_id AND r.ts = m.ts"
    ).fetchall()
    ids = {d.id for d in devices}
    return {dev: (ts, bat) for dev, ts, bat in rows if dev in ids}
