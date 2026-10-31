# Project Research Summary

**Project:** shelog (Shelly climate logger for a renovation subfloor)
**Domain:** Self-hosted sample-and-hold climate logger: Shelly Cloud polling, Open-Meteo outdoor data, SQLite time series, tiny public read-only UI
**Researched:** 2026-10-31
**Confidence:** MEDIUM overall (HIGH on stack, schema and Open-Meteo shapes; LOW on Shelly response shapes until the probe runs)

## Executive Summary

shelog is a single-host, always-on logger. It polls one Shelly Cloud v2 endpoint per minute for seven battery sensors (four on the exposed subfloor, one Flood in the centre, two reference rooms with wooden floors), stores deduplicated raw readings plus every poll attempt in SQLite WAL, pulls hourly outdoor weather from Open-Meteo, derives dew point, absolute humidity and wood EMC at read time, and serves a public read-only chart page. Experts build this pattern as a single writer with a functional core (pure parsers, dedup rule, metrics, resampling) and a thin IO shell. Sensor data is event-driven and sample-and-hold, so forward-fill, step rendering and time-weighted statistics are data semantics, not plot styling.

The recommended build is deliberately small: Python 3.13, `httpx2`, Starlette, uvicorn, stdlib `sqlite3`, vendored uPlot, and a hand-written sync poll loop. Ship two Compose services from one image (`poller` as the only writer, `web` read-only on the `proxy` network) so the public UI can never stop or leak from collection. The sequencing is fixed by the Core Value: a probe step must first pin down the real Shelly response shapes and per-model reporting behaviour, then collection and deployment come before any UI.

The main risks are: (1) Shelly field paths and timestamp semantics are unverified, and a wrong timestamp combined with `INSERT OR IGNORE` silently drops data; (2) reporting cadence is per model and long (Gen1 H&T and Flood report about every 12 h on battery), so a flat 6 h gap or silence rule draws false breaks and false alerts; (3) the auth key travels in the URL query and must never reach logs, errors, the database or the public API; (4) cross-container WAL and bind-mount UID/backup mistakes can lose data on deploy. Two project-specific corrections apply: the slab is in ground contact with no cellar, so outdoor soil moisture, soil temperature and precipitation are must-have variables (overriding FEATURES.md, which skipped soil); and the sensors measure subfloor-surface air, not material moisture, so the UI shows trends and deltas and never a "ready" verdict.

## Key Findings

### Recommended Stack
Three runtime dependencies (`httpx2`, `starlette`, `uvicorn` without extras) plus stdlib. One image, two Compose services, uv pinned to a specific tag, `python:3.13-slim`. Store all times as integer UTC unix seconds; use `timeformat=unixtime` from Open-Meteo. No ORM, no pandas/numpy, no APScheduler, no asyncio, no `openmeteo-requests`, no Pydantic. Vendor uPlot 1.6.32 into the package (no CDN at runtime).

**Core technologies:**
- Python 3.13 (`python:3.13-slim`): runtime; SQLite 3.46 gives WAL, STRICT tables, UPSERT, `unixepoch()`.
- `httpx2` 2.16.x (`>=2.16,<3`): sync HTTP client for Shelly, Open-Meteo, ntfy; `MockTransport` for tests. Young package; fallback is plain `httpx` 0.28.1 (one-line import change).
- Starlette 1.10.x + uvicorn 0.54.x (`--workers 1`): read-only JSON API and `StaticFiles`; plain `def` handlers run in the threadpool, right for blocking sqlite3.
- stdlib `sqlite3` in WAL mode: single writer, many readers; `query_only=ON` for the web connection; never `mode=ro` on the WAL database.
- uPlot 1.6.32 (vendored): stepped paths, null-broken gaps, synced cursors, multi-axis, `tzDate`.
- Dev tools: pytest 9.2, ruff 0.17 (select `DTZ` to enforce UTC discipline), mypy (optional, on pure modules).

### Expected Features

**Must have (table stakes, v1 "collection live"):**
- Unattended poller: one batched Shelly request per cycle (<= 10 ids), <= 1 req/s, backoff, 429/`Retry-After` handling, single-instance lock.
- Raw, uncalibrated, deduplicated readings with report-time source; `poll_log` for every attempt (shelly and weather) so gaps are explainable.
- Raw JSON retained on change (`raw_status`), enough to re-parse later.
- Outdoor logging with backfill (Open-Meteo, hourly): temperature, RH, dew point, surface pressure, precipitation, shortwave radiation, wind speed, cloud cover, plus **soil moisture and soil temperature** (ground-contact slab).
- Health/staleness visibility and ntfy alerts for collector health only: poll failing > 1 h, auth rejected, device silent beyond its per-model threshold, battery low.
- Production deploy on Bombur early, poller before UI.

**Should have (v1.x, readable data):**
- JSON API with forward-fill resampling, stats (time-weighted), health, CSV export (raw or resampled, with calibration offsets recorded).
- Derived metrics module (dew point, absolute humidity, wood EMC) with unit tests; EMC labelled as equilibrium in this air.
- Single-page UI: presets, zoom/pan, overlays, step rendering with gap breaks, device/group selection, health badge.
- Outdoor overlay on indoor charts; indoor-minus-outdoor absolute humidity; subfloor-minus-reference delta (dry-standard style convergence).
- RH band shading with "% of time outside band" as display only.
- Calibration CLI after a 24 h co-location window (offsets applied at read time, so history is re-based retroactively).

**Defer (v2+):**
- Event annotations, drying-trend slope/plateau summary, data-quality overlay, manual entry of CM / pin-meter readings as reference points, Real Time Events (WebSocket) push, cloud-stats backfill (needs HAR capture).

**Anti-features (do not build):**
- Automated "ready for flooring" verdict or traffic light: sensors cannot measure screed or slab moisture; thresholds vary by manufacturer and substrate.
- Grafana/InfluxDB/MQTT stack, user accounts, device control, ML drying forecasts, smoothing or bucket means of raw samples, storing derived values in the DB, rollup/compaction jobs, push notifications per threshold crossing.

### Architecture Approach
One Python package (`shelog`), one image, two services. Data flows Shelly/Open-Meteo -> `poller` (sole writer) -> SQLite file in a bind-mounted `./data` -> `web` (read-only connections) -> browser. The only channel between services is the database file. Full schema is defined in migration `0001` on day one (reading, raw_status, outdoor in long format, poll_log, alert_state, meta), and later changes are additive only. Derived metrics, calibration offsets and band/group config live outside the schema.

**Major components:**
1. `shelly.client` (IO): one batched v2 call per cycle; owns 1.1 s minimum gap, timeouts, 429/`Retry-After`, error classification into a fixed category enum.
2. `shelly.parse` (pure): one parser per model (`ht_gen1`, `ht_gen3`, `flood_gen1`), tested against sanitized fixtures from the probe; per-device try/except.
3. `store` (write side): `should_insert(prev, new)` pure dedup; one transaction per cycle; poll_log written for failures too.
4. `poller` (scheduler): single-threaded loop on `time.monotonic()` deadlines; jobs for shelly (60 s), weather (15-60 min), alerts (after each cycle), housekeeping (daily `VACUUM INTO` backup); SIGTERM via `Event.wait`.
5. `weather.openmeteo`: GET forecast API, parse (pure), upsert `ON CONFLICT(ts, variable) DO UPDATE`; drop rows with `ts > now`.
6. `metrics` + `series` (pure): dew point, absolute humidity, EMC; forward-fill, step-preserving min/max downsample, time-weighted stats, gap intervals.
7. `query` + `web`: read pipeline (carry-in row, offsets, derive per report, then resample), columnar JSON, `Cache-Control: public, max-age=30`.
8. `alerts` (pure rules + ntfy sender): state-transition alerts persisted in `alert_state`; ntfy failure never breaks the loop.

**Key patterns:**
- Derive before resample (EMC is nonlinear; never compute it from bucket min T and min RH).
- Carry-in row is mandatory for every range; final held segment ends at `min(to, now)`.
- Two kinds of flat stretch, never conflated: poller outage (unknown, break the line, from `poll_log`) versus device quiet (hold is valid until the per-device silence threshold).
- Store facts, derive at read time. `devices.toml` lives on the server (bind-mounted, not in the public repo or image); only `devices.example.toml` is committed.

### Critical Pitfalls

1. **Timestamp validity and silent drops.** A `ts` of 0 or a wrong zone collides on the primary key and `INSERT OR IGNORE` hides it. **Avoid:** probe decides `ts_source` per model; validator rejects 0, negative, future and ancient values; count ignored rows; warn when a device is OK but never inserts; parse `_updated` as explicit UTC.
2. **HTTP 200 and `online` treated as success or liveness.** Body-level `isok`/errors; `online=0` is normal for sleeping devices; a device missing from the response is a per-device no-op; Gen1 `null` on sensor error must never become 0. **Avoid:** fixed outcome classes; staleness only from report timestamp; read `tmp.tC` not `tmp.value`.
3. **Per-model reporting cadence versus a flat 6 h threshold.** Gen1 H&T and Flood report about every 12 h on battery (10 min on external power); Gen3 H&T about every 2 h. **Avoid:** per-device `max_expected_silence`; gap breaks only for poller outages or silence beyond that threshold.
4. **Sample-and-hold implemented as a plot style.** **Avoid:** time-weighted pure module with hand-computed tests (held across range start, across a gap, final partial segment, two reports in one bucket); min/max for downsampling; verify uPlot stepped alignment direction.
5. **EMC unit mistake and loose tests.** Hailwood-Horrobin uses °F; passing °C is numerically close near 21 °C. **Avoid:** public functions take `t_c` and `rh` only; tight pinned tests (21 °C/50 % = 9.25 %, 21 °C/65 % = 11.97 %, 0 °C/50 % = 9.52 %, 30 °C/60 % = 10.64 %, 40 °C/60 % = 10.20 %); RH clamped to (0, 100]; Flood returns None for RH metrics.
6. **Auth key leakage.** v2 takes `auth_key` in the query string; httpx logs and exception text include it, and `poll_log.error` is public via `/api/health`. **Avoid:** loggers at WARNING, redaction filter, error categories only; probe checks for a header/body variant; sanitized fixtures; no lat/lon or device ids in public output.
7. **Cross-container WAL, UID, backup and bind-mount traps.** `./data` mounted read-write in both services with the same UID; DB inside the mount; backups via `VACUUM INTO`; clean SIGTERM. `bombur-add-app` replaces literal `CHANGE_ME` with random values — Shelly key/host need a different placeholder. **Avoid:** deploy-time spike (writer in one container, fresh read in the other; redeploy keeps row count).

## Implications for Roadmap

Suggested sequence (names are suggestions for the roadmapper):

- **Probe and fixtures (gate):** sanitized fixtures per model incl. error/asleep variants; `ts_source` per model; `_updated` timezone; wrong key/host, unknown id, 429 behaviour; per-device `/settings`; auth-key transport; sanitizer test. Plus a short Open-Meteo spike on soil/precip variable names and `models=` for the flat's coordinates.
- **Core collector:** config (env + server-side `devices.toml`, refuses placeholder secrets), db + full schema `0001`, shelly client, per-model parsers, `should_insert`, scheduler with SIGTERM, flock, redaction filter.
- **Deploy #1 on Bombur:** Dockerfile, two-service compose (web as placeholder health), CI test gate, daily backup, deploy-survival and cross-container WAL smoke tests. Collection live.
- **Alerts and health:** state-transition ntfy alerts (`poll_down`, `auth_rejected`, `silent:<id>`, `battery_low`, `db_error`), per-device data-tuned silence thresholds, `/api/health`, dead-man's-switch decision.
- **Outdoor weather with backfill:** forecast API hourly with `past_days` (≤92), pinned model, upsert, separate backoff; curated variables incl. soil moisture/temperature and precipitation; attribution. Not time-critical thanks to the 92-day window, but valuable early.
- **Read side:** metrics, series, query pipeline, `/api/*`, CSV export.
- **Public read-only UI:** vendored uPlot, presets, zoom, synced charts, step rendering, outdoor overlay labelled as model data, health badge, `Europe/Prague` display zone.
- **Later:** calibration CLI, `reparse`, data-quality overlay, annotations, trend slopes.

### Research Flags
- **Probe:** blocking — Shelly v2 shapes, timestamps, errors, key transport.
- **Weather:** soil variable names/depths, model choice, forecast vs historical-forecast seams.
- **Alerts:** silence thresholds tuned from real data.
- **UI:** uPlot stepped alignment, `uPlot.join`, wheel-zoom plugin.
- Standard patterns (skip research): collector internals, deploy (Bombur docs), metrics/series/API, calibration CLI.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Versions verified 2026-10-31; Open-Meteo shapes verified live; `httpx2` young (MEDIUM-HIGH), `httpx` fallback. |
| Features | MEDIUM | Flooring thresholds indicative; check against manufacturer sheet and local standard (ČSN / DIN 18560). |
| Architecture | MEDIUM-HIGH | Cross-container WAL is an inference to prove at deploy; Shelly field paths LOW until probe. |
| Pitfalls | MEDIUM | Gen1/Gen3 cadence and Open-Meteo facts HIGH; Shelly error/429/timestamp behaviour MEDIUM until probe; EMC values computed locally. |

**Overall confidence:** MEDIUM. Shelly response shapes are the single LOW-confidence input and gate the first phase.

### Gaps to Address
- Shelly v2 response shapes, timestamp semantics, batch partial failure — resolve in probe.
- Wrong key/host and 429 responses — capture in probe within the rate limit.
- Auth key transport — test header/body; otherwise redaction is the only control.
- Per-device settings incl. on-device temperature offset — read `/settings` for all seven.
- Soil variables (names, depths, availability) — spike; label as model output, never a slab measurement.
- Open-Meteo backfill source conflict — forecast API with pinned `models=` within 92 days; historical-forecast only for older data.
- EMC formula cross-check against Wood Handbook tables.
- Flooring thresholds indicative only; no default verdict.
- Dead-man's switch for a dead poller/VPS — decide during alerts work.
- `httpx2` maturity — keep the client behind one module.

## Sources

### Primary (HIGH confidence)
- Open-Meteo forecast, historical, historical-forecast docs, pricing and terms (live calls 2026-10-31)
- Shelly Cloud Control API v1/v2 docs; Shelly Gen1 API docs; Allterco knowledge base
- PyPI and npm registries (2026-10-31); Starlette release notes; SQLite WAL docs; uv Docker guide; uPlot repo; ntfy docs
- Owner's `bombur-infra` skill and `bombur-deploy` script

### Secondary (MEDIUM confidence)
- Shelly community forum and third-party client docs; Simon Willison on SQLite WAL across Docker containers (Apr 2026); vdp/baulinks 2022 screed CM limits; Wagner Meters ASTM F2170; NWFA jobsite conditions

### Tertiary (LOW confidence)
- bau.de forum on calcium-sulfate screed limits; IICRC S500 second-hand; local EMC computations

---
*Research completed: 2026-10-31*
*Ready for roadmap: yes; Shelly shapes remain the open gate (probe phase)*
