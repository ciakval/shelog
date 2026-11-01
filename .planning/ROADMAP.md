# Roadmap: shelog

## Overview

shelog's value is a complete climate history, and every day without logging is lost for good, so the roadmap gets collection running first and builds the visible parts later. Phase 1 uses the owner's real Shelly account to pin down the actual response shapes and builds the collector core from them, tested against sanitized fixtures. Phase 2 deploys that collector to Bombur, makes the data survive deploys and reboots, and sends the owner phone alerts. Phase 3 adds outdoor weather (including soil moisture and soil temperature, because the slab sits directly on the ground) and backfills it to the start of the indoor record. Phase 4 makes the history readable through calibrated, derived and correctly resampled JSON. Phase 5 adds the public chart page on top of that API.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Probe & Core Collector** - Find out what Shelly Cloud really returns, then build a collector that stores deduplicated, raw, correctly timestamped readings
- [ ] **Phase 2: Collection Live on Bombur** - The poller runs unattended in production with durable data, daily backups, a health endpoint and ntfy alerts
- [ ] **Phase 3: Outdoor Weather** - Hourly Open-Meteo weather and soil data for the flat's location is logged and backfilled to the start of the indoor record
- [ ] **Phase 4: Metrics & Read API** - Calibrated, derived (dew point, absolute humidity, EMC) and step-correct resampled history is served as public JSON
- [ ] **Phase 5: Public Web UI** - One public page shows overlaid indoor and outdoor history with presets, zoom, step rendering, stats and health

## Phase Details

### Phase 1: Probe & Core Collector
**Goal**: The owner can run the collector locally against the real Shelly account and see deduplicated, raw, correctly timestamped readings from all seven sensors land in SQLite, with every poll attempt explained and the auth key never exposed
**Depends on**: Nothing (first phase)
**Requirements**: PROBE-01, PROBE-02, PROBE-03, PROBE-04, PROBE-05, COLL-01, COLL-02, COLL-03, COLL-04, COLL-05, COLL-06, COLL-07, COLL-08
**Success Criteria** (what must be TRUE):
  1. Owner runs `shelog probe` with their real key and host and gets raw status JSON for all seven devices. Sanitized fixtures for H&T Gen1, H&T Gen3 and Flood Gen1 (including error, asleep and rate-limit responses) are committed, and a test fails if any fixture contains a real device ID, MAC, IP, SSID or key.
  2. For each model, a document records which field holds the real report time (`ts_source`, with its timezone meaning) and each device's reporting settings (wake period, thresholds, any on-device offset). Each device in `devices.toml` has a silence threshold based on those settings, and only `devices.example.toml` is in the repo.
  3. After running locally against the real account for at least an hour, the poller has sent one batched request per interval and never more than 1 request per second. The database holds a new row only when a device's report time moved forward, with raw uncalibrated T, RH, battery and flood values, sensor-error nulls kept as null, and the raw JSON saved alongside. Implausible timestamps were rejected and counted.
  4. A wrong key, a simulated HTTP 429 with Retry-After, and a network outage each cause capped backoff and a poll-log row with an outcome category. A wrong key also shows up as a separate "auth rejected" state. A search of the logs, error text and database finds no trace of the auth key.
**Plans**: 7/8 plans executed

Plans:
- [x] 01-01-PLAN.md: Tracer. Scaffold (Python 3.13, uv, zero runtime deps), config, rate-guarded client and a live `shelog probe` baseline run from git worktrees via `scripts/live.sh` (wave 1)
- [x] 01-02-PLAN.md: Tracer, continued. End-to-end `shelog poll --once`: schema 0001, parse, dedup, real all_status to SQLite (wave 2)
- [x] 01-03-PLAN.md: Sanitizer, sanitized real fixtures, real-id and configured-key scans over every tracked file, and complete parse/dedup rules (wave 3)
- [x] 01-04-PLAN.md: Resilient poll loop: classifier proven on real sockets, synthetic rate-limit fixture, capped backoff, Retry-After, auth state, single-instance lock (wave 3)
- [x] 01-05-PLAN.md: Secret hygiene and strict config: redacting logs, the `shelog check` leak scan, strict devices.toml, example files (wave 3)
- [x] 01-06-PLAN.md: Probe step matrix (v1/v2, error group, dummy-key wrong host, no bursts) and the sanitized `--save-fixtures` export (wave 4)
- [x] 01-07-PLAN.md: Live probe capture; classifier pinned to real envelopes; docs/shelly-api.md; per-device silence thresholds with provenance; COVERAGE re-validation (wave 5)
- [ ] 01-08-PLAN.md: Phase acceptance: e2e sentinel leak test, dummy-key wrong-key and outage drills, one-hour real run with an aggregate report, CLAUDE.md refresh (wave 6)
**Owner action required**:
  - Provide the Shelly Cloud auth key and server host (Shelly app: User settings > Authorization cloud key) as a local env file only, and list the seven device IDs (including which unit is the H&T Gen3) in a local `devices.toml`.
  - `shelog probe` runs locally with the real credentials (Claude may run it; owner-approved 2026-10-31); parsers and fixtures derive from its real responses. Optional: check reporting settings in the Shelly app. Gen1 wake periods the API doesn't return use the documented default (source `default`) and are tuned from observed report gaps in Phase 2, so no owner step blocks Phase 1.
**Notes**: Needs research first (Shelly v2 response shapes, timestamp semantics, partial-batch failures, whether the auth key can travel outside the URL). Define the full schema (readings, raw_status, outdoor, poll_log, alert_state, meta) on day one so later phases only add to it.

### Phase 2: Collection Live on Bombur
**Goal**: The collector runs unattended in production on Bombur, its data survives redeploys, restarts and reboots, and the owner gets a phone notification whenever collection stops being healthy
**Depends on**: Phase 1
**Requirements**: OPS-01, OPS-02, OPS-03, OPS-04, OPS-05, OPS-06, DATA-05
**Success Criteria** (what must be TRUE):
  1. A push to `main` runs the tests, builds the image, pushes it to GHCR and deploys it to Bombur, and a failing test blocks the deploy. The production poller is storing readings from all seven sensors.
  2. After a redeploy, a container restart and a VPS reboot, the number of stored readings has only grown and the poll log explains each outage window. A consistent daily backup snapshot appears and opens as a valid database.
  3. `https://shelog.remesh.cz/api/health` is served by the `web` service, which has no Shelly key in its environment. It reads the live database while the poller writes and reports last successful poll, silent devices and auth state as coarse status only, with no error text, device IDs or coordinates.
  4. The owner gets an ntfy push when polling has failed for more than an hour, right away when the auth key is rejected, when a device is silent past its own threshold, and when a battery is low, plus a recovery push when each one clears. This is checked by forcing at least the auth-rejected and recovery cases.
  5. `DEPLOYMENT.md` and the README let the owner set up, deploy, add a device, read logs, roll back and restore a backup without reading the code.
**Plans**: TBD
**Owner action required**:
  - Create the public GitHub repo `ciakval/shelog` and push. No remote exists yet.
  - Protect `main`, at least against force-push and deletion. The `production` environment only accepts protected branches.
  - Run `bombur-onboard shelog <port>` from the repo root. `compose.yaml` is owned by the server and changing it later means onboarding again, so it must already list both `poller` and `web`, the `./data` bind mount and the `devices.toml` mount at that point.
  - Fill in the server `.env` with the Shelly key and host and the ntfy topic. Don't use `CHANGE_ME` as the placeholder for the Shelly values, because `bombur-add-app` replaces those with random values. Put the real `devices.toml` on the server and subscribe to the ntfy topic on the phone.
  - Approve the VPS reboot test, since the server is shared.
**Notes**: Deploy before alerts within this phase so collection starts as early as possible. `/api/health` (DATA-05) is the first real job of the `web` service and shows early that it can read the database while the poller writes. Decide here whether to pull forward an external dead-man's switch (ROB-01, v2), because alerts sent by the poller itself can't report that the poller or the VPS is dead.

### Phase 3: Outdoor Weather
**Goal**: Outdoor conditions at the flat's location, including precipitation, soil moisture and soil temperature, are logged next to the sensor data and reach back to the start of the indoor record, without ever putting Shelly collection at risk
**Depends on**: Phase 2
**Requirements**: WX-01, WX-02, WX-03, WX-04
**Success Criteria** (what must be TRUE):
  1. The production database holds hourly outdoor rows for the configured location for every chosen variable: temperature, RH, dew point, precipitation, surface pressure, shortwave radiation, wind speed, cloud cover, and soil moisture and soil temperature at shallow depths.
  2. After the first production run, outdoor data reaches back to the start of the indoor record (up to 92 days) without holes. Recent hours are updated as the model revises them, and no row has a future timestamp.
  3. While Open-Meteo is unreachable (simulated), Shelly polls keep their schedule. Weather failures get their own poll-log category and their own backoff.
  4. The coordinates exist only in the server configuration. No repo file, log line or public endpoint reveals them.
**Plans**: TBD
**Owner action required**:
  - Put the flat's latitude and longitude in the server `.env`.
**Notes**: Needs research first (soil variable names and depths available at these coordinates, a pinned `models=`, where the forecast and historical-forecast APIs join). Not urgent, but it must ship within 92 days of Phase 2 going live. After that, the first days of the indoor record can only be backfilled from the historical-forecast API.

### Phase 4: Metrics & Read API
**Goal**: All collected history can be read back as calibrated, derived and correctly resampled series and statistics through a public JSON API that reveals nothing private
**Depends on**: Phase 3
**Requirements**: DATA-01, DATA-02, DATA-03, DATA-04, DATA-06
**Success Criteria** (what must be TRUE):
  1. Unit tests show dew point, absolute humidity and wood EMC matching pinned reference values at several temperatures (for example EMC 9.25 % at 21 °C/50 %, 9.52 % at 0 °C/50 %, 10.20 % at 40 °C/60 %). The Flood sensor gets no RH-based metrics.
  2. Changing a device's calibration offset in `devices.toml` changes its whole history (T, RH and everything derived from them) on the next API read, with no database rewrite.
  3. For any time range, `GET /api/readings` returns per-device series that start from the value held before the range, are forward-filled at none/5m/15m/1h, and are downsampled with min/max above about 5k points per series so short spikes stay visible.
  4. `GET /api/stats` returns per-device min, max and time-weighted mean of T, RH and EMC, plus last report time and battery. The results match hand-computed values on a fixture with uneven report intervals.
  5. `GET /api/devices` and the outdoor endpoints return display names, groups and outdoor series. A scan of every API response finds no device IDs, MACs, coordinates or auth key.
**Plans**: TBD
**Notes**: Derived values are computed per report first and resampled after, never the other way round (EMC is nonlinear). No owner action is needed.

### Phase 5: Public Web UI
**Goal**: Anyone visiting shelog.remesh.cz can explore the full overlaid climate history of the subfloor, the reference rooms and the outdoors on one fast page, so the owner can judge the drying trend from data
**Depends on**: Phase 4
**Requirements**: UI-01, UI-02, UI-03, UI-04, UI-05, UI-06, UI-07
**Success Criteria** (what must be TRUE):
  1. A visitor can pick 24 h, 3 d, 7 d, 30 d, all or a custom range, then zoom and pan. The temperature, RH, EMC and dew-point charts overlay all selected devices, share one time cursor and show times in Europe/Prague.
  2. A visitor can switch single devices or a whole group (subfloor or reference) on and off. Series are drawn as steps, and poller outages and silences past a device's threshold show as breaks in the line, never as joined or held lines.
  3. Outdoor temperature, RH, absolute humidity, precipitation, soil moisture and soil temperature share the indoor time axis, are labelled as Open-Meteo model data and carry the required attribution.
  4. A visitor sees a stats table for the chosen range and a health indicator with the last poll and any silent devices, plus a banner when the auth key is rejected.
  5. A 7-day chart of all RH-capable devices at full resolution loads in under 2 seconds.
**Plans**: TBD
**UI hint**: yes
**Notes**: uPlot is vendored, with no CDN at runtime. Needs research first (uPlot step alignment, `uPlot.join`, wheel-zoom plugin). No owner action is needed beyond reviewing the page.

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Probe & Core Collector | 7/8 | In Progress | - |
| 2. Collection Live on Bombur | 0/TBD | Not started | - |
| 3. Outdoor Weather | 0/TBD | Not started | - |
| 4. Metrics & Read API | 0/TBD | Not started | - |
| 5. Public Web UI | 0/TBD | Not started | - |
