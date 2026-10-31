# Pitfalls Research

**Domain:** Shelly Cloud polling climate logger (battery sensors, sample-and-hold series) + Open-Meteo outdoor data + SQLite + public read-only UI, Docker on Bombur VPS
**Researched:** 2026-10-31
**Overall confidence:** MEDIUM. Shelly documentation is thin on the exact cloud v2 response shape, 429 behaviour and timestamp fields, so several items are marked "VERIFY IN PROBE". Open-Meteo, Gen1 device behaviour, SQLite/Docker and formula items are HIGH or MEDIUM.

Confidence tags used below: [HIGH] = official docs fetched or reproduced by computation here; [MED] = community/forum evidence or well-established behaviour; [LOW] = inference, must be verified empirically; [PRIOR] = standard knowledge (httpx, SQLite, uPlot) not re-fetched in this session.

**Phase legend** (suggested roadmap phases; the roadmapper may renumber):
- P1 Probe and fixtures
- P2 Poller + DB + parsers (tests against fixtures)
- P3 Deploy on Bombur (Docker, CI, `.env`, healthcheck), as early as possible
- P4 Outdoor weather (Open-Meteo) + backfill
- P5 Health + ntfy alerts
- P6 API + derived metrics (dew point, AH, EMC) + calibration
- P7 Frontend (uPlot)
- P8 CSV export, calibration CLI, polish

---

## Critical Pitfalls

### Pitfall 1: Trusting a timestamp field that is 0, missing, or in an unknown timezone, and `INSERT OR IGNORE` hides the damage

**What goes wrong:** The SPEC's schema has `PRIMARY KEY (device_id, ts, source)` and dedups on "report timestamp advanced". Sleeping devices can expose `unixtime = 0` (SPEC §3, to be verified), and the Gen1 H&T `/status` section of the Gen1 docs does not even list `unixtime`. If the parser takes `unixtime` or `sys.unixtime` at face value, every row for that device gets `ts = 0`, the PK collides, and `INSERT OR IGNORE` silently drops everything after the first row. The poller looks healthy (polls OK, no errors) while the table stays empty for that device. The v1 `_updated` field is a string `"YYYY-MM-DD HH:MM:SS"` with no documented timezone [HIGH: official v1 docs example]; parsing it with naive `strptime` in a container with its own TZ shifts every timestamp by 1-2 h relative to Open-Meteo and the poll log.

**Why it happens:** The "dedup by timestamp" rule is only as good as the timestamp's validity. Device clocks of sleeping devices are set on wake and can be stale or 0; cloud-side `_updated` format and zone are undocumented; `INSERT OR IGNORE` turns a logic bug into silent data loss.

**How to avoid:**
- P1: determine empirically, per model and per endpoint (v2 and v1), which field changes exactly when a new report arrives. Wake a device with its button (adds `act_reasons: ["button"]` on Gen1) and compare the candidate fields against wall-clock time at the moment of the press. Record the timezone of `_updated` (almost certainly UTC, but prove it).
- P2: a `plausible_ts(ts, now)` validator (reject 0, negative, `> now + 60 s`, `< now - N days`) that falls back to the next timestamp source and records `ts_source`. Never write `ts = 0`.
- P2: replace blanket `INSERT OR IGNORE` with an insert that counts the "ignored" rows per poll; assert `ignored <= n_devices_with_unchanged_ts`. Log a WARNING when a device is polled OK N times in a row yet no row is ever inserted.
- P2: when falling back to `ts_source='poll'` (first-seen time), do NOT stamp a stale value with "now" after a poller outage. The first poll after downtime sees the cloud's last state, which may be hours old; only insert it with the cloud timestamp, or flag it (`ts_source='poll'`, lower precision) and exclude it from time-weighted stats edges.
- Parse `_updated` as UTC explicitly (`datetime.strptime(...).replace(tzinfo=UTC)`); never use naive datetimes anywhere.

**Warning signs:** One device has exactly one row in `reading`; a row with `ts` of 1970-01-01; `ts_source` mixes across devices; reports appear 1-2 h shifted relative to a button-wake you did by hand; `poll_log.ok=1` but no new `reading` rows for hours although the Shelly app shows fresh values.

**Phase to address:** P1 (decide the timestamp source), P2 (validator, tests with a `unixtime: 0` fixture).

---

### Pitfall 2: Treating HTTP 200 as success and treating `online` as liveness

**What goes wrong:** Several distinct failure modes collapse into "looks OK":
1. The v1 envelope is `{"isok": true/false, "data": ..., "errors": ...}` [HIGH: v1 docs]. A forum report of `max_req: Request limit reached!` came back as an application-level error on an otherwise accepted request [MED: Shelly forum thread 22549]. A parser that only checks the HTTP status stores nothing (or garbage) and logs `ok=1`.
2. v2's `online` is a 0/1 field per device [HIGH: v2 docs]. Battery devices are asleep most of the time, so `online = 0` is the NORMAL state [MED: Shelly forum KB "battery operated Shelly why not reachable"]. If code skips or flags devices with `online == 0`, it discards valid cached last-state data. If code treats `online == 1` as "fresh", it records a stale value as new.
3. A device missing from the response (unknown id, or `status` omitted: the v2 docs say `status` is left out if every picked property is missing) must be a per-device no-op, not an error for the whole poll and not a zero reading.
4. Coercing missing or `null` to `0`. Gen1 sensors report `null` instead of `0` on sensor error since firmware 1.12.1, and the validity rule is `is_valid: true` AND no `sensor_error` [HIGH: Gen1 changelog]. A 0.0 degC or 0 %RH row looks like a real subfloor reading.

**Why it happens:** Happy-path parsing against one captured response. The v2 docs list no error codes for `/get` and no 429 semantics.

**How to avoid:**
- P2: poll success = HTTP 2xx AND body-level success AND `data` shape valid. Classify each poll outcome: `ok`, `rate_limited`, `auth_rejected`, `wrong_host`, `network`, `shape_error`. Store the class in `poll_log.error` (never the raw URL, see Pitfall 8).
- P2: staleness is decided ONLY by the report timestamp, never by `online`. Never alert on `online=0` alone.
- P2: per-device parse returns `None` (skip) for absent `status`, `is_valid=false`, `sensor_error` present, or null fields. Nothing is ever defaulted to 0. Store NULL columns only if the other field is valid (Flood has no RH by design).
- P1: during the probe, include one deliberately bogus id in a batch to learn whether v2 returns partial results or fails the whole call (undocumented, [LOW] until verified). Also capture a response while a device is asleep.
- P1/P2: normalise device ids (case, hex vs decimal forms) before matching to config; a third-party client had to do this [MED: hex.pm `shelly` library docs].

**Warning signs:** Rows with `t_c = 0.0 AND rh = 0.0`; polls logged OK while `n_devices` < 7; a device flipping between present/absent in `poll_log.n_devices`.

**Phase to address:** P1 (shapes, partial-failure behaviour), P2 (classifier + fixtures for offline/asleep/error devices).

---

### Pitfall 3: Gap and silence thresholds shorter than the devices' real reporting cadence

**What goes wrong:** The SPEC draws gaps after 6 h without a report and alerts when a sensor is "silent beyond a threshold". But Gen1 H&T defaults to a periodic report every **12 h** on battery (10 min on external power) and otherwise reports only when T or RH moves past the threshold [HIGH: Gen1 docs `sleep_mode.period`, Allterco FAQ]. Gen1 Flood: periodic 12 h, temperature threshold 1 degC [HIGH: Gen1 docs]. Gen3/Plus H&T: every 2 h on battery, every 5 min on USB [HIGH: Allterco KB]. A stable subfloor legitimately produces 12 h of silence. A 6 h break threshold draws false gaps in the chart every night; an alert threshold below ~13 h pages the owner for healthy sensors.

**Why it happens:** Assuming the "5 min resolution" goal is achievable. It is not: data is event-driven, resolution is bounded by the thresholds (0.5 degC / 0.5 %RH as configured) and periodic wake. The Flood series will be much coarser than H&T because its threshold is 1 degC unless changed.

**How to avoid:**
- P1/P2: read each device's `/settings` once (`sleep_mode.period`, `sensors.temperature_threshold`, `humidity_threshold`, `external_power`) and record in `devices.toml` as expected cadence; or store a per-model `max_expected_silence` (Gen1 H&T 12 h + margin, Flood 12 h + margin, Gen3 2 h + margin; the margin must cover a missed wake).
- P5: silence alert = `now - last_report_ts > max_expected_silence * 1.25` per device, not one global number. Also alert on battery below ~20% (a dead battery on the subfloor silently ends that series).
- P6/P7: distinguish two gap kinds in data and drawing: (a) device quiet but poller healthy, so the hold is still valid, no break until `max_expected_silence`; (b) POLLER down (no OK polls in `poll_log`), hold is NOT valid because intermediate reports were lost, so break the series there. Drive (b) from `poll_log`, not from the device timestamps.
- Tell the owner in the README that "5 min resolution" means "5 min grid of held values". If finer real data is wanted, the lever is the on-device thresholds (settable down to fractions of a degree through `/settings`, at battery cost), not the poll interval.

**Warning signs:** Nightly chart breaks on the quietest sensors; ntfy alerts at the 6th hour; Flood line looks like a staircase with 12 h treads (that is its real behaviour).

**Phase to address:** P1 (collect settings), P5 (alerts), P6/P7 (gap rules).

---

### Pitfall 4: Sample-and-hold implemented as a plot style instead of as a data semantic

**What goes wrong:**
- Bucket means or linear interpolation of event-driven samples bias toward periods with more reports (devices report MORE when values change fast, e.g. door open, drying surge) and invent values that never existed. Time-weighted mean must weight each value by how long it was held.
- Range edges: the series value at `from` is the last row BEFORE `from` (carry-in). Without it, the first part of every range is empty or the mean is computed over a shorter window. The final held segment extends to `min(to, now)` (or the last OK poll), not to `to`.
- Stats such as min/max must come from raw reports, not from the resampled grid.
- Forward-fill across a poller outage (Pitfall 3b) invents data.
- uPlot needs ONE shared x array; seven devices have seven different report times. If you union the timestamps and leave nulls, uPlot breaks the line at every foreign timestamp (default `spanGaps: false`). The server must forward-fill each series onto the union grid (or the resample grid) and emit `null` only for true gaps.
- `uPlot.paths.stepped` has an alignment option; the wrong direction shifts every hold backward in time. Verify the hold extends to the RIGHT of each sample.
- Server-side downsampling with LTTB or plain averaging is wrong for step data; use min/max per bucket (the SPEC says so) and keep the held value at bucket start.

**Why it happens:** Time-series tutorials assume regular sampling.

**How to avoid:** P6: write the resampler and the time-weighted stats as one pure module over `(ts, value)` segments with explicit carry-in, "valid-until" bounds, and gap intervals; unit-test with hand-computed cases (value held across range start, across a gap, final partial segment, two reports in one bucket). P7: a visual test fixture where the correct picture is obvious (a single step at a known time).

**Warning signs:** Stats mean differs from the visually obvious mean; first hours of "7 d" view blank; lines have tiny breaks where other devices reported.

**Phase to address:** P6 (data semantics + tests), P7 (stepped paths, shared x).

---

### Pitfall 5: EMC computed with the wrong temperature unit (and tests too loose to notice)

**What goes wrong:** The Hailwood-Horrobin formulation in the SPEC takes T in degF. Passing degC is a one-line mistake that is **numerically close at room temperature**, so a sanity test with loose tolerance passes. Computed here with the SPEC formula:

| T / RH | Correct (degC converted to degF) | degC passed in by mistake |
|--------|----------------------------------|---------------------------|
| 21 degC / 50% | 9.25% | 9.47% |
| 21 degC / 65% | 11.97% | 12.34% |
| 0 degC / 50% | 9.52% | 9.12% |
| 30 degC / 60% | 10.64% | 11.34% |
| 40 degC / 60% | 10.20% | 11.32% |

A test written as `≈ 9.2 ± 0.3` passes the wrong code at 21 degC. Also: the Gen1 `tmp.value` is in the device's configured unit (`tmp.units`) while `tmp.tC` is always degC [HIGH: Gen1 docs]; a device switched to degF in the app would feed degF into a function expecting degC.

**Why it happens:** Formula sources (USDA Wood Handbook) are in imperial units; Magnus dew point uses degC; the same module mixes both.

**How to avoid:**
- P6: the derived module's public functions take degC and %RH only; a single private `_c_to_f()` is the only place conversion occurs. Name parameters `t_c`.
- P6: tests at multiple temperatures with tolerance <= 0.05%: pin 21 degC/50% -> 9.25, 21/65 -> 11.97, 0/50 -> 9.52, 30/60 -> 10.64, 40/60 -> 10.20 (values from the SPEC formula; also cross-check against Wood Handbook Table 4-2 for the 21 degC row). A boundary test at RH 0 and 100.
- P2: parsers read `tmp.tC` (Gen1) and `temperature:0.tC` (Gen3), never `tmp.value`.
- Dew point/AH: use ONE set of Magnus constants (17.62/243.12) everywhere; clamp RH into (0.01, 100] before `log` (Flood has no RH: return `None`, do not compute; RH=0 gives `log(0)`; RH just over 100 after an offset is common).
- For outdoor series compute dew point/AH with the same function from Open-Meteo T and RH; use Open-Meteo's `dew_point_2m` only as a cross-check, so indoor and outdoor curves use identical math.

**Warning signs:** Indoor EMC curve 0.2-0.7 points off the Wood Handbook table; ValueError math domain error from the dew-point function on a Flood row.

**Phase to address:** P6.

---

### Pitfall 6: Open-Meteo endpoint choice, data lag and forecast rows stored as observations

**What goes wrong:**
- The ERA5/ERA5-Land **archive** API has a ~5 day delay [HIGH: Open-Meteo archive docs]. Using it for "outdoor data for the period already logged" leaves the last 5 days empty, which is exactly the part that matters now.
- The **forecast** API returns forecast days by default; storing the whole response puts future model values in the `weather` table as if observed. Always request `forecast_days=1` (or `past_hours`/`past_minutely_15`) and drop any row with `ts > now`. `past_days` is capped at 92 [HIGH].
- The **historical-forecast** API (stitched first hours of past model runs) is the right backfill for recent weeks; it starts around 2022 and its docs warn it is less suited for long series because model versions change [HIGH]. Mixing archive (0.25 degree/11 km reanalysis) and forecast models (ICON-D2 ~2 km) in one series creates visible seams at the join.
- `best_match` can switch models between runs; the same location may jump by 1-2 degC when the model changes. Pin `models=` explicitly (e.g. an ICON/AROME model that covers Central Europe) and store the model name per row or in config.
- "15-minutely" is **native only in Central Europe and North America** (ICON-D2/AROME, HRRR); elsewhere it is interpolated from hourly [HIGH: forecast docs]. Even within Central Europe, whether every requested variable is native or interpolated varies by variable [LOW: not verified per variable]. The archive API is hourly only [HIGH].
- Timestamps: with the default `timezone=GMT` the response gives naive ISO strings like `2026-10-31T13:00`; `timezone=auto`/named zones fix the UTC offset at request time and ignore a DST change inside the range [HIGH: docs]. A backfill spanning 25 Oct 2026 (EU DST end) would be off by 1 h on one side. Use `timeformat=unixtime` and store UTC integers.
- Precipitation (and radiation) are preceding-interval sums; temperature/RH/pressure are instantaneous [PRIOR; the docs describe a `current.interval` for backward-looking sums]. Do not interpolate or average sums as if instantaneous.
- Hourly outdoor values are an instantaneous model sample at the label time: do NOT apply the sensor "hold forward" semantic to them; treat them as a smooth series (linear interpolation is acceptable and honest here), and label it "model, not station".
- JSON arrays may contain `null` for missing hours; Python `None` must not be stored as 0. Errors come as HTTP 4xx with `{"error": true, "reason": "..."}`.

**Why it happens:** Four similar-looking endpoints; docs describe each in isolation.

**How to avoid:**
- P4: design: live fetch = forecast endpoint with `past_hours` large enough to self-heal gaps (e.g. 24-48 h), `forecast_days=1`, `timeformat=unixtime`, pinned `models`, upsert by `(ts, variable_set)` so later, better values overwrite earlier ones; drop `ts > now`.
- P4: backfill = historical-forecast API from the first sensor row onward (same model family as the live fetch, so no seam); fall back to `past_days` (<=92). Do not use the archive API for the recent window; use it only if a pre-2022 comparison is ever wanted.
- P4: spike (10 min) to compare `minutely_15` against `hourly` for your coordinates and see whether values between full hours are actually different; if not, store hourly and say so.
- Keep the weather fetcher isolated (own task, own `weather_fetch_log`, own backoff) so an Open-Meteo outage never delays Shelly polling.
- Rate limits are generous (600/min, 5000/h, 10000/day free; requests with >10 variables or >2 weeks of data count fractionally) [HIGH: pricing page]; fetch every 15-60 min, not every minute. No uptime guarantee and misuse may be blocked without notice [HIGH: terms].

**Warning signs:** Weather table has rows in the future; last 5 days outdoor is blank; 1 h step at the DST date; an unexplained step in outdoor T when best_match rolled over.

**Phase to address:** P4.

---

### Pitfall 7: SQLite WAL across containers, read-only mounts, UIDs, and backups

**What goes wrong:** The SPEC has a poller process and a web process sharing one DB. Specific traps on Bombur:
- WAL works across containers on one host, because the `-shm` index is an mmap'd file and all containers share the host kernel [MED: Simon Willison's April 2026 experiment, run on Docker Desktop macOS, not on native Linux; consistent with SQLite docs: WAL needs same-host shared memory, not network filesystems]. It does NOT work on NFS/CIFS or 9p mounts.
- Mounting `./data` into the web container with `:ro` breaks WAL readers: they must still create/write `-shm` ("unable to open database file" / "attempt to write a readonly database") [PRIOR]. Mount read-write and open the connection read-only in code (`file:...?mode=ro` URI plus `PRAGMA query_only=ON`).
- Different UIDs in the two containers: the poller creates `-wal`/`-shm` as mode 644, owned by UID A; a reader running as UID B cannot write `-shm`. Run both services as the same UID, and chown `./data` to it once (Bombur known pitfall: container UID must be able to write `./data`).
- Local dev on WSL2: a DB under `/mnt/c/...` (9p/drvfs) breaks WAL; keep dev DB on the Linux filesystem.
- Backups: "copy the deploy directory" (Bombur convention) copies `.db`, `-wal` and `-shm` at different moments from a live WAL DB; the copy can be inconsistent or miss recent commits if `-wal` is not copied. Use `sqlite3 data/shelog.db ".backup ..."` or `VACUUM INTO` for the backup artefact.
- Long-lived read transactions in the web app (a cursor kept open) stop WAL checkpointing and let `-wal` grow. Use short transactions and `PRAGMA wal_autocheckpoint`.
- Missing `busy_timeout` makes the poller's write fail with `database is locked` when a CSV export holds a read lock during checkpoint.

**How to avoid:**
- P3: simplest robust option: run poller and web in ONE container/process (poller as a background task in the same app) with a single UID; no cross-container concerns. If two services are preferred for isolation, give them the same `user:`, rw bind mount, and test on the server.
- P2: set `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON` on every connection; poller is the only writer.
- P3: a documented backup command (cron on Bombur or a tiny in-app daily `VACUUM INTO data/backup/`) and a restore test.
- Single-instance guard (see Pitfall 9): `flock` on `data/.poller.lock`.

**Warning signs:** `database is locked`, `attempt to write a readonly database`, web UI works only after the poller restarts, `-wal` file multi-GB, backup restore missing the last day.

**Phase to address:** P2 (pragmas, single writer), P3 (mounts, UID, backup).

---

### Pitfall 8: The auth_key leaks through logs, error strings, traces, the API, or fixtures

**What goes wrong:** The v2 endpoint takes the key in the URL query (`?auth_key=...`) [HIGH: v2 docs]. Consequently:
- `httpx` logs every request at INFO as `HTTP Request: POST https://host/v2/...?auth_key=SECRET "HTTP/1.1 200 OK"` [PRIOR]. `docker logs` now contain the key.
- `str(httpx.HTTPStatusError)` and many exception messages include the full URL. If the poller stores `str(e)` into `poll_log.error`, and `/api/health` returns the last error or ntfy sends it, the key is public.
- OpenTelemetry HTTP-client instrumentation records the full URL as a span attribute; Bombur's convention is to emit OTLP, so the key would leave the process into the collector/Grafana [PRIOR].
- The poll_log or `/api/health` echoes `host` (`shelly-NN-eu.shelly.cloud`), which identifies the account's cloud shard.
- Committed fixtures: Gen1/Gen3 status JSON contains the device MAC (the Gen1 device id is MAC-derived), `wifi_sta.ssid`, `wifi_sta.ip`, and cloud/MQTT details [PRIOR; verify in probe]. Fixtures committed to a (possibly public) repo leak these.
- An ntfy.sh topic is effectively a password (anyone who knows the topic can read/post); the topic must not be in the repo or public config.
- The Open-Meteo request and response echo `latitude`/`longitude`; do not return them from the public API (it is the owner's home).

**How to avoid:**
- P2: wrap every outbound call in a `safe_request` that catches exceptions and records ONLY the class name + HTTP status + short redacted message; never `str(exc)`. Set the `httpx` and `httpcore` loggers to WARNING; install a log filter that redacts `auth_key=[^&\s"]+`. Disable URL attributes in OTel instrumentation (or do not instrument httpx).
- P1: the probe has a `--sanitize` step that replaces id/MAC/SSID/IP/auth fields with stable fake values before writing fixtures to `tests/fixtures/`; add a test (or pre-commit grep) that fails if a fixture contains the real device ids or a 32+ char token. Real ids live only in server config.
- P3: `.env.example` uses a placeholder that is NOT `CHANGE_ME` for externally issued secrets (see Pitfall 11).
- P5: ntfy topic from env; random and long.
- P6: public API exposes slug ids (`LR-1`) and display names only; round coordinates to ~2 decimals for the fetch and never echo them.

**Warning signs:** `grep -r auth_key` finds hits in `docker logs`, `poll_log`, test fixtures, or `/api/health` output.

**Phase to address:** P1 (sanitized fixtures), P2 (logging/redaction), P5 (alert content), P6 (public API shape).

---

### Pitfall 9: Violating the account-wide 1 req/s limit (second callers, restart storms, retry loops)

**What goes wrong:** The limit is per ACCOUNT, not per process [MED: v2 docs say 1 req/s; hex.pm client doc and a forum thread say bursts get 429/`max_req`]. Things that share it: a developer running `poll` locally with the prod key while Bombur polls; a staging container with the same key; the probe command; the Shelly app's cloud control calls; a crash-looping container that retries at start-up; the poller and any test script. Docs give no `Retry-After` or retry-window semantics [HIGH: v2 docs state no 429 behaviour].

**How to avoid:**
- P2: poll interval 60 s with jitter; backoff exponential with cap (e.g. 5 s -> 5 min) and full jitter on 429/5xx/network error; honour `Retry-After` if present; never retry within the same second; a global token-bucket (1 req / 1.2 s) for ALL Shelly calls in the process, including probe and any v1 fallback.
- P2: single-instance guard (`flock` on a file in `data/`); fail fast with a clear message if another instance is running.
- P3: no staging environment with the real key; dev uses fixtures and `--once` against prod only deliberately. Document in the README.
- P3: `restart: unless-stopped` plus a startup sleep/backoff when config is invalid, so a bad `.env` is not a hot restart loop that burns rate limit and spams ntfy.
- Do not add a second endpoint "just to be sure" (v1 `device/status` per device would multiply requests: 7 requests = 7 seconds of budget). v1 is deprecated ("will be removed in the near future") [HIGH: v1 docs]; do NOT build a v1 fallback until v2 proves insufficient in the probe.

**Warning signs:** Intermittent 429s with no obvious cause; 429s that disappear when the dev laptop closes.

**Phase to address:** P2, P3.

---

### Pitfall 10: Auth/host failures indistinguishable from outages; key and host rotation

**What goes wrong:** The auth key changes if the account password changes; the account's server host can change [HIGH: Allterco FAQ and "Troubleshooting Cloud API": check server assignment, auth key after password change]. Error responses for a wrong key or wrong server are not documented [HIGH: v1/v2 docs list no such errors; no source found describing the response for a wrong server]. If the poller treats every failure as "network blip, retry", it can run for days against a dead key: Core Value (data) is lost silently, and for sleeping devices the intermediate reports are gone for good.

**How to avoid:**
- P1: capture what a wrong key and a wrong host actually return (test with a deliberately wrong key on a throwaway request, within the rate limit), and encode that as the classifier for `auth_rejected` / `wrong_host`.
- P2/P5: `auth_rejected` raises an immediate ntfy alert (not after 1 h), then re-alerts daily; other failures alert after >1 h of consecutive failure and again at recovery (state-transition alerts, not per-poll alerts, to avoid ntfy spam).
- Keep key and host as env vars separately; the README says "after changing the Shelly password: re-copy BOTH key and host from the app (User settings -> Authorization cloud key), update `.env`, `docker compose up -d`".
- External dead-man's switch: the poller cannot alert on its own death. Use a container HEALTHCHECK (last OK poll < 5 min ago) and an external probe (e.g. Bombur's Prometheus/Grafana scraping the health endpoint, or a ping-style heartbeat service). [MED]

**Warning signs:** `poll_log` shows all failures with same HTTP status for hours; no ntfy message although nothing is logged.

**Phase to address:** P1 (capture error shapes), P5.

---

### Pitfall 11: Bombur/Docker deployment traps that lose or corrupt data

**What goes wrong:**
- **DB written outside the bind mount.** If `DB_PATH` defaults to a path in the image filesystem (e.g. relative `./shelog.db`) and compose mounts `./data` elsewhere, the DB lives in the container layer and **vanishes on the next deploy** (every push to `main` recreates the container). This is the main "losing data during deploys" risk; the brief restart gap itself costs almost nothing because the cloud holds the last state.
- **Non-root UID vs `./data` ownership** (Bombur known pitfall). The container crash-loops with "unable to open database file" on first deploy.
- **`CHANGE_ME` placeholders.** `bombur-add-app` replaces the literal value `CHANGE_ME` in `.env.example` with a random value [per bombur-infra skill]. If `SHELLY_AUTH_KEY=CHANGE_ME` is used, the onboarding silently writes a random key; the poller then gets auth failures and the owner may not realise they must paste the real key and host. Use a distinct placeholder (e.g. `SET_ME_FROM_SHELLY_APP`) for externally issued values and make the app refuse to start (with a sleep, see Pitfall 9) if it sees it. Only truly app-internal secrets (e.g. a random salt) should use `CHANGE_ME`.
- **PID 1 and SIGTERM.** A Python process as PID 1 ignores SIGTERM unless it installs a handler; compose waits 10 s then SIGKILLs. SQLite WAL survives SIGKILL, but a poll in-flight is wasted and you may leave a half-written batch if you wrap it in many commits. Use `init: true` (tini) or a handler; commit one transaction per poll.
- **Schema migrations on start in an auto-deployed pipeline.** A broken migration + `restart: unless-stopped` = crash-loop = lost days with no staging. Make migrations forward-only and additive (old image must run on the new schema so rollback by `IMAGE_TAG` works), and test them against a COPY of the production DB before merging.
- **Both services use the same image tag.** Every deploy restarts poller AND web. Acceptable (seconds), but keep start-up work (migrations, backfill) off the critical path: poll first, backfill asynchronously.
- **No healthcheck:** a deployed container that is "running" but not polling looks fine. Add `HEALTHCHECK` on last OK poll age.
- **Docker enabled at boot / unattended-upgrades reboot at 04:30:** confirm `restart: unless-stopped` and that Docker starts at boot; the first polls after boot can fail on network not yet up; the 1 h alert threshold absorbs it.
- Do not publish `ports:`; only the web container joins the `proxy` network (Bombur convention); `container_name` must be `shelog-web` and unique.
- Dev data in the repo: `.dockerignore`/`.gitignore` must exclude `data/` and `.env`.

**How to avoid:** P3: at start-up, assert that `DB_PATH` resolves inside the configured data dir and that the data dir is a mount point in production (`os.path.ismount`) or contains a sentinel file written on first run; log the resolved path. Do the first deploy with a throwaway row, redeploy, and confirm it survives.

**Warning signs:** Row count resets after a deploy; container restart count climbing; `docker compose ps` shows "restarting".

**Phase to address:** P3 (all of the above), P2 (single-transaction poll, SIGTERM handling).

---

### Pitfall 12: Time zones and DST in the UI, CSV and stats

**What goes wrong:** The apartment is in Central Europe; EU DST ended on 25 Oct 2026 and the next change is 28 Mar 2027, so any multi-month renovation logging crosses it. Typical errors:
- uPlot (and `Date`) render in the **viewer's** browser zone by default: the same public chart shows different hours to different visitors, and the "03:00-04:00" on autumn day appears duplicated or missing.
- "Daily" or "per day" aggregation bucketed by `ts // 86400` (UTC days) rather than local days; local days are 23 or 25 h long.
- Range presets computed on the client (`Date.now()`) while "last report 3 h ago" is computed on the server: client clock skew shows negative or large ages.
- CSV with local naive times has an ambiguous repeated hour on fall-back day; Excel mangles ISO strings with `Z`.
- Open-Meteo `timezone=auto` hides a fixed offset (Pitfall 6).
- Python `datetime.now()` naive or `utcfromtimestamp` (deprecated); `TZ` env in the container.

**How to avoid:** P2: all storage and logic in UTC unix seconds; no naive datetimes (ruff rule `DTZ` enabled). P7: a configured display zone (e.g. `Europe/Prague`) used consistently via uPlot's `tzDate` hook with `Intl.DateTimeFormat`, and shown on the page ("times in Europe/Prague"); server returns its own `now` and the UI uses `now` from the API for ages and presets. P8: CSV has `ts_utc` (ISO 8601 with `Z` or unix) AND `ts_local` with explicit offset. P6/P7: if you offer daily stats, compute the local-day boundaries with `zoneinfo`. Add a test fixture spanning 25 Oct 2026 02:00-03:00 local.

**Warning signs:** A chart looks 1-2 h shifted compared with the Shelly app; two visitors see different axis labels; duplicate x labels on 25 Oct.

**Phase to address:** P2 (UTC discipline), P4 (Open-Meteo timeformat), P7, P8.

---

## Moderate Pitfalls

### Pitfall 13: Gen1 vs Gen3 vs Flood status shapes, units and field presence
**What goes wrong:** Parsers written for one model break on another: Gen1 `tmp.tC` / `hum.value` / `bat.value`; Gen3 `temperature:0.tC` / `humidity:0.rh` / `devicepower:0.battery.percent` [SPEC, unverified for the cloud v2 envelope]. Gen1 `tmp.value` follows the configured unit; `tmp.tC` is always degC [HIGH]. `sensor_error` is present only on error; `bat.value` shows 100% on USB power [HIGH: Gen1 changelog]. The Gen3 may expose external-sensor components (`temperature:100+`) if an add-on is attached; Gen3 reports `devicepower:0` battery as a nested object. Flood has no `hum`; code that does `status["hum"]["value"]` raises `KeyError` and kills the whole poll batch.
**Prevention:** P1 captures one fixture per model plus an error and an asleep variant. P2: one parser function per model, selected by config `model` AND cross-checked against the response's `type`/`gen`/`code` fields (v2 returns them [HIGH]); a mismatch is logged as `shape_error` for that device only. A per-device `try/except` so one malformed device never loses the other six. Also check on-device `temperature_offset` in Flood/H&T settings once; a non-zero on-device offset contaminates "raw uncalibrated" data.
**Phase:** P1, P2.

### Pitfall 14: Dedup rule that drops "unchanged" reports
**What goes wrong:** Dedup by VALUE (the SPEC's fallback) discards re-reports of identical values, which are the only evidence that a stable sensor is alive and that the held value is still true. Dedup by timestamp alone keeps them, but if `_updated` also moves on non-sensor events (wifi/cloud status, OTA), you store rows with unchanged values; that is fine, rows are tiny.
**Prevention:** P2: dedup key is the validated report timestamp; keep identical-value rows; additionally maintain `device_state(last_report_ts, last_poll_seen_ts)` so silence alerts do not need to scan `reading`. If only value-based dedup is possible for some model, still update `last_seen`. Record `ts_source` per row and report the share of each source in `/api/health`.
**Phase:** P2.

### Pitfall 15: Raw JSON retention bloat
**What goes wrong:** Storing the full 7-device JSON at every 60 s poll is ~1440 snapshots/day of several KB each (order of 10 MB/day [LOW: estimate]) and grows the DB, backups and WAL; also the raw JSON contains MACs/SSIDs (Pitfall 8).
**Prevention:** P2: store raw JSON only when a device's report timestamp changes (or on parse failure), compress or truncate, enforce retention (`raw_status` older than N days deleted by a daily job), toggle via env; never expose through the API.
**Phase:** P2.

### Pitfall 16: Calibration pitfalls
**What goes wrong:** Offsets estimated on a co-location window with temperature drift, or sampled on the raw irregular timestamps, mix thermal lag and quantisation (the 0.5 threshold) into the offset. Offsets applied to T but not to the dependent RH inflate dew point/EMC errors. Offset after clamp (RH > 100%).
**Prevention:** P8: compute offsets on a common forward-filled 5-min grid, use only windows where all sensors are stable (small spread of the median), report the spread, and apply `rh_offset` before clamping to [0,100]. Keep offsets with an effective-from timestamp in config if re-calibration will happen, so history does not silently change meaning (store the offset version, or accept that read-time calibration re-bases the whole history and say so).
**Phase:** P6/P8.

### Pitfall 17: Unbounded public API requests
**What goes wrong:** `/api/readings?from=0&to=9999999999&resample=none` or `resample=1s` on a tiny VPS allocates huge results; CSV export of "all" is one request. Anyone on the internet can trigger it.
**Prevention:** P6: whitelist `resample` (`none|5m|15m|1h`), clamp the number of output points (e.g. 20k), cap raw rows per request, validate `devices`; add `Cache-Control: max-age=30` and gzip at Caddy; for `resample=none` over long ranges force server-side downsample. Read-only DB connection.
**Phase:** P6.

### Pitfall 18: EMC/RH interpretation errors in the UI
**What goes wrong:** EMC from air T/RH is the moisture the wood WOULD reach, not the measured moisture of the subfloor. Sensors lying on a wet subfloor read the air layer at the surface (possibly self-heating or cold-bridge effects); H&T RH accuracy is a few %RH, so differences between sensors under ~3 %RH are noise. Showing EMC with two decimals suggests precision that does not exist, and mixing model-based outdoor series with sensor series without labelling invites false conclusions.
**Prevention:** P7: label EMC "equilibrium moisture content in this air"; show sensor accuracy note; mark outdoor as "Open-Meteo model"; one decimal place; Open-Meteo CC BY 4.0 attribution shown on the page (required; free tier is for non-commercial, no ads/subscriptions [HIGH]).
**Phase:** P7.

---

## Minor Pitfalls

### Pitfall 19: CDN-loaded JS on a public page
uPlot from a CDN adds an external dependency and a tracking/availability surface. Vendor the minified file in the repo (no build step needed) and serve it from the app. Phase P7.

### Pitfall 20: Real Time Events API distracts from the MVP
Shelly's WebSocket push API would remove lost-intermediate-sample risk but needs OAuth with account credentials and adds a long-lived connection; defer. Investigate only after the poller has run a few days and you can quantify how often two reports arrive within one poll interval (compare `reading` ts spacing). Phase P8 or later.

### Pitfall 21: Poll-time vs report-time mismatch in stats
Showing "last report" as the last poll time (instead of the last report timestamp) or health based on `online`. Use report ts and last OK poll time as two separate indicators. Phase P5/P7.

### Pitfall 22: Poll timing alignment
Polling exactly every 60 s with sleep after processing drifts; polling with `asyncio.sleep(60)` after a slow request still respects 1 req/s. Use a monotonic scheduler with jitter; skip (don't queue) missed ticks after a long outage so that a backlog never fires multiple requests per second. Phase P2.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Parsing `tmp.value` instead of `tmp.tC` | One code path | Wrong data if a device is switched to degF | Never |
| `INSERT OR IGNORE` without counting ignored rows | Simple dedup | Silent total data loss (ts=0) | Never; count and assert |
| Naive `datetime` anywhere | Quick | DST/TZ off-by-hours bugs across storage, CSV and Open-Meteo | Never |
| Storing derived metrics (EMC, dew point) in the DB | Faster reads | Cannot fix formula or calibration afterward | Never (already decided: derive at read time) |
| Skipping the single-instance lock | Less code | Double pollers exceed 1 req/s, 429 storms | Never in prod |
| Keeping full raw JSON forever | Debuggability | Growth, leaks MACs/SSIDs | Only first days (as SPEC says) with retention |
| One service for poller+web | No cross-container WAL problems | Web crash can stop polling | Acceptable for MVP; isolate polling error handling in its own task |
| Hardcoding gap threshold to 6 h | Simple | False gaps on 12 h periodic sensors | Never; per-model value |
| Using `best_match` Open-Meteo model | Zero config | Model switches cause seams | Only if recording which model was used |
| Skipping tests for the deploy pipeline | Faster first deploy | Auto-deploy of a broken poller from `main` | Only for the first "hello poller" deploy; fixture tests must precede the second |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Shelly Cloud v2 `/devices/api/get` | Using `online` for liveness; checking only HTTP 200; per-device HTTP calls | One batched call (<=10 ids), classify body-level result, staleness from report timestamp |
| Shelly auth | Key in URL gets logged by httpx / OTel / exceptions | Redaction filter, loggers at WARNING, never persist `str(exc)` |
| Shelly host | Hardcoding `shelly-NN-eu` | Host from env; alert on persistent failure; re-copy key+host after password change |
| Shelly rate limit | Dev + prod using same key concurrently | Single-instance lock, shared token bucket, no staging with real key |
| Shelly Gen1 | Reading `tmp.value` | Read `tmp.tC`; check `is_valid` and `sensor_error` |
| Open-Meteo forecast | Storing future rows; `best_match`; naive ISO strings | `forecast_days=1`, drop `ts > now`, pin `models`, `timeformat=unixtime` |
| Open-Meteo archive | Using it for the last days | 5-day lag: use historical-forecast / forecast `past_days` for recent window |
| Open-Meteo terms | No attribution on public page; ads/subscriptions | CC BY 4.0 credit link in UI; stay non-commercial |
| ntfy.sh | Public topic name in repo; alert per failed poll | Random topic in `.env`; state-transition alerts with re-alert interval |
| Caddy/Bombur | `ports:` published; container_name collision | Only `shelog-web` on `proxy`; no host ports |
| GHCR/bombur-deploy | `CHANGE_ME` for externally issued secrets | Distinct placeholder + startup validation |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Unbounded `resample=none` / CSV for long ranges | Slow/oom responses, exposed DoS | Point caps, whitelist resample, cache headers | Months of data, bots hitting the endpoint |
| Forward-fill via SQL self-joins on a generated grid | Slow queries | Fetch carry-in row + range rows per device, forward-fill in app code | > 100k rows (not at this scale; tiny: ~7 devices x a few hundred events/day) |
| Raw JSON snapshot per poll | DB growth ~MBs/day | Store on change, retention | After a few weeks |
| Long read transactions | WAL grows | Short transactions | Heavy UI use |
| Re-fetching Open-Meteo for large ranges every start | Counts fractionally against the quota; slow start | Backfill only the missing window | After repeated restarts / deploys |

Expected data volume is tiny (event-driven readings from 7 devices); the "less than 2 s for 7 days" criterion is trivially reachable. The real performance risks are public unbounded queries and payload size (gzip).

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| `auth_key` in logs/errors/traces/`poll_log`/health | Anyone can read/control the Shelly account's devices (key has account scope) | Redaction filter; never persist exception strings; keep key in server `.env` only |
| Real device ids/MACs/SSIDs in fixtures or API | Device identification, targeting | Sanitiser in probe; slug ids in public API |
| Lat/lon echoed by API/health/CSV | Reveals the owner's home location | Round for fetch, never return coordinates |
| ntfy topic in repo or UI | Others read/post alerts | Random topic in `.env` |
| Public `/api/health` returning raw error text | Info leak | Return coarse states (`ok`, `degraded`, `auth_error`) and timestamps only |
| Web container rw access to DB | Bug in UI could corrupt data | `mode=ro` URI + `query_only` on the web connection |
| `ports:` published or CORS `*` with credentials | Bypasses Caddy/HTTPS (Docker bypasses ufw) | Never publish ports; no CORS needed |
| Dev tooling committed (`.env`, `data/`) | Secret leak | `.gitignore` + `.dockerignore` + secret scan in CI |

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| Linear interpolation of sensor data | Fake smooth curves, false trends | Step rendering with break rules from Pitfall 3 |
| Gap breaks at wrong threshold | Misreads healthy sensors as dead | Per-model threshold; poller-outage gaps shown differently from quiet-device |
| Viewer-local time axis | Different visitors see different hours | Fixed display zone, shown in the UI |
| Mixed series without provenance | Outdoor model data mistaken for a measurement | Label "Open-Meteo model", dashed/different style |
| EMC shown as precise to 0.01 % | False confidence | One decimal, accuracy note |
| Offline sleeping devices shown as "offline" red | Alarm fatigue | Show last report age + expected cadence instead |
| Zoom state lost on refresh | Frustration | Keep range in URL hash |

## "Looks Done But Isn't" Checklist

- [ ] **Poller:** rows actually inserted for ALL 7 devices after 24 h (not only `poll_log.ok`) - verify per-device row counts and `ts_source` distribution
- [ ] **Timestamps:** no `ts` of 0 or in the future; `_updated` timezone proven by a button wake
- [ ] **Deploy:** row count survives a redeploy (DB path inside the bind mount); a deliberate `docker compose up -d` recreate shows no data loss
- [ ] **Secrets:** `grep -ri auth_key` over `docker logs`, `poll_log`, fixtures, repo, `/api/*` is empty
- [ ] **Rate limit:** 24 h with zero 429 in `poll_log`; second instance start is refused
- [ ] **Alerts:** a deliberately wrong key triggers an auth alert within minutes; killing the container triggers an external alert
- [ ] **Weather:** no rows with `ts > now`; last 5 days present; DST boundary (25 Oct 2026) continuous
- [ ] **Formulas:** EMC tests at 0/21/30/40 degC with tight tolerance; dew point/AH defined for RH in (0,100]
- [ ] **Gaps:** healthy 12 h-silent sensor shows no break; poller outage shows a break
- [ ] **Backup:** restore of the `.backup` file works
- [ ] **Public UI:** no device ids, no coordinates, no raw errors, Open-Meteo attribution present
- [ ] **Restart/reboot:** container comes back after VPS reboot and polls without manual action

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Silent ts=0 collisions (P1) | MEDIUM-HIGH | Lost intermediate reports are unrecoverable; if raw JSON was retained, re-parse; otherwise only the last state per device survived. Fix validator, redeploy; later optionally backfill from Shelly Cloud charts via a HAR capture (`source='cloud_stats'`) |
| DB outside bind mount (P11) | HIGH | Data in the old container layer is gone after recreate; `docker cp` from the stopped container if it still exists; otherwise accept the loss. Fix path; add mount assertion |
| Wrong EMC unit (P5) | LOW | Derived at read time: fix function and redeploy; history corrects itself |
| Wrong calibration (P16) | LOW | Config only; raw data untouched |
| Leaked auth key (P8) | MEDIUM | Change the Shelly account password (rotates key), update `.env`, redeploy; scrub logs and rotate Docker logs; purge repo history if committed |
| Auth/host change undetected (P10) | MEDIUM | Re-copy key+host, redeploy; gap data is lost (cloud keeps only last state) |
| Forecast rows stored as observations (P6) | LOW | `DELETE FROM weather WHERE ts > fetched_at`; refetch with the right endpoint |
| Wrong Open-Meteo model/seam (P6) | LOW | Weather is re-fetchable: drop and backfill with pinned model |
| WAL corruption/permission (P7) | LOW-MEDIUM | Restart with matching UIDs; if `-wal` lost, restore from `.backup` |
| Bad migration crash-loop (P11) | MEDIUM | Roll back `IMAGE_TAG` to previous SHA (works if migration was additive); poll gap until fixed |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| 1 Timestamp validity / silent drops | P1, P2 | Fixture with `unixtime:0`; per-device row count after 24 h; ignored-row counter |
| 2 HTTP 200 / online / null-to-zero | P1, P2 | Classifier tests incl. bogus-id probe, asleep and error fixtures |
| 3 Gap/silence thresholds | P1, P5, P6/P7 | Config has per-model cadence; healthy 12 h-quiet sensor produces no alert/break |
| 4 Sample-and-hold semantics | P6, P7 | Hand-computed time-weighted mean tests; visual step fixture |
| 5 EMC units / dew point domain | P6 | Multi-temperature tight-tolerance tests; Flood (no RH) handled |
| 6 Open-Meteo endpoints/lag/TZ/model | P4 | No future rows; last 5 days present; unixtime used; model pinned |
| 7 WAL in Docker | P2, P3 | Two-process test on the server; backup/restore test; same UID |
| 8 Secret leakage | P1, P2, P5, P6 | grep checks; fixture sanitiser test |
| 9 Rate limit violations | P2, P3 | 24 h zero 429; second instance refused |
| 10 Auth/host failure handling | P1, P5 | Wrong-key drill raises alert |
| 11 Deploy traps | P3 | Redeploy keeps data; placeholder refused; SIGTERM clean |
| 12 TZ/DST | P2, P4, P7, P8 | DST-spanning fixture (25 Oct 2026) in tests; display zone visible |
| 13 Model shape differences | P1, P2 | One fixture per model; per-device try/except |
| 14 Dedup rule | P2 | Unchanged re-report retained; `last_seen` updated |
| 15 Raw JSON bloat | P2 | Retention job; DB size monitored |
| 16 Calibration | P6, P8 | Offsets reported with spread; applied before clamp |
| 17 Unbounded API | P6 | Max points enforced; whitelist test |
| 18 Interpretation/labels | P7 | Labels and attribution present |

## Items Needing Empirical Verification in the Probe (not resolvable from docs)

1. Exact v2 response envelope for Gen1 devices (does `status` include `_updated`, `unixtime`, `time`, `bat`, `tmp.tC`?) and the Gen3 equivalent (`ts`, `sys.unixtime`).
2. Timezone and update semantics of `_updated`; whether it moves on non-sensor events.
3. Behaviour of a v2 batch containing an unknown id (partial vs fail).
4. Response to a wrong `auth_key` and to a wrong host.
5. 429 body, HTTP status and any `Retry-After`.
6. Whether v2 accepts `auth_key` outside the query string (header/body) to avoid URL leakage.
7. Device `/settings` (period, thresholds, on-device temperature offset) for all 7 devices.
8. Whether Open-Meteo `minutely_15` for the flat's coordinates is native or interpolated for T and RH.

## Sources

- Open-Meteo pricing/limits: https://open-meteo.com/en/pricing [HIGH]
- Open-Meteo terms (non-commercial, CC BY 4.0, blocking): https://open-meteo.com/en/terms [HIGH]
- Open-Meteo historical weather (archive) API, 5-day delay, hourly only, timezone/DST note: https://open-meteo.com/en/docs/historical-weather-api [HIGH]
- Open-Meteo historical forecast API (stitched runs, from ~2022, 15-minutely Central Europe/NA only): https://open-meteo.com/en/docs/historical-forecast-api [HIGH]
- Open-Meteo forecast docs (`minutely_15`, `past_days` <= 92, `timeformat=unixtime`, timezone behaviour, `current.interval`): https://open-meteo.com/en/docs [HIGH]
- Shelly Cloud Control API v2 (`/v2/devices/api/get`, <=10 ids, `online` 0/1, 1 req/s): https://shelly-api-docs.shelly.cloud/cloud-control-api/communication-v2 [HIGH]
- Shelly Cloud Control API v1 (`/device/status`, `isok`, `_updated` string, deprecation notice): https://shelly-api-docs.shelly.cloud/cloud-control-api/communication [HIGH]
- Shelly Gen1 API (H&T/Flood `/status` and `/settings`, `sleep_mode.period` 12 h / 10 min on external power, thresholds, `is_valid`, `sensor_error`, null on error since 1.12.1): https://shelly-api-docs.shelly.cloud/gen1/ [HIGH]
- Allterco KB, Plus/Gen3 H&T wake-up schemes (2 h periodic, 5 min on USB, 3-minute awake): https://allterco.freshdesk.com/en/support/solutions/articles/103000226308-wake-up-schemes-and-reports-of-shelly-plus-h-t [HIGH]
- Allterco KB, H&T update period (12 h Gen1, fixed): https://allterco.freshdesk.com/en/support/solutions/articles/103000043817-is-it-possible-to-adjust-the-status-update-period-for-shelly-h-t-shelly-plush-t- [MED]
- Allterco KB, Troubleshooting Cloud API (server assignment, key changes with password): https://allterco.freshdesk.com/en/support/solutions/articles/103000295302-troubleshooting-cloud-api [MED]
- Shelly forum, "Strange Request limit reached" (`max_req`): https://shelly-forum.com/thread/22549-strange-request-limit-reached [MED/LOW]
- Shelly forum KB, battery devices not reachable: https://shelly-forum.com/lexikon/entry/272-battery-operated-shelly-why-not-reachable [MED]
- hex.pm `shelly` client docs (1 req/s per account, 429 on bursts, id normalisation): https://hex.pm/packages/shelly/0.3.0/files/lib/shelly.ex [MED]
- Simon Willison, SQLite WAL across Docker containers (April 2026): https://simonwillison.net/2026/Apr/7/sqlite-wal-docker-containers/ [MED; tested on Docker Desktop macOS only]
- Local computation (this session) of the SPEC's EMC formula at 0/21/30/40 degC [HIGH]
- bombur-infra skill (UID/bind mount, `CHANGE_ME` replacement, no `ports:`, deploy conventions) [HIGH, owner's own docs]
- httpx INFO request logging and exception URLs, OpenTelemetry HTTP URL span attributes, SQLite WAL read-only/`-shm` requirements, uPlot null/spanGaps behaviour: [PRIOR] standard behaviour, not re-fetched; confirm during implementation

---
*Pitfalls research for: Shelly Cloud polling climate logger with Open-Meteo, SQLite, public read-only UI on Docker/Bombur*
*Researched: 2026-10-31*
