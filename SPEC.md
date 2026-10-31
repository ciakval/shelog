# shelly-climate-log — Specification & Context

Context document for a Claude Code session. Read fully before writing code.
Sections marked **TBD** must be resolved with the user or verified empirically.

---

## 1. Background

The owner is renovating an apartment and needs to decide on replacing a wooden floor.
In the living room + kitchen the old floor has been removed. Temperature/humidity
sensors lie directly on the exposed subfloor surface, to assess whether it is
drying out and is suitable for a new wooden floor. Two reference rooms still
have wooden floors in different condition.

Shelly Cloud only shows coarse per-device charts. The owner wants:

- **Long, fine-grained history:** several days to weeks, ~5 min resolution.
- **Combined views:** multiple devices overlaid in one chart.
- **Derived metrics** relevant to wood flooring.

Constraints:
- No local infrastructure in the flat (no MQTT broker, no always-on host).
- The devices report to Shelly Cloud only.
- The solution runs on the owner's **VPS** (Linux, public internet).
- The owner is an embedded/systems developer (C, Rust, Python). He values clean, minimal, well-automated solutions.
- **Avoid heavyweight stacks.** No Grafana/Influx unless clearly justified.

## 2. Devices

| # | Location | Placement | Model | Cloud ID | Display name |
|---|----------|-----------|-------|----------|--------------|
| 1 | Living room + kitchen | on subfloor, floor removed | H&T Gen1 (**TBD** which ones) | TBD | LR-1 |
| 2 | Living room + kitchen | on subfloor, floor removed | H&T Gen1 / Gen3 | TBD | LR-2 |
| 3 | Living room + kitchen | on subfloor, floor removed | H&T Gen1 / Gen3 | TBD | LR-3 |
| 4 | Living room + kitchen | on subfloor, floor removed | H&T Gen1 / Gen3 | TBD | LR-4 |
| 5 | Living room + kitchen | on subfloor, centre | **Shelly Flood** (Gen1; reports temp + flood, no RH) | TBD | LR-Flood |
| 6 | Work room | floor "so-so" (reference) | H&T Gen1 / Gen3 | TBD | Work |
| 7 | Bedroom | floor OK (reference) | H&T Gen1 / Gen3 | TBD | Bedroom |

Totals: 5× H&T Gen1, 1× H&T Gen3, 1× Flood (Gen1).

Device inventory lives in a config file, not in code. Example:

```toml
# devices.toml
[[device]]
id = "abc123"
name = "LR-1"
group = "subfloor"      # subfloor | reference
model = "ht_gen1"       # ht_gen1 | ht_gen3 | flood_gen1
t_offset = 0.0          # calibration, see §7
rh_offset = 0.0
```

### Reporting behaviour (important for design)

- All devices are battery-powered and **sleep**. They wake and push state to the cloud only when a reading changes by more than the threshold (set to 0.5 °C / 0.5 %RH). They also wake on their periodic interval.
- **The cloud holds only the last reported state.** A poller sees nothing between polls. If a device reports twice between polls, the intermediate value is lost.
- Consequence: **poll faster than the target resolution.**
- Values are **sample-and-hold**: a reading is valid until the next report. All resampling and plotting must use forward-fill / step semantics, never linear interpolation or bucket means of raw samples.

## 3. Shelly Cloud API

- **Docs:**
  - Cloud Control API: https://shelly-api-docs.shelly.cloud/cloud-control-api/
  - Cloud Control API v2: https://shelly-api-docs.shelly.cloud/cloud-control-api/communication-v2
- **Auth:** `auth_key` + account-specific server host (`shelly-NN-eu.shelly.cloud`). Both are obtained in the Shelly app under User settings → Authorization cloud key.
  - The key changes when the account password changes. Handle auth errors loudly; see §6 health checks.
  - The host can be relocated by Shelly.
- **Rate limit:** 1 request/second per account.
- **Primary endpoint:**
  ```
  POST https://<HOST>/v2/devices/api/get?auth_key=<KEY>
  {"ids": [...≤10 ids...], "select": ["status"]}
  ```
  All 7 devices fit in one request.
- **Fallback endpoint** (v1, per device): `POST https://<HOST>/device/status` with `id` and `auth_key`.
  - Returns `data.device_status`; `_updated` is a cloud-side timestamp of the last report.
- **Status field paths** (**TBD — verify against real responses first**):
  - Gen1 H&T: `tmp.value` / `tmp.tC`, `hum.value`, `bat.value`.
    - `unixtime` may be 0 on sleeping devices.
    - Check for `_updated` or similar cloud timestamps.
  - Gen1 Flood: `tmp.value`, `flood` (bool), `bat.value`.
  - Gen3 H&T: `temperature:0.tC`, `humidity:0.rh`, `devicepower:0.battery.percent`, `sys.unixtime`.
  - v2 response envelope shape: TBD.
- **Possible improvement to investigate:** Shelly's *Real Time Events API* (WebSocket push, OAuth with the account owner's credentials).
  - It would eliminate the lost-intermediate-sample problem.
  - Investigate after the MVP and report feasibility; do not block the MVP on it.
- **Historical backfill (optional, later):** control.shelly.cloud charts are fed by undocumented endpoints.
  - The user may supply a HAR or curl capture from browser devtools.
  - Then write a one-off importer marked `source='cloud_stats'`, separate from polled data.
  - Do not reverse-engineer it without that capture.

## 4. Architecture

Single small Python project, deployed on the VPS.

```
systemd service (poller, long-running loop)
        │  every 60 s: 1 request for all devices
        ▼
   SQLite (WAL)  ◄──── read-only ──── web app (FastAPI or similar, small)
                                         │
                                         ▼
                              single static HTML page + JS charts
                              (uPlot preferred: fast with 100k+ points; Plotly acceptable)
        ▲
   reverse proxy (Caddy) with HTTPS + basic auth
```

- **Language/tooling:** Python ≥3.11, `uv` for env/deps, `httpx`, stdlib `sqlite3`, `tomllib`.
  - Minimal dependencies. Type hints, `ruff`, `pytest`.
- **Poll interval:** default 60 s, configurable.
  - That is ~1440 requests/day, well within limits.
  - Jittered sleep, exponential backoff on errors, honour HTTP 429.
- **Secrets:** env file (`/etc/shelly-climate-log/env`), mode 600. Never logged, never committed.
- **Deployment:** systemd units (service for poller, service for web), an install script or Makefile target. Document it in the README.

## 5. Data model

```sql
-- raw, deduplicated reports: one row per new device report
CREATE TABLE reading (
  device_id   TEXT    NOT NULL,
  ts          INTEGER NOT NULL,  -- unix s; device/cloud report time if available, else first-seen poll time
  ts_source   TEXT    NOT NULL,  -- 'device' | 'cloud' | 'poll'
  t_c         REAL,
  rh          REAL,              -- NULL for Flood
  battery     REAL,
  flood       INTEGER,           -- Flood only
  source      TEXT    NOT NULL DEFAULT 'poll',  -- 'poll' | 'cloud_stats' (backfill)
  PRIMARY KEY (device_id, ts, source)
);

-- every poll attempt, for diagnosing gaps
CREATE TABLE poll_log (
  ts INTEGER NOT NULL, ok INTEGER NOT NULL, http_status INTEGER, error TEXT, n_devices INTEGER
);
```

- **Dedup rule:** insert a new row only if the report timestamp advanced. If no reliable timestamp exists, insert when any value changed versus the last stored row. A device re-reporting identical values is then dropped, which is acceptable.
- Store **raw, uncalibrated** values. Apply calibration offsets at read time.
- Keep the raw poll JSON for the first days (`raw_status` table or a rotating file). This guards against misparsed fields; make it toggleable.

## 6. Frontend requirements

Single page, no build step if feasible (vanilla JS + uPlot from CDN).

1. **Time range:** presets 24 h / 3 d / 7 d / 30 d / all, plus a custom range; zoom and pan on charts.
2. **Charts:**
   - Temperature, RH, wood EMC, dew point.
   - All selected devices overlaid in each chart; shared, synchronized time cursor across charts.
3. **Device selection:** checkboxes, with group toggles (`subfloor` / `reference`).
4. **Step rendering** (sample-and-hold). Gaps longer than a configurable threshold (default 6 h without a report) are drawn as breaks, not lines.
5. **Optional band shading** on the RH chart: target range, default 40–60 %, configurable.
6. **Stats table** for the selected range, per device:
   - min / max / time-weighted mean of T, RH and EMC;
   - % of time outside the RH band;
   - last report time and battery.
7. **Optional subfloor vs reference view:** mean of the subfloor group minus mean of the reference group, for absolute humidity and EMC.
8. **CSV export** of the selected range: raw rows or resampled.
9. **Health indicator:**
   - time of the last successful poll;
   - any device with no report > threshold;
   - auth failure banner.

**API** (JSON):
- `GET /api/devices`
- `GET /api/readings?from=&to=&devices=&resample=` — `resample` = none | 5m | 15m | 1h, forward-fill semantics.
- `GET /api/stats?...`
- `GET /api/health`

Downsample server-side when the point count would exceed ~5k per series. Use step-preserving min/max per bucket so short excursions stay visible.

## 7. Derived metrics

Computed at read time from calibrated T (°C) and RH (%). Put them in a pure, unit-tested module.

- **Dew point:** Magnus formula, a = 17.62, b = 243.12 °C.
- **Absolute humidity** (g/m³): `216.7 * (rh/100 * 6.112 * exp(17.62*t/(243.12+t))) / (273.15 + t)`.
- **Wood equilibrium moisture content (EMC, %):** Hailwood–Horrobin form from the USDA Wood Handbook. T in °F, h = RH/100:
  ```
  W  = 330 + 0.452 T + 0.00415 T²
  K  = 0.791 + 4.63e-4 T − 8.44e-7 T²
  K1 = 6.34 + 7.75e-4 T − 9.35e-5 T²
  K2 = 1.09 + 2.84e-2 T − 9.04e-5 T²
  EMC = 1800/W · [ K h/(1−K h) + (K1 K h + 2 K1 K2 K² h²) / (1 + K1 K h + K1 K2 K² h²) ]
  ```
  Sanity tests: ~21 °C / 50 % → ≈ 9.2 %; ~21 °C / 65 % → ≈ 11.9 %. Verify against Wood Handbook tables.
- **Calibration:** per-device `t_offset` / `rh_offset` from config.
  - Planned procedure: the user co-locates all sensors for 24 h.
  - Provide a small CLI that computes offsets against the median sensor over a given time window: `shelly-climate-log calibrate --from --to`.

## 8. CLI

```
shelly-climate-log poll [--once]              # loop or single poll
shelly-climate-log probe                      # dump raw status JSON per device (step 1 of development)
shelly-climate-log serve                      # web app
shelly-climate-log export --from --to [--resample 5m] > out.csv
shelly-climate-log calibrate --from --to
```

## 9. Development plan

1. **Probe first.** Implement `probe`, run it against real devices (user supplies env), commit sanitized responses as test fixtures.
   - Fix the field paths in §3 from real data before writing parsers.
   - Specifically determine, per model, which timestamp marks the actual report time.
2. **Poller + DB:**
   - parser per model;
   - dedup;
   - poll_log;
   - backoff;
   - tests against fixtures.
3. **Deploy the poller early.** Data collection is time-critical; every day not logged is lost. Get the poller running on the VPS before building the UI.
4. API + derived metrics (tests for formulas).
5. Frontend.
6. Health checks; optional push alert (e.g. ntfy.sh or e-mail) when polling fails for more than 1 h or the auth key is rejected.
7. Optional: Real Time Events API investigation; cloud-stats backfill importer; calibration CLI.

## 10. Acceptance criteria

- The poller runs unattended under systemd, survives VPS reboot and network or cloud outages, and never exceeds 1 req/s.
- No duplicate rows; poll_log explains every gap.
- A chart of all 6 RH-capable devices over 7 days at full resolution loads in under 2 s.
- Derived-metric unit tests pass.
- The README covers setup, env vars, deployment, and how to add a device.
- Secrets are never in the repo or the logs.

## 11. Open questions (ask the user)

- Which device ID is the Gen3? Names and IDs for the table in §2.
- VPS: distribution, and is Caddy/nginx already running? Domain or subdomain for the frontend?
- Is an alerting channel wanted (ntfy, e-mail)?
- Is there historical data the owner wants backfilled from the cloud (requires a devtools capture)?
