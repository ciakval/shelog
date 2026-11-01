# Requirements: shelog

**Defined:** 2026-10-31
**Core Value:** Collect a complete, trustworthy, fine-grained (~5 min) climate history from every sensor, starting as soon as possible.

## v1 Requirements

### Probe & Discovery

- [ ] **PROBE-01**: Owner can run `shelog probe` with their auth key/host and get the raw status JSON for every configured device
- [ ] **PROBE-02**: Sanitized probe responses (no device IDs, MACs, IPs, SSIDs, keys) are committed as test fixtures for each model (H&T Gen1, H&T Gen3, Flood Gen1), and a test fails if a fixture contains a real identifier
- [ ] **PROBE-03**: For each model, the field that marks the actual report time is identified and documented (`ts_source` decision), including timezone semantics
- [ ] **PROBE-04**: Error behaviour is captured and documented: wrong key, wrong host, unknown device id in a batch, rate-limit response, sleeping/offline device
- [ ] **PROBE-05**: Each device's reporting settings (wake period, thresholds, any on-device offset) are recorded so silence thresholds can be set per device

### Collection

- [x] **COLL-01**: Poller fetches all devices in one Shelly Cloud request per interval (default 60 s, jittered, configurable) and never exceeds 1 request/second
- [ ] **COLL-02**: Poller backs off exponentially on errors (capped), honours HTTP 429 / Retry-After, and treats a rejected auth key as a distinct state
- [ ] **COLL-03**: A new reading row is stored only when a device's report timestamp advanced (or values changed when no reliable timestamp exists); implausible timestamps are rejected and counted, never silently dropped
- [x] **COLL-04**: Readings are stored raw and uncalibrated (T, RH, battery, flood flag) with the source of their timestamp; sensor-error nulls are never coerced to 0
- [x] **COLL-05**: Every poll attempt (Shelly and weather) is logged with outcome category, HTTP status and device count, so every gap in data is explainable
- [x] **COLL-06**: The raw status JSON is retained alongside each stored reading so readings can be re-derived later
- [ ] **COLL-07**: The Shelly auth key never appears in logs, error text, the database, or any public output
- [x] **COLL-08**: Device inventory (id, name, group, model, offsets, silence threshold) is read from a server-side `devices.toml`; only an example file is in the public repo

### Outdoor Weather

- [ ] **WX-01**: Poller fetches hourly outdoor data from Open-Meteo for configured coordinates: temperature, RH, dew point, precipitation, surface pressure, shortwave radiation, wind speed, cloud cover, soil moisture and soil temperature (shallow layers)
- [ ] **WX-02**: On first run the outdoor series is backfilled as far as the API allows (up to 92 days) and recent hours are updated as the model revises them (upsert, no future rows)
- [ ] **WX-03**: A weather API outage never delays or breaks Shelly polling
- [ ] **WX-04**: Coordinates are configured on the server and never shown publicly

### Operations

- [ ] **OPS-01**: App runs on Bombur as a Docker Compose project (`poller` + `web` services from one image) deployed by the standard GitHub Actions → GHCR → bombur-deploy pipeline on push to `main`, with tests gating the build
- [ ] **OPS-02**: Data survives redeploys, container restarts and VPS reboots (database inside the bind-mounted `./data`; verified by a redeploy test)
- [ ] **OPS-03**: The read-only web service never receives the Shelly key and can read the database while the poller writes
- [ ] **OPS-04**: A consistent database backup snapshot is taken daily
- [ ] **OPS-05**: Owner receives an ntfy push when polling has failed for > 1 h, immediately when the auth key is rejected, when a device is silent beyond its own threshold, or when a battery is low — and a recovery message when each clears
- [ ] **OPS-06**: `DEPLOYMENT.md` / README cover setup, env vars, deployment, how to add a device, and day-2 operations

### Metrics & API

- [ ] **DATA-01**: Dew point, absolute humidity and wood EMC (Hailwood–Horrobin) are computed at read time from calibrated T/RH, in a unit-tested pure module with tight reference values at several temperatures
- [ ] **DATA-02**: Per-device calibration offsets from config are applied at read time to all history
- [ ] **DATA-03**: `GET /api/readings` returns per-device series for a time range with forward-fill resampling (none / 5m / 15m / 1h) and step-preserving min/max downsampling above ~5k points per series
- [ ] **DATA-04**: `GET /api/stats` returns per device for a range: min / max / time-weighted mean of T, RH, EMC, plus last report time and battery
- [ ] **DATA-05**: `GET /api/health` returns last successful poll, silent devices and auth state, with coarse status only (no error text)
- [ ] **DATA-06**: `GET /api/devices` and outdoor series endpoints expose display names, groups and outdoor data without device IDs or coordinates

### Web UI

- [ ] **UI-01**: Visitor can pick a time range (24 h / 3 d / 7 d / 30 d / all / custom) and zoom and pan the charts
- [ ] **UI-02**: Visitor sees temperature, RH, EMC and dew-point charts with all selected devices overlaid and a synchronized time cursor
- [ ] **UI-03**: Visitor can select devices individually or by group (subfloor / reference)
- [ ] **UI-04**: Series are drawn as steps (sample-and-hold); poller outages and over-threshold silences are drawn as breaks
- [ ] **UI-05**: Visitor sees a stats table for the selected range and a health indicator (last poll, silent devices, auth failure banner)
- [ ] **UI-06**: Outdoor series (temperature, RH, absolute humidity, precipitation, soil moisture/temperature) are shown on the same time axis, labelled as Open-Meteo model data, with the required attribution
- [ ] **UI-07**: A chart of all RH-capable devices over 7 days at full resolution loads in under 2 s; times are shown in Europe/Prague

## v2 Requirements

### Data tools

- **TOOL-01**: CSV export of a range, raw or resampled
- **TOOL-02**: Calibration CLI computing offsets against the median sensor over a co-location window
- **TOOL-03**: `reparse` command re-deriving readings from stored raw JSON

### Analysis views

- **VIEW-01**: Subfloor-minus-reference delta for absolute humidity and EMC
- **VIEW-02**: Indoor-minus-outdoor absolute humidity
- **VIEW-03**: RH band shading with % of time outside band
- **VIEW-04**: Event annotations (CLI/config) shown on charts
- **VIEW-05**: Drying-trend slopes and data-quality overlay

### Robustness

- **ROB-01**: External dead-man's switch alerting when the poller or VPS itself is down
- **ROB-02**: Shelly Real Time Events API (WebSocket) to avoid lost intermediate reports
- **ROB-03**: Cloud-stats historical backfill importer
- **ROB-04**: OpenTelemetry export once a collector exists on Bombur

## Out of Scope

| Feature | Reason |
|---------|--------|
| "Ready for flooring" verdict / traffic light | Sensors measure subfloor-surface air, not material moisture; owner wants data, analysis later |
| Grafana / InfluxDB / MQTT / local hardware | Heavyweight; no always-on host in the flat |
| User accounts / authentication | UI is intentionally public read-only |
| Device control / settings changes | Logging only |
| Smoothing, interpolation, bucket means of raw samples | Wrong for sample-and-hold data |
| Storing derived or calibrated values | Derive at read time so formulas/offsets can change |
| Alerts on climate thresholds (e.g. RH out of band) | Alert fatigue; alerts cover collector health only |
| ML / drying forecasts | Speculative; not needed for the decision |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| PROBE-01 | Phase 1 | Pending |
| PROBE-02 | Phase 1 | Pending |
| PROBE-03 | Phase 1 | Pending |
| PROBE-04 | Phase 1 | Pending |
| PROBE-05 | Phase 1 | Pending |
| COLL-01 | Phase 1 | Complete |
| COLL-02 | Phase 1 | Pending |
| COLL-03 | Phase 1 | Pending |
| COLL-04 | Phase 1 | Complete |
| COLL-05 | Phase 1 | Complete |
| COLL-06 | Phase 1 | Complete |
| COLL-07 | Phase 1 | Pending |
| COLL-08 | Phase 1 | Complete |
| WX-01 | Phase 3 | Pending |
| WX-02 | Phase 3 | Pending |
| WX-03 | Phase 3 | Pending |
| WX-04 | Phase 3 | Pending |
| OPS-01 | Phase 2 | Pending |
| OPS-02 | Phase 2 | Pending |
| OPS-03 | Phase 2 | Pending |
| OPS-04 | Phase 2 | Pending |
| OPS-05 | Phase 2 | Pending |
| OPS-06 | Phase 2 | Pending |
| DATA-01 | Phase 4 | Pending |
| DATA-02 | Phase 4 | Pending |
| DATA-03 | Phase 4 | Pending |
| DATA-04 | Phase 4 | Pending |
| DATA-05 | Phase 2 | Pending |
| DATA-06 | Phase 4 | Pending |
| UI-01 | Phase 5 | Pending |
| UI-02 | Phase 5 | Pending |
| UI-03 | Phase 5 | Pending |
| UI-04 | Phase 5 | Pending |
| UI-05 | Phase 5 | Pending |
| UI-06 | Phase 5 | Pending |
| UI-07 | Phase 5 | Pending |

**Coverage:**
- v1 requirements: 36 total
- Mapped to phases: 36
- Unmapped: 0 ✓

---
*Requirements defined: 2026-10-31*
*Last updated: 2026-10-31 after roadmap creation (traceability filled)*
