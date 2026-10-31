# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Greenfield. The only source of truth is `SPEC.md` — read it fully before writing code. Sections marked **TBD** (device IDs, Shelly status field paths, v2 response envelope) must be resolved with the user or from real API responses, not guessed. Update this file once the project skeleton exists.

## Secrets and live credentials

`.env` (Shelly key/host) and `devices.toml` are gitignored. A hook blocks Claude from reading `.env` in any form — don't try to work around it. Code loads secrets at runtime; when a command needs live credentials, ask the owner to run it with the `!` prefix and make sure it never prints the key. The repo is public: never write real device IDs, MACs, IPs, SSIDs or coordinates into committed files (fixtures must be sanitized). Verify commands must not contain the literal text `.env` (`.env.example` is fine).

## What this is

`shelly-climate-log`: polls Shelly Cloud for 7 battery-powered temperature/humidity sensors (H&T Gen1/Gen3, Flood Gen1) that monitor a drying subfloor, stores readings in SQLite, and serves a single-page chart UI with wood-flooring metrics (dew point, absolute humidity, wood EMC).

## Tooling (planned per SPEC §4)

Python ≥3.11, `uv` for env/deps, `httpx`, stdlib `sqlite3` + `tomllib`, `ruff`, `pytest`. Keep dependencies minimal; no Grafana/Influx or other heavy stacks. Frontend is vanilla JS + uPlot from CDN, no build step.

```
uv sync
uv run ruff check . && uv run ruff format .
uv run pytest                          # all tests
uv run pytest tests/test_x.py::test_y  # single test
uv run shelly-climate-log probe|poll [--once]|serve|export|calibrate
```

## Architecture

Two independent processes share one SQLite DB (WAL mode):

- **Poller** (long-running): one `POST /v2/devices/api/get` per interval (default 60 s, jittered) for all devices; exponential backoff, honour 429; never exceed 1 req/s per account. Logs every attempt to `poll_log` so gaps are explainable.
- **Web app** (FastAPI or similar): opens the DB **read-only**; serves `/api/devices`, `/api/readings`, `/api/stats`, `/api/health` plus the static page.

Device inventory lives in `devices.toml` (id, name, group `subfloor|reference`, model, calibration offsets) — never hard-code devices. Secrets (`auth_key`, host) come from an env file, never logged or committed.

## Non-obvious invariants

- **Sample-and-hold data.** Devices sleep and push only on change/interval; the cloud keeps only the last state. All resampling, stats and plotting use forward-fill/step semantics and **time-weighted** means — never linear interpolation or plain bucket means. Gaps > threshold (default 6 h) render as breaks.
- **Dedup on insert:** insert a row only if the report timestamp advanced (or, if no reliable timestamp, if values changed). `ts_source` records where `ts` came from (`device|cloud|poll`).
- **Store raw, uncalibrated values**; apply `t_offset`/`rh_offset` and compute derived metrics at read time.
- **Server-side downsampling** above ~5k points/series must be step-preserving min/max per bucket so short excursions survive.
- **Derived metrics** (Magnus dew point, absolute humidity, Hailwood–Horrobin EMC with °F input) go in a pure, unit-tested module; SPEC §7 has the exact constants and sanity values (21 °C/50 % → ≈9.2 %, 21 °C/65 % → ≈11.9 %).
- Auth key rotates when the account password changes, and Shelly can relocate the host — surface auth failures loudly (health endpoint + banner).

## Development order (SPEC §9)

1. `probe` first; commit **sanitized** real responses as test fixtures and derive parsers/field paths from them.
2. Poller + DB + per-model parsers, tested against fixtures.
3. **Deploy the poller early** — every day not logged is lost data. UI comes after.
4. API + derived metrics → frontend → health/alerting → optional (Real Time Events API, backfill, calibration CLI).
