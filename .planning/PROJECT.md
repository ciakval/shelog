# shelog (shelly-climate-log)

## What This Is

A small service on the owner's VPS (Bombur) that continuously logs temperature and humidity from 7 battery-powered Shelly sensors in an apartment under renovation — four lying on the exposed subfloor of the living room/kitchen, one Shelly Flood in its centre, and two in reference rooms that still have wooden floors — together with outdoor weather for the same location. A public read-only web page at `shelog.remesh.cz` shows long, fine-grained, overlaid history and wood-flooring metrics, so the owner can judge with data whether the subfloor is dry enough for a new wooden floor.

Full original specification: `SPEC.md` (repo root). This document supersedes it where they differ.

## Core Value

**Collect a complete, trustworthy, fine-grained (~5 min) climate history from every sensor, starting as soon as possible.** Every day not logged is lost forever; visualization and analysis can be built later on top of good data.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] Discover real Shelly Cloud response shapes per device model (`probe`), commit sanitized fixtures, derive parsers from them
- [ ] Unattended poller: all devices in one request per interval (default 60 s), ≤1 req/s, backoff + 429 handling, survives reboots and cloud/network outages
- [ ] Deduplicated storage of raw, uncalibrated readings in SQLite with report-time source; every poll attempt logged so gaps are explainable; raw JSON retained (toggleable)
- [ ] Outdoor weather (temperature, RH, dew point, precipitation, and — because the slab is in ground contact — soil moisture/soil temperature; plus other cheap useful variables) for the flat's location from Open-Meteo, stored alongside sensor data, including backfill for the period already logged
- [ ] Deployed on Bombur via the standard pipeline (GitHub Actions → GHCR → bombur-deploy) as early as possible — poller first, UI later
- [ ] Alerts via ntfy.sh when polling fails for > 1 h, the auth key is rejected, or a sensor goes silent beyond a threshold
- [ ] Derived metrics at read time: dew point, absolute humidity, wood EMC (Hailwood–Horrobin), with per-device calibration offsets; unit-tested
- [ ] JSON API: devices, readings (with forward-fill resampling), stats, health
- [ ] Single public read-only web page: time-range presets + zoom/pan; overlaid T / RH / EMC / dew-point charts incl. outdoor series; device + group selection; step rendering with gap breaks; optional RH band; stats table; health indicator
- [ ] CSV export (raw or resampled)
- [ ] Calibration CLI (offsets vs median sensor over a co-location window)

### Out of Scope

- Automated "is it dry yet" verdicts / decision logic — owner wants data first; analysis comes later
- Grafana / InfluxDB / MQTT / any local infrastructure in the flat — heavyweight, and no always-on host in the flat
- Authentication / user accounts — UI is intentionally public read-only
- Write/control actions on devices — logging only

## Context

- **Site history:** the flat is on the ground floor (level 0); the problem area (living room + kitchen) has **no cellar beneath — slab in ground contact**. A previously installed wooden floor there **warped badly** and was removed. The owner wants to reinstall a wooden floor if conditions allow. The sensors do not measure material moisture; their purpose is to characterize the **general environment the new floor would live in** (subfloor-surface air vs. reference rooms vs. outdoors), including how it responds to weather (rain, soil moisture, seasons). Ground-contact makes outdoor soil-moisture/soil-temperature and precipitation data relevant.
- Owner is an embedded/systems developer (C, Rust, Python) who values clean, minimal, well-automated solutions. Cares about the result; **technical and architectural choices in SPEC.md are recommendations, not mandates.**
- Devices: 5× H&T Gen1, 1× H&T Gen3, 1× Flood Gen1 (temperature only, no RH). Device IDs and which unit is the Gen3 are not yet known — to be collected during the probe step.
- Devices sleep and push to Shelly Cloud only on change (threshold 0.5 °C / 0.5 %RH) or periodic wake. The cloud holds only the last state → poll faster than target resolution; values are sample-and-hold (forward-fill / step semantics everywhere; time-weighted means).
- Shelly Cloud Control API: `auth_key` + account-specific host; v2 `POST /v2/devices/api/get` handles up to 10 ids per call; 1 req/s limit; key changes with the account password. Status field paths are unverified (see SPEC §3). Real Time Events API (WebSocket push) is a possible later improvement.
- Hosting: Bombur VPS (Debian 13), Docker Compose per app in `/home/deploy/DOCKER/shelog`, Caddy reverse proxy on the `proxy` network, automatic HTTPS, `main` deploys to production; persistent state in bind-mounted `./data`; runtime secrets in server `.env`. Conventions in the `bombur-infra` skill.
- Outdoor data: Open-Meteo (free, no key, model-based) for configurable lat/lon.

## Constraints

- **Deployment**: Bombur standard pipeline (Docker image tagged by SHA, `compose.yaml`, `bombur-onboard`, reusable `ciakval/bombur-deploy` workflow) — owner's chosen infrastructure; replaces the systemd plan in SPEC §4
- **Rate limit**: never exceed 1 request/second to Shelly Cloud — account-wide API limit
- **Secrets**: Shelly auth key and host only in server `.env`; never in git, logs, or the public UI/API
- **Timeline**: poller must be collecting in production before UI work starts — data is time-critical
- **Footprint**: minimal dependencies, no heavyweight stacks — owner preference
- **Public UI**: everything served is world-readable; must not leak secrets or device auth details

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Deploy via Bombur Docker/GHCR pipeline instead of systemd units | Owner's standard infra; consistent ops across projects | — Pending |
| Outdoor weather from Open-Meteo | Free, no key, any location, historical backfill available | — Pending |
| Public read-only UI, no auth | It's only climate data; simplest access | — Pending |
| Alerting via ntfy.sh | Trivial phone push, no SMTP needed | — Pending |
| Repo stays public; `devices.toml` lives on the server (bind-mounted), not in the image/repo — only a `devices.example.toml` is committed | Device IDs must not be published | — Pending |
| App name `shelog` (`shelog.remesh.cz`, container `shelog-web`) | Matches repo | — Pending |
| Recommended (not mandated) stack: Python ≥3.11, uv, httpx, stdlib sqlite3 (WAL), small web framework, vanilla JS + uPlot | From SPEC; owner treats tech choices as recommendations | — Pending |
| Store raw uncalibrated values; derive metrics + calibration at read time | Keeps data reusable if formulas/offsets change | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-10-31 after initialization*
