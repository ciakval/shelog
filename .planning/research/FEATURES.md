# Feature Research

**Domain:** Self-hosted indoor-climate / building-moisture logger (Shelly Cloud sensors on a drying subfloor, reference rooms, outdoor weather) used to judge subfloor readiness for a wooden floor
**Researched:** 2026-10-31
**Confidence:** MEDIUM (feature landscape from product knowledge: HIGH. Flooring thresholds: MEDIUM, from trade sources and forum summaries that must be checked against the floor manufacturer's sheet. Open-Meteo variable names: HIGH, from official docs.)

## Framing: what the sensors can and cannot say

Flooring standards and installers judge **material moisture**, not air climate:

| What installers/standards check | Method | Typical threshold | Source confidence |
|---|---|---|---|
| Cement screed residual moisture | CM (calcium carbide) test | <= 2.0 CM-% unheated, <= 1.8 CM-% heated (DE parquet industry, vdp 2022) | MEDIUM |
| Anhydrite / calcium sulfate screed | CM test | <= 0.5 CM-% unheated, <= 0.3 CM-% heated (sources conflict) | LOW-MEDIUM |
| Concrete slab (US) | In-situ RH probes, ASTM F2170 (3 probes for first 1000 sq ft) | <= 75 % RH in slab (or manufacturer value); CaCl test <= 3 lb | MEDIUM |
| Wood subfloor (US) | Pin/pinless wood MC meter | <= 12 % MC, within about 3-4 % of the flooring MC | MEDIUM (NWFA's exact figure not confirmed) |
| Jobsite air | Thermo-hygrometer, recorded at delivery and install | 30-50 % RH (NWFA) or 35-55 %, 60-80 F (about 16-27 C); many EU manufacturers say 45-65 % RH, 18-24 C | MEDIUM |
| Flooring material | Wood MC meter | typically 6-9 % MC; acclimate until stable | MEDIUM |
| Acclimation | Time/stability based, not a fixed number | Days to weeks; "until MC stops changing". Modern guidance says condition the **space** (HVAC running) rather than the boards | MEDIUM |

Consequence for shelog: **the sensors lie in the air layer on the subfloor surface. They give a proxy, not the legal "ready" measure** (CM or in-slab RH). The tool's job is to show the trend (is it drying, has it plateaued, is the subfloor air converging to the reference rooms and to what outdoor air would give?) and to document ambient conditions. The owner's "data first, verdict later" stance fits this exactly. The final decision will still need a CM / probe / pin-meter measurement; the UI should not pretend to replace it (see anti-features).

Construction-drying practice (IICRC S500-style restoration logs) adds three useful ideas: use **absolute humidity** (grains per pound or g/m3) rather than RH to judge whether air is drying (RH swings with temperature), use an **unaffected "dry standard"** as the reference (here: the reference rooms with wooden floors), and **record readings at the same points on a regular cadence until the goal is met**. All three map directly onto the existing design.

## Feature Landscape

### Table Stakes (Users Expect These)

Missing these means the tool is useless or untrustworthy. Ordered roughly by criticality to the Core Value (complete, trustworthy, fine-grained history, collected as early as possible).

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Unattended continuous poller (all devices in one request, <= 1 req/s, backoff, 429 handling) | Every logger. Gaps are unrecoverable | MEDIUM | Needs probe step first; cloud only has last state, so poll faster than target resolution |
| Raw, uncalibrated storage with dedup and report-time source | Trustworthy history; lets formulas/offsets change later | MEDIUM | Sample-and-hold semantics; store ts_source |
| Poll attempt log (every attempt, ok/error/http status) | Makes gaps explainable (outage vs sleeping device) | LOW | Cheap; do in the first deploy |
| Raw JSON retention (toggleable, rotating) | Guards against misparsed fields | LOW | Becomes the safety net if parsers change |
| Persistence across reboot / container restart | Basic expectation | LOW | Bind-mounted `./data`, restart policy |
| Early production deploy (poller before UI) | Time-critical data | MEDIUM | Bombur pipeline; sequencing constraint, not a feature |
| Outdoor weather logging with backfill (Open-Meteo) | Indoor drying cannot be interpreted without outdoor T/RH; owner explicitly wants backfill for the logged period | MEDIUM | See outdoor variable table below. Store hourly raw variables; keep fetch decoupled from the sensor poller |
| Health/staleness visibility (last successful poll, per-device last report, battery) | Every home sensor app shows last-seen and battery | LOW | Battery is the leading cause of silent gaps for H&T devices |
| Alerts on failure (ntfy: poll failing > 1 h, auth rejected, sensor silent) | Unattended collector needs a "tell me when it breaks" channel | LOW | Dedupe/rate-limit alerts; one message per incident plus a recovery message |
| Time-series chart with range presets and zoom/pan (24 h / 3 d / 7 d / 30 d / all) | Table stakes in Home Assistant history, Shelly app, SensorPush/Govee | MEDIUM | uPlot; server-side downsampling above about 5k points/series |
| Multi-device overlay with synchronized cursor | The stated reason Shelly Cloud charts are insufficient | MEDIUM | Device/group selection with subfloor/reference toggles |
| Correct step rendering with gap breaks | Sample-and-hold data drawn as interpolated lines is misleading | LOW-MEDIUM | Gap threshold configurable (default 6 h) |
| Derived metrics: dew point, absolute humidity, wood EMC | Core of "wood flooring metrics"; every moisture-focused tool shows dew point and an absolute measure | LOW-MEDIUM | Pure, unit-tested module; EMC sanity: 21 C/50 % about 9.2 %, 21 C/65 % about 11.9 % |
| Stats table per range (min / max / time-weighted mean of T, RH, EMC; last report; battery) | Sensor apps show min/max/avg; time-weighting is required by sample-and-hold | MEDIUM | Time-weighted, not sample-weighted |
| JSON API (devices, readings with forward-fill resample, stats, health) | Backbone for UI and any later analysis | MEDIUM | Same API serves CSV |
| CSV export (raw or resampled) | SensorPush, Govee, ThermoBeacon, HA all export; and "analysis later" depends on it | LOW | Include both raw and calibrated columns and the calibration offsets used |
| Secrets hygiene (auth key never in repo, logs, or public API) | Public UI makes this a hard constraint | LOW | Redact in logs, health endpoint exposes no host or key |

### Differentiators (Competitive Advantage)

Features that fit this specific problem. Not expected from generic loggers, but they directly serve the flooring decision. Roughly ordered by value per cost.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Outdoor series overlaid on indoor charts (T, RH, dew point, absolute humidity) | Shows whether subfloor humidity follows the weather (ventilation / uncontrolled) or is driven by the slab itself. Generic apps do not have this | LOW-MEDIUM | Needs outdoor in the same API/series model; derive abs. humidity for outdoor the same way as indoor |
| Subfloor-vs-reference delta view (mean of subfloor group minus reference group, in absolute humidity and EMC) | The closest thing to the restoration "dry standard" method: subfloor is dry when it converges to the reference | LOW-MEDIUM | Needs group definitions; handle missing members and Flood (T only) |
| Indoor-minus-outdoor absolute humidity | A positive excess over outdoor means a moisture source (the slab); a drop towards zero means the source has dried. Removes temperature confounding better than RH | LOW | Pure derived series; needs outdoor data to be aligned (hourly step vs 5 min step, forward-fill) |
| RH target band shading and "% of time outside band" | Flooring guidance is expressed as a RH band; gives a single scannable number per device | LOW | Configurable (default 40-60 %); keep the band as display, not a verdict |
| Wood EMC as first-class chart | Translates air RH+T into the number installers actually think in (wood MC %). Govee/SensorPush do not show it | LOW once derived module exists | Hailwood-Horrobin; label as equilibrium, not measured MC |
| Drying-trend summary (e.g. daily mean absolute humidity / EMC per group, slope over last N days) | Makes "is it still dropping or plateaued" visible without reading charts. Plateau detection is what drying monitors report | MEDIUM | Observation only. Do after enough data exists. Display slopes, do not threshold into a verdict |
| Per-device calibration offsets + calibration CLI (offsets vs median over co-location window) | Cheap hygrometers differ by several %RH; with 7 sensors compared against each other, offsets matter more than absolute accuracy | MEDIUM | Offsets in config, applied at read time; raw stays raw. Fixed 24 h co-location by owner |
| Event annotations (floor removed, heater on, window open, screed poured, dehumidifier on) | Interpreting a drying curve needs context. Restoration logs are mostly annotations | LOW-MEDIUM | Simple timestamp+text table, shown as chart markers. Public write is a problem: write via CLI or config file only (no auth in UI) |
| Data-quality display (sampling density, coverage % per device, poll-log outages shaded) | Tells the viewer whether a flat line is real or a gap. Backs the "trustworthy" claim | MEDIUM | Reuse poll_log; distinguish "device quiet (no change)" from "poller down" |
| Flood sensor as extra signal (flood flag + T on subfloor centre) | Surface-water indicator on a wet subfloor | LOW | Show as state markers; it is binary and rarely changes |
| Real Time Events (WebSocket) push to capture intermediate samples | Removes lost-intermediate-sample problem of last-state polling | HIGH | Investigate after MVP (already planned); not a day-one feature |
| Cloud-stats historical backfill importer (`source='cloud_stats'`) | Recovers data from before shelog existed | MEDIUM | Blocked on owner supplying a HAR/curl capture; keep separate from polled data |

### Anti-Features (Commonly Requested, Often Problematic)

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Automated "ready for flooring" verdict / traffic light | The whole purpose, so it seems natural | Air sensors cannot measure screed/slab moisture; thresholds (CM-%, slab RH) are measured by other methods and vary by manufacturer and substrate. A false green could cost a floor. Owner explicitly deferred analysis | Show trends, deltas and EMC; let the owner decide. Optionally let them record CM / pin-meter readings as annotations later |
| Grafana / InfluxDB / MQTT / TSDB stack | "Standard" for home sensor logging | Heavyweight, nothing always-on in the flat, contradicts footprint constraint. SQLite handles about 7 devices x 288 points/day trivially | SQLite WAL + uPlot |
| User accounts / login / admin UI | Needed if the UI could write | Public read-only by decision; adds attack surface and secrets | Config in files/CLI; annotations and calibration via CLI |
| Device control (relays, thresholds, firmware, reconfiguring report interval) | Shelly can do it | Logging only; writes through the cloud API raise risk and rate-limit load | Out of scope |
| Smoothing / interpolation / bucket means of raw samples | Prettier charts | Sample-and-hold means interpolation invents data and hides short excursions | Forward-fill step charts; min/max per bucket when downsampling |
| Storing calibrated or derived values in the DB | Faster reads | Locks in today's formula and offsets; calibration will be revised after co-location | Raw in DB, calibrate and derive at read time (cheap at this scale) |
| Predictions / ML drying-time forecast | Seems like the "smart" feature | Few weeks of data, one subfloor, confounded by ventilation and heating; unvalidated models give false precision | Plain slope/plateau displays after data exists |
| Fast sub-minute polling to catch intermediates | More resolution | Hits the 1 req/s account limit, devices only push on 0.5 C / 0.5 %RH change or wake anyway, nothing new to fetch | 60 s polling; investigate WebSocket instead |
| Push notifications for every threshold crossing (RH out of band etc.) | Consumer apps all do it | Floods the phone during a drying phase where RH is meant to move; alert fatigue hides the real "collector is broken" alerts | Alerts only for collector health (poll fail, auth, silent sensor, low battery) |
| Multi-user / multi-site / generic device framework | Reusability | One flat, seven devices; abstraction cost with no use | Device inventory in a config file, models limited to the three known ones |
| Long-term retention/rollup policies, compaction | Habit from TSDB systems | Data volume is tiny (about 2k raw rows/day); history is the asset | Keep everything raw; downsample at read time only |
| Soil moisture / ET0 / UV / visibility from Open-Meteo | "Free, so store everything" | Irrelevant to a flat interior; adds noise and API weight | Store the curated set below only |

## Outdoor Weather Variables (Open-Meteo)

Open-Meteo hourly variables confirmed in the official docs: `temperature_2m`, `relative_humidity_2m`, `dew_point_2m`, `pressure_msl`, `surface_pressure`, `precipitation`, `rain`, `snowfall`, `cloud_cover`, `shortwave_radiation` (plus direct/diffuse), `wind_speed_10m`, `wind_direction_10m`, `wind_gusts_10m`, `vapour_pressure_deficit`, `weather_code`, soil temperature/moisture by depth. The historical (ERA5) API is hourly at 0.25 degrees (about 25 km), updates daily with about a 5-day delay, and does not offer wind gusts or snow depth. For the most recent days use the forecast API (it supports `past_days`) and reconcile later. MEDIUM confidence on exact `past_days` limits; verify in the architecture/stack phase.

| Variable | Role in interpreting indoor / subfloor drying | Recommendation |
|---|---|---|
| `temperature_2m` | Drives ventilation exchange, building temperature, and the absolute-humidity calculation | **Store (must)** |
| `relative_humidity_2m` | With T gives outdoor absolute humidity; a natural overlay on indoor RH | **Store (must)** |
| `dew_point_2m` | Provided by Open-Meteo; the direct comparator for indoor dew point and a condensation check. Cross-check with the app's own Magnus calculation from T/RH | **Store (must)** |
| Absolute humidity | The key comparator: indoor minus outdoor absolute humidity shows moisture sources versus ventilation. Not an Open-Meteo field | **Derive at read time** from T and RH with the same function used for indoor (one formula for both) |
| `precipitation` (rain + snowfall) | Rainy/humid spells and wet-air intrusion through windows; explains indoor bumps that follow outdoor weather. Also relevant if the building envelope or roof leaks | **Store (should)** |
| `shortwave_radiation` | Solar heating of the flat (south-facing windows) drives indoor T swings and therefore RH swings (RH falls as air warms); explains daily cycles | **Store (should)** |
| `wind_speed_10m` | Proxy for infiltration/ventilation when windows are cracked or the envelope is open | **Store (should)** |
| `cloud_cover` | Cheap context for radiation and nighttime cooling | Store (nice) |
| `surface_pressure` | Barometric pumping of a slab is a second-order effect at best; useful as a sanity check for the model and for converting humidity precisely | Store (cheap, low priority). Use `surface_pressure`, not `pressure_msl`, for altitude-correct vapour calculations |
| `wind_direction_10m`, `wind_gusts_10m` | Little interpretive value for interior drying; gusts are missing in ERA5 anyway | Skip |
| `vapour_pressure_deficit`, `apparent_temperature`, `wet_bulb_temperature_2m` | Redundant with T/RH/dew point | Skip (derivable if ever needed) |
| Soil temperature / soil moisture | Relevant for ground-contact slabs and basements; not for a flat above ground | Skip. Revisit only if the flat turns out to be ground floor on a slab |
| `weather_code`, `is_day` | Display convenience only | Skip, derivable |

Storage guidance: store Open-Meteo values as returned, hourly, with the model/source and a fetched-at stamp (forecast values get revised, ERA5 reanalysis replaces them). Outdoor is model output at about 25 km grid resolution (or the forecast model grid), not a measurement on the facade; label it so in the UI, and note the owner may later prefer a nearby station.

## Feature Dependencies

```
Probe (real response shapes per model)
    └──requires──> Parsers + fixtures
                       └──requires──> Poller + dedup storage + poll_log
                                          └──requires──> Production deploy (Bombur)
                                                             └──enables──> Alerts (ntfy)

Open-Meteo fetch + backfill ──requires──> Storage schema (outdoor table), lat/lon config
    (independent of the Shelly poller; can ship in same deploy or one phase later)

Raw storage ──requires──> Derived-metrics module (dew point, abs. humidity, EMC) [read-time]
Derived-metrics module ──requires──> Calibration offsets (config) ──enhanced-by──> Calibration CLI

JSON API ──requires──> Raw storage + Derived metrics + Forward-fill resampler
    ├──requires──> Web UI (charts, stats, health)
    └──requires──> CSV export

Outdoor overlay ──requires──> Outdoor storage + Derived metrics + API series model for outdoor
Subfloor-vs-reference delta ──requires──> Device groups in config + Derived metrics + Time-weighted stats
Indoor-minus-outdoor abs. humidity ──requires──> Outdoor overlay + forward-fill alignment
Drying-trend summary ──requires──> Delta view + enough accumulated data
Data-quality display ──requires──> poll_log + per-device last-report

Event annotations ──enhances──> Charts (no dependency on the poller)
Real Time Events ──enhances──> Poller (replaces polling gaps; conflicts with running 2 writers unless dedup is by report ts)
Cloud-stats backfill ──requires──> owner-supplied HAR/curl capture; must remain a separate `source`
```

### Dependency Notes

- **Probe gates everything:** field paths and which timestamp marks a real report differ per model (Gen1 vs Gen3 vs Flood). Parsers and dedup cannot be written correctly before this.
- **Poller and deploy come before UI:** nothing in the UI is needed to protect the Core Value; every UI feature depends only on the data schema being stable, so freeze the reading schema early. Raw storage plus read-time derivation is what makes later schema changes cheap.
- **Outdoor backfill is time-sensitive in a different way:** Open-Meteo history is retrievable later at no loss (ERA5 keeps it), so outdoor can follow the sensor poller by a few days. Sensor data cannot. That makes outdoor medium priority for ordering but required for MVP value.
- **Derived metrics before the stats and the delta views:** stats, band percentages, and delta all consume calibrated, derived series.
- **Calibration offsets default to 0:** the system works without the CLI; the CLI only improves the numbers, so it can come late. Because raw is stored, late calibration changes history retroactively without data loss.
- **Alerts are cheap but need health state:** poll_log and per-device last-report must exist first.
- **Real Time Events conflicts with polling only if dedup is weak:** dedup by device report timestamp keeps both sources safe.

## MVP Definition

Note: here "MVP" means the earliest useful increments, in the order the Core Value demands.

### Launch With (v1: data collection live)

- [ ] Probe + sanitized fixtures + per-model parsers: field paths are unverified
- [ ] Poller with dedup, raw storage, poll_log, raw JSON retention, backoff/429: Core Value
- [ ] Production deploy on Bombur: every day not logged is lost
- [ ] ntfy alerts for poll failure, auth rejection, silent sensor: unattended operation
- [ ] Open-Meteo outdoor logging with backfill (T, RH, dew point, precipitation, shortwave radiation, wind speed, surface pressure, cloud cover): needed to interpret everything else

### Add After Validation (v1.x: make the data readable)

- [ ] JSON API with forward-fill resampling, health, CSV export: first step toward "analysis later", usable before any UI
- [ ] Derived-metrics module (dew point, absolute humidity, EMC) with unit tests
- [ ] Single-page UI: presets, zoom/pan, overlay, step rendering with gap breaks, device/group selection, health indicator
- [ ] Outdoor overlay and subfloor-vs-reference delta
- [ ] Stats table with time-weighted values and % outside RH band
- [ ] Calibration CLI after the 24 h co-location

### Future Consideration (v2+)

- [ ] Event annotations: after the first drying curve exists and ambiguity appears
- [ ] Drying-trend summary (slopes, plateau display): needs weeks of data
- [ ] Data-quality overlay (coverage %, outage shading)
- [ ] Real Time Events investigation
- [ ] Cloud-stats backfill importer (needs HAR capture)
- [ ] Manual entry of CM / pin-meter / probe readings as annotated reference points (owner decides; never as an automated verdict)

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| Probe + parsers | HIGH | LOW | P1 |
| Poller + dedup + poll_log + raw JSON | HIGH | MEDIUM | P1 |
| Bombur deploy | HIGH | MEDIUM | P1 |
| ntfy alerts (collector health) | HIGH | LOW | P1 |
| Open-Meteo outdoor + backfill | HIGH | MEDIUM | P1 |
| JSON API + CSV export | HIGH | MEDIUM | P1 |
| Derived metrics module | HIGH | LOW | P1 |
| Overlay charts, presets, zoom/pan, step + gaps | HIGH | MEDIUM | P1 |
| Health indicator | MEDIUM | LOW | P1 |
| Stats table (time-weighted) | MEDIUM | MEDIUM | P2 |
| Outdoor overlay, indoor-minus-outdoor abs. humidity | HIGH | LOW-MEDIUM | P2 |
| Subfloor-vs-reference delta | HIGH | LOW-MEDIUM | P2 |
| RH band shading + % outside | MEDIUM | LOW | P2 |
| Calibration offsets + CLI | MEDIUM | MEDIUM | P2 |
| Event annotations | MEDIUM | LOW-MEDIUM | P3 |
| Drying-trend summary | MEDIUM | MEDIUM | P3 |
| Data-quality overlay | MEDIUM | MEDIUM | P3 |
| Real Time Events push | MEDIUM | HIGH | P3 |
| Cloud-stats backfill | MEDIUM | MEDIUM | P3 (blocked on HAR) |

**Priority key:**
- P1: Must have for launch of collection and first readable output
- P2: Should have, add when the data is flowing
- P3: Nice to have, future consideration

## Competitor Feature Analysis

| Feature | Home Assistant history / HA + Influx | Shelly app / Cloud charts | SensorPush / Govee / ThermoBeacon apps | Restoration drying monitors (IICRC-style logs) | Our Approach |
|---------|------|------|------|------|------|
| History resolution | Full state-change history, native retention | Coarse, per-device | Fixed 1-5 min logs (BLE/gateway), cloud-stored | Daily manual readings | About 5 min (60 s polls, dedup on report) |
| Multi-device overlay | Yes (history graph, statistics graph) | No | Yes in some apps | Table per point per day | Yes, with synchronized cursor |
| Derived metrics | Template sensors (dew point, abs. humidity) | No | Dew point, VPD, abs. humidity (SensorPush) | GPP, dew point, "dry standard" comparison | Dew point, abs. humidity, wood EMC at read time |
| Outdoor reference | Via weather integration, manual overlay | No | Limited | Outside readings logged on each visit | Open-Meteo stored alongside, overlaid |
| Thresholds / alerts | Automations, very flexible | Basic notifications | Out-of-range alerts | Goal-driven (reach dry standard) | Collector-health alerts only |
| Export | CSV / DB queries | Limited | CSV | PDF/log reports | CSV raw or resampled, JSON API |
| Calibration | Template offsets | Device-level offset in the Shelly app | App-level offsets | Meter calibration | Per-device offsets applied at read time |
| Annotations | Logbook entries | No | Rare | Core of the documentation | Deferred, CLI-only |
| Needs local infrastructure | Yes (always-on host) | No | Gateway or phone nearby | No | No (VPS only, cloud polling) |
| Verdict/readiness | No | No | No | Yes, against moisture-meter goals | **No, deliberately** |

## Sources

- Open-Meteo forecast API docs, variable list: https://open-meteo.com/en/docs (HIGH)
- Open-Meteo historical weather API (ERA5, 0.25 degrees, about 5-day delay): https://open-meteo.com/en/docs/historical-weather-api (HIGH)
- vdp / baulinks, 2022 consensus on screed moisture limits (cement: 1.8 CM-% heated, 2.0 CM-% unheated): https://www.baulinks.de/webplugin/2022/0996.php4 (MEDIUM)
- Screed CM limits for calcium sulfate screed and forum discussion (conflicting, use manufacturer sheet): https://bau.de/forum/estrich-bodenbelaege/14746.php (LOW)
- Wagner Meters, acceptable concrete moisture levels (ASTM F2170, 75 % RH): https://wagnermeters.com/concrete-moisture-test/concrete-info/acceptable-concrete-moisture (MEDIUM)
- Hardwood Floors magazine, subfloor moisture and vapor retarders: https://hardwoodfloorsmag.com/subfloor-focus-minimizing-moisture-part-3-vapor-barriers-pads-floating-floors/ (MEDIUM)
- BuildDirect, wood subfloor <= 12 % MC and <= 3 % difference: https://www.builddirect.com/blogs/pro-flooring-installers/acceptable-moisture-levels-before-installing-luxury-vinyl-plank-flooring (LOW-MEDIUM; NWFA's exact "within 4 %" figure was not confirmed)
- NWFA acclimation and jobsite conditions: https://hardwoodfloorsmag.com/2023/10/09/acclimation-environmental-conditions-before-during-and-after-the-installation/ and https://wagnermeters.com/acclimation (MEDIUM; RH range cited as 30-50 % or 35-55 %)
- Restoration drying logs and psychrometrics (GPP, dry standard, daily records; IICRC S500 quoted second-hand): https://www.accuserve.com/blog/why-ongoing-monitoring-recording-of-readings-should-occur-daily (LOW-MEDIUM)
- Wood Handbook Hailwood-Horrobin EMC form (from SPEC.md; verify against Wood Handbook tables in the metrics phase)
- Product feature knowledge of Home Assistant, Shelly Cloud, SensorPush, Govee, ThermoBeacon (training knowledge, not re-verified this session; MEDIUM)

Caveat: all numeric flooring thresholds above are indicative. The owner's chosen floor manufacturer's installation sheet and local standard (for example the Czech ČSN or DIN 18560 for screed) take precedence and should be looked up before any decision is encoded into the UI (for example the RH band default).

---
*Feature research for: self-hosted building-moisture / climate logger (shelog)*
*Researched: 2026-10-31*
