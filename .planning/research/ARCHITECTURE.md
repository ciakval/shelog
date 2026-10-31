# Architecture Research

**Domain:** Cloud-polling sample-and-hold sensor logger (Shelly Cloud + Open-Meteo) with SQLite storage, read-side derivation, and a public read-only web UI; Docker Compose on a single VPS (Bombur)
**Researched:** 2026-10-31
**Confidence:** MEDIUM-HIGH (structure and SQLite/Compose behaviour HIGH; Shelly response shapes LOW until the `probe` step, so anything that depends on them is marked)

## Executive Recommendation

- **One Python package, one Docker image, two Compose services**: `poller` (sole writer, no network exposure) and `web` (read-only, on the `proxy` network). Both bind-mount `./data`.
- **SQLite in WAL mode is safe across the two containers** because they share one host kernel and the same bind-mounted directory. This is an inference from SQLite's WAL rules (same host, same `-shm` file), not something the SQLite docs say about containers, so spike-test it in the first deploy phase.
- **Store only facts, derive everything else at read time.** The schema is complete from day one (readings, outdoor, poll_log, raw_status, alert_state), so adding the UI later is purely additive code, with no migrations.
- **Split the code into a pure core (parsers, dedup rule, metrics, resampling, stats, alert rules) and thin IO shells** (poller loop, HTTP handlers). The pure core is where the unit tests live.
- **Ship the `web` service in the very first deploy** as a minimal `/api/health` plus placeholder page. `compose.yaml` is server-owned and only changes via `bombur-onboard`, so declaring both services once avoids re-onboarding later. It also exercises the Caddy/HTTPS/shared-WAL path while data is already being collected.

## Standard Architecture

### System Overview

```
                        INTERNET
   Shelly Cloud (v2 /devices/api/get)        Open-Meteo (forecast API, past_days)
          ^  1 req/60 s, <=1 req/s                  ^  every ~15-60 min, idempotent upsert
          |                                         |
+---------+-----------------------------------------+-----------------------------+
| compose project "shelog"  (/home/deploy/DOCKER/shelog)                          |
|                                                                                 |
|  +-----------------------------+                +----------------------------+  |
|  | service: poller             |                | service: web               |  |
|  | `shelog poll`               |                | `shelog serve`             |  |
|  | env_file: .env (SHELLY_*)   |                | env: non-secret vars only  |  |
|  | NO network exposure         |                | networks: default + proxy  |  |
|  |                             |                | container_name shelog-web  |  |
|  |  scheduler (single thread)  |                |                            |  |
|  |   |- shelly job (60 s)      |                |  HTTP handlers (sync)      |  |
|  |   |- weather job (15-60 m)  |                |   |- /api/devices          |  |
|  |   |- alerts check (/cycle)  |                |   |- /api/readings         |  |
|  |   |- housekeeping (daily):  |                |   |- /api/outdoor          |  |
|  |      backup, prune raw     |                |   |- /api/stats            |  |
|  |                             |                |   |- /api/health           |  |
|  |  WRITER (only one)          |                |   '- static/ (index.html,   |  |
|  |  connect_rw()               |                |        app.js, uPlot)      |  |
|  +--------------+--------------+                |  READER connect_ro()       |  |
|                 | write                         +--------------+-------------+  |
|                 v                                              | read           |
|        +-------------------------------------------------------v--------+       |
|        |  ./data/shelog.db  (+ -wal, -shm)   bind mount, rw in BOTH     |       |
|        |  WAL, single writer, many readers, user_version migrations     |       |
|        |  ./data/backup/  (daily VACUUM INTO)                           |       |
|        +----------------------------------------------------------------+       |
+----------------------------------------------------------+----------------------+
                                                           | proxy network
                                          Caddy (sites/shelog.caddy) -> shelog-web:8000
                                                           |
                                          https://shelog.remesh.cz   (public, read-only)
```

Direction of data: **Shelly/Open-Meteo -> poller -> SQLite -> web -> browser**. The web service never writes to SQLite and never calls Shelly. The poller never imports anything from the read side. The only channel between the two services is the database file.

### Component Responsibilities

| Component | Responsibility | Typical Implementation |
|-----------|----------------|------------------------|
| `config` | Env vars (secrets, intervals, lat/lon, thresholds) plus `devices.toml` (id, name, group, model, offsets). Immutable dataclasses. | `tomllib` + `os.environ`, safe dev defaults so `git clone && run` works without `.env` |
| `db` | Open connections, set PRAGMAs, apply migrations. Two factories: `connect_rw()` (poller only) and `connect_ro()` (web, CLI readers). | stdlib `sqlite3`; migrations as ordered SQL list keyed by `PRAGMA user_version`; no ORM |
| `shelly.client` | One HTTP call for all device ids. Owns the 1 req/s guard, timeouts, 429/Retry-After, and error classification. Returns the raw envelope or a typed error. | `httpx.Client`, hard minimum gap of ~1.1 s between any two requests, including retries |
| `shelly.parse` | Per-model pure functions: raw device status dict -> `Report(device_id, ts, ts_source, t_c, rh, battery, flood)`. | One function per model (`ht_gen1`, `ht_gen3`, `flood_gen1`), tested against committed sanitized fixtures from `probe` |
| `store` | Write-side: dedup decision, insert readings and raw, write poll_log. One transaction per poll cycle. | Pure `should_insert(prev, new)` plus thin SQL wrappers |
| `weather.openmeteo` | Fetch, parse (pure), upsert into `outdoor`. Handles the first-run backfill. | `httpx` GET, `timeformat=unixtime`, `INSERT ... ON CONFLICT DO UPDATE` |
| `poller` | Scheduler loop: due-time table for jobs, backoff/jitter, SIGTERM handling, advisory lock. | Single-threaded loop on `time.monotonic()` deadlines; `Event.wait(timeout)` as the interruptible sleep |
| `alerts` | Pure rules (state -> list of alert/recovery actions) plus an ntfy sender. State persisted in `alert_state` so restarts do not re-spam. | Rules evaluated from DB state at the end of each cycle; failures of ntfy never break the loop |
| `metrics` | Pure: dew point, absolute humidity, wood EMC (Hailwood-Horrobin). | Plain functions, `math` only, table-driven unit tests |
| `series` | Pure, generic over one step series `(ts[], val[])`: carry-in handling, forward-fill to a grid, step-preserving min/max downsample, time-weighted stats, gap marking. | Plain lists or arrays; no SQL, no HTTP |
| `query` | Read-side orchestration: SQL fetch (including carry-in row), apply calibration offsets, derive metrics per report, then call `series`. | Composes `db.connect_ro` + `metrics` + `series` |
| `web` | JSON API and static files. Thin: validate params, clamp ranges, call `query`, serialise. | Small framework with sync handlers (blocking sqlite3 belongs in a threadpool, not an event loop) |
| `cli` | `probe`, `poll`, `serve`, `export`, `calibrate`, `reparse`, `healthcheck`. All are entry points over the same package. | `argparse`, one `shelog` console script |

## Recommended Project Structure

```
shelog/
├── pyproject.toml
├── devices.toml                 # committed, baked into the image (see "Config placement")
├── Dockerfile  compose.yaml  .env.example  DEPLOYMENT.md
├── .github/workflows/deploy.yml
├── src/shelog/
│   ├── config.py
│   ├── db.py                    # connect_rw / connect_ro / migrate
│   ├── migrations/
│   │   └── 0001_init.sql        # complete v1 schema (see Data Model)
│   ├── shelly/
│   │   ├── client.py            # IO: httpx, rate guard, error classes
│   │   └── parse.py             # PURE: per-model parsers
│   ├── weather/
│   │   └── openmeteo.py         # IO fetch + PURE parse
│   ├── store.py                 # write-side SQL + PURE should_insert()
│   ├── poller.py                # scheduler loop (imports store, shelly, weather, alerts)
│   ├── alerts.py                # PURE rules + ntfy sender
│   ├── metrics.py               # PURE
│   ├── series.py                # PURE
│   ├── query.py                 # read-side (imports db.connect_ro, metrics, series)
│   ├── web/
│   │   ├── app.py
│   │   └── static/              # index.html, app.js, vendored uPlot (no CDN)
│   └── cli.py
└── tests/
    ├── fixtures/                # sanitized probe output, one per model, plus Open-Meteo sample
    └── test_*.py
```

### Structure Rationale

- **Import rules enforce the writer/reader split.** `poller.py` and `store.py` are write side. `query.py` and `web/` are read side. Nothing on the read side imports `store`. `connect_ro()` sets `PRAGMA query_only=ON` as a backstop.
- **`metrics.py` and `series.py` never import `db`.** They are exercised with literal lists in tests, so the formula and step-semantics tests need no database.
- **One package, one image.** The same image runs both services, so parsers, schema, and config can never drift between writer and reader.
- **Vendored uPlot, no CDN.** The page works offline, avoids a third-party dependency on a public page, and lets you set a strict CSP.

## Architectural Patterns

### Pattern 1: Single writer, many readers (WAL across containers)

**What:** `poller` is the only process that writes, runs migrations, and creates the DB file. `web` and CLI readers open `file:/app/data/shelog.db?mode=ro` with `busy_timeout` set.
**When to use:** Always here. SQLite WAL allows concurrent readers while one writer commits.
**Trade-offs and rules:**
- **Do not mount `./data` as `:ro` in `web`.** A WAL reader must be able to write the `-shm` file; SQLite docs say read-only WAL access works only if `-shm` and `-wal` already exist and are readable, or the directory allows creating them, and otherwise requires write access to `-shm`. Mount `./data` read-write, enforce read-only at the connection (`mode=ro` plus `query_only`).
- **Same UID in both containers.** Same image means same `USER`, so `-shm`/`-wal` created by the poller are writable by `web`. A UID mismatch produces "attempt to write a readonly database" on read-only queries.
- **Same host only.** SQLite: "All processes using a database must be on the same host computer; WAL does not work over a network filesystem." Fine on Bombur's local disk. In local dev keep the repo on the WSL ext4 filesystem, not under `/mnt/c`, and never use Docker Desktop style VM-mounted shares.
- **Both must see the same inode for `-shm`.** SQLite warns about processes seeing different files (its example is chroot). Two services bind-mounting the same host directory is fine; do not give one container a copy or a different mount. Verify once with a spike (poller writes in container A, web reads fresh data in container B within a second).
- **Short read transactions only.** Long-running readers block WAL checkpoints (SQLite documents this starvation). Per request: open, one or a few queries, fetch all, close. Never stream a cursor to a slow client; the CSV export reads fully first (rows are small).
- **The poller sets `journal_mode=WAL`** (persistent in the DB file), `synchronous=NORMAL`, `busy_timeout=5000`. The web also sets `busy_timeout`.
- **Web must tolerate "no DB yet".** At first `up -d` the web can start before the poller has created the file. `connect_ro()` failure -> `/api/health` returns `{"status":"starting"}` / HTTP 503, retried on the next request. Never have the web create or migrate the DB.

### Pattern 2: Complete schema first, additive-only afterwards

**What:** Migration `0001_init.sql` creates every table the full product needs, even those the poller does not yet fill (`outdoor` is empty until the weather job ships). Later changes are additive (`ALTER TABLE ... ADD COLUMN`, new tables) via `0002_*.sql`.
**When to use:** The constraint "poller in production before UI, no schema churn".
**Trade-offs:** A few unused tables for a while (free). The cost is thinking about the schema upfront, which is what this document does.
**Why UI work needs no schema work:**
1. Calibration offsets live in `devices.toml`, not the DB.
2. Derived metrics (dew point, absolute humidity, EMC) are computed at read time, never stored.
3. Group membership, display names, and the RH band live in config.
4. Anything the parser did not extract can be recovered from `raw_status` via `shelog reparse`.
5. Outdoor variables are rows, not columns (see Data Model).

### Pattern 3: Functional core, imperative shell

**What:** All decisions are pure functions over plain data. The shell fetches inputs, calls the function, and performs the result.
**Where:** `parse` (dict -> Report), `should_insert(prev, new) -> bool`, `alerts.evaluate(state, now) -> [Action]`, `series.*`, `metrics.*`.
**Trade-offs:** Slightly more plumbing, but the tests the project needs (fixtures, formula sanity, dedup rule, gap handling) run without network or SQLite, and the roadmap can plan "metrics" and "series" as independent, TDD-friendly work items.

### Pattern 4: Read pipeline (derive, then resample)

The read side is a fixed pipeline. Order matters:

```
SQL fetch per device:  rows in [from, to]  PLUS the carry-in row (last row with ts < from)
   -> apply calibration offsets (t_c + t_offset, rh + rh_offset)
   -> derive per report row: dew point, abs. humidity, EMC     # on exact (T, RH) pairs from the SAME report
   -> per metric: build step series (ts[], val[])
   -> mark gaps (see Gap semantics)
   -> series.resample / series.downsample_minmax / series.stats
   -> JSON (columnar arrays) or CSV
```

- **Derive before resample.** EMC is nonlinear in T and RH. Computing it from min T and min RH of a bucket produces a state that never existed. Compute on each report, then take min/max/time-weighted mean of the derived series.
- **Carry-in row is mandatory.** Without the last pre-`from` state, the left edge of every chart is blank until each device's first in-range report, which for a stable device can be hours.
- **Forward-fill grid (`resample=5m|15m|1h`):** for grid points `g_k = from + k*step`, value is the last row with `ts <= g_k`, via a single merge walk O(rows + grid). Never interpolate; never average raw samples per bucket.
- **Step-preserving min/max downsample (when a series has more than about 5k points):** pick a bucket width so output is at most ~5k points. Per bucket emit the carry-in state at bucket start, plus the rows holding the bucket min and max (in time order). A step plot through those points still shows every excursion. Apply per metric series after derivation (the argmin row for T differs from that for RH).
- **Time-weighted stats:** integrate the step function over covered time only; mean = integral / covered seconds; "% time outside RH band" integrates an indicator. Min/max include the carry-in state.
- **Honest sizing note:** 7 devices reporting on change (0.5 C / 0.5 %RH) produce on the order of 1-2k rows/day in total. 30 days is tens of thousands of rows, a few thousand per series. The `/api/readings` path is far below the 2 s acceptance bound with plain SQL and Python. Build raw output first; build downsampling only when the "all" range exceeds the point cap. No rollup tables, no materialized aggregates.

### Pattern 5: Gap semantics (outage vs silence)

**What:** There are two different reasons for a flat stretch in a sample-and-hold series, and they must not be conflated.
- **Poll outage** (poller down, cloud unreachable, auth rejected): the state is *unknown* for that interval. Detect from `poll_log`: consecutive successful shelly polls further apart than ~3x the poll interval. Forward-filling across it is a lie; draw a break and exclude it from time-weighted stats.
- **Device silence** (polls fine, device simply has not reported): with a 0.5 C / 0.5 %RH threshold a stable room legitimately produces no change for hours, so silence alone is *not* a gap for a short period. Only flag a break past the configurable threshold (default 6 h), and surface it in health ("no report > threshold").

SPEC.md only describes the second; the first is a necessary addition because the poller can be down while the cloud holds a stale last state. `query` computes outage intervals once per request with a window function over `poll_log` (about 1440 rows/day), and `series` consumes a list of `(start, end)` unknown intervals.

### Pattern 6: Single-threaded scheduler with a job table

**What:** The poller is one loop, no threads, no asyncio. Jobs have a `next_due` monotonic deadline: `shelly` (60 s, deadline advances by interval so it does not drift, plus small jitter), `weather` (15-60 min), `alerts` (after every shelly cycle), `housekeeping` (daily).
**When to use:** At 1 request/minute, concurrency is pure cost. Sequential execution makes the 1 req/s guarantee trivial and removes SQLite writer contention.
**Trade-offs:** A slow weather fetch delays the next Shelly poll by its duration. Mitigate with a hard 10 s timeout on weather and always running the shelly job first when both are due.
**Details:**
- Backoff on shelly errors: exponential with jitter, **cap at about 2-5 minutes**. The cloud keeps only last state, so every skipped poll risks a lost intermediate report; do not back off to 30 min.
- HTTP 429: honour `Retry-After`, otherwise back off. Treat as `error='rate_limited'` in the log.
- SIGTERM: set a stop `Event`, finish the current transaction, exit. `docker compose up -d` stops the old container during a deploy; a sub-minute gap is harmless for last-state-only cloud data.
- Take an `flock` on `./data/poller.lock` so a second poller on the same data dir refuses to start. (A laptop running the real key concurrently would still violate the account-wide 1 req/s limit and cannot be prevented in code; document: dev uses fixtures or `poll --once`.)

## Data Model (complete v1, no later churn)

Timestamps: **integer unix seconds, UTC, everywhere.** Time zone conversion is a presentation concern.

```sql
-- migrations/0001_init.sql  (PRAGMA user_version = 1)

-- Every poll attempt, shelly and weather. Explains every gap.
CREATE TABLE poll_log (
  id          INTEGER PRIMARY KEY,
  ts          INTEGER NOT NULL,                 -- attempt time
  kind        TEXT    NOT NULL DEFAULT 'shelly',-- 'shelly' | 'weather'
  ok          INTEGER NOT NULL,                 -- 0/1
  http_status INTEGER,
  error       TEXT,    -- COARSE CATEGORY ONLY: 'auth' | 'rate_limited' | 'timeout' | 'network' | 'http_5xx' | 'parse:<device>' ; NEVER raw exception text/URLs
  n_devices   INTEGER, -- devices present in response
  n_new       INTEGER, -- readings inserted this cycle
  latency_ms  INTEGER
);
CREATE INDEX poll_log_kind_ts ON poll_log(kind, ts);

-- Raw, deduplicated, uncalibrated device reports
CREATE TABLE reading (
  device_id TEXT    NOT NULL,
  ts        INTEGER NOT NULL,   -- report time (see ts_source)
  ts_source TEXT    NOT NULL,   -- 'device' | 'cloud' | 'poll'  (fixed by probe findings)
  t_c       REAL,
  rh        REAL,               -- NULL for Flood
  battery   REAL,
  flood     INTEGER,            -- Flood only
  polled_at INTEGER NOT NULL,   -- when WE first saw it (always poll time; insurance if ts semantics change)
  poll_id   INTEGER REFERENCES poll_log(id),
  source    TEXT    NOT NULL DEFAULT 'poll',    -- 'poll' | 'cloud_stats' (future backfill)
  PRIMARY KEY (device_id, ts, source)
) WITHOUT ROWID;

-- Raw per-device JSON, written only when a reading row is inserted (see rationale)
CREATE TABLE raw_status (
  device_id TEXT    NOT NULL,
  ts        INTEGER NOT NULL,
  source    TEXT    NOT NULL DEFAULT 'poll',
  body      TEXT    NOT NULL,
  PRIMARY KEY (device_id, ts, source)
) WITHOUT ROWID;

-- Outdoor weather, long format: adding a variable never changes the schema
CREATE TABLE outdoor (
  ts         INTEGER NOT NULL,  -- timestamp as returned by Open-Meteo (unixtime, UTC)
  variable   TEXT    NOT NULL,  -- 'temperature_2m' | 'relative_humidity_2m' | 'dew_point_2m' | 'surface_pressure' | 'precipitation' | ...
  value      REAL,
  interval_s INTEGER NOT NULL DEFAULT 3600,  -- aggregation window (precipitation = sum over the PRECEDING interval)
  fetched_at INTEGER NOT NULL,
  PRIMARY KEY (ts, variable)
) WITHOUT ROWID;

-- Alert dedup/recovery state that survives restarts
CREATE TABLE alert_state (
  key         TEXT PRIMARY KEY,   -- 'poll_down' | 'auth_rejected' | 'silent:<device_id>'
  since       INTEGER NOT NULL,
  notified_at INTEGER
);

-- Small key/value for provenance (e.g. weather lat/lon used, so a config change is visible)
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

Design notes:

- **`ts` semantics are the biggest unknown** (SPEC.md §3: `_updated`, `unixtime` may be 0 on sleeping devices, Gen3 `sys.unixtime`). Keeping `polled_at` in every row makes a wrong `ts` choice recoverable by `reparse` rather than a data loss. The `probe` phase must fix `ts_source` per model before the first production poll.
- **Dedup is mostly the primary key.** `INSERT OR IGNORE` on `(device_id, ts, source)` is the first line. A report whose `ts` advanced is a real report even if values are identical (it is evidence of liveness), so it is stored. Where a model has no reliable report time (`ts_source='poll'`), `should_insert` falls back to "values changed versus the last stored row". The poller loads the last row per device into memory at start (index lookup on the PK) and updates it on insert.
- **`raw_status` keyed like `reading` and written only on inserts** (about 1 KB x ~1-2k/day, roughly 1-2 MB/day) is a deliberate change from SPEC.md's per-poll raw JSON (1440 polls/day x several KB, prune after days). It keeps raw data for the whole life of the project, so `reparse` can always repair a parser mistake, and it needs no retention job. If probe shows the cloud-side timestamp only appears in non-new polls, the roadmap may add an env toggle that also keeps full poll envelopes for N days (additive, no schema change needed; store with a synthetic `source='envelope'`).
- **`WITHOUT ROWID` for `reading`/`outdoor`/`raw_status`** gives clustered `(device_id, ts)` range scans. `raw_status` bodies are small enough for this to be fine, and keeping them out of `reading` keeps the hot table narrow.
- **No `device` table.** Device identity is config; the DB stores `device_id` strings. Retired or renamed devices stay queryable through the id.
- **Open-Meteo `precipitation` is a preceding-interval sum, not an instantaneous value**; the `interval_s` column exists so the read side does not forward-fill rain as if it were a state.
- **`poll_log` keeps one row per attempt forever** (about 525k rows/year, trivial). Not pruning means gap analysis is always possible.

## Process Model and Compose Topology

**Decision: one image, two services.** Rationale versus a single combined process:

| Concern | Two services (recommended) | Single container / process |
|---------|---------------------------|---------------------------|
| Isolation | A web crash or deploy never touches the poller; UI bugs cannot break data collection (core value) | A UI exception, thread starvation, or `--workers N` (N pollers!) can break or duplicate collection |
| Secrets | `web` does not receive the Shelly key | Public-facing process holds the key |
| Complexity | Two services in compose; DB is the only interface | One process but threads/async coordination |
| WAL risk | Cross-container WAL, safe on one host | None |

Cross-container WAL is the only added risk and is mitigated by the rules in Pattern 1. A deploy restarts both services (shared `IMAGE_TAG`); the poller downtime is seconds.

```yaml
# compose.yaml (shape; server-owned, delivered via bombur-onboard)
services:
  poller:
    image: ghcr.io/${GITHUB_REPOSITORY:-ciakval/shelog}:${IMAGE_TAG:-latest}
    command: ["shelog", "poll"]
    restart: unless-stopped
    env_file: .env                      # SHELLY_AUTH_KEY, SHELLY_HOST, NTFY_TOPIC, LAT/LON ...
    volumes: ["./data:/app/data"]
    stop_grace_period: 20s
    # no container_name, no ports, default network only

  web:
    image: ghcr.io/${GITHUB_REPOSITORY:-ciakval/shelog}:${IMAGE_TAG:-latest}
    command: ["shelog", "serve", "--host", "0.0.0.0", "--port", "8000"]
    container_name: ${CONTAINER_NAME:-shelog-web}
    restart: unless-stopped
    environment:                        # EXPLICIT non-secret vars only; do NOT use env_file here
      DB_PATH: /app/data/shelog.db
      RH_BAND_LOW: ${RH_BAND_LOW:-40}
      RH_BAND_HIGH: ${RH_BAND_HIGH:-60}
      GAP_THRESHOLD_S: ${GAP_THRESHOLD_S:-21600}
    volumes: ["./data:/app/data"]       # rw on purpose (WAL -shm); read-only is enforced at the connection
    networks: [default, proxy]

networks:
  proxy:
    external: true
```

Notes:
- The Bombur template's single `web` service with `env_file: .env` is adapted: only the poller gets the secrets file. Compose interpolation of `${VAR}` still reads `.env`, so non-secret tuning values can live there.
- `bombur-deploy` runs `docker compose pull` then `up -d --remove-orphans`. Multi-service compose works with it unchanged.
- Caddy returns 502 until `shelog-web` exists, which is expected and harmless before the first deploy completes.
- Optional compose `healthcheck` for the poller: `shelog healthcheck` exits non-zero if the newest successful shelly `poll_log.ts` is older than ~5 minutes. Visible in `docker compose ps`; it does not auto-restart, it is for humans.
- Add a `.dockerignore` excluding `data/`, `.env`, `.git`. Run as non-root; `chown` the host `./data` to the container UID once at first deploy (known Bombur pitfall).

### Config placement

- **`devices.toml` committed in the repo and baked into the image.** Both services are always the same image tag, so poller and web can never disagree about devices or offsets. A calibration change is a commit plus deploy, which gives an audit trail, and since offsets apply at read time the change is retroactive for all history.
- Device IDs are not auth secrets, but the public API exposes only `name`, `group`, `model`, never the cloud id or host.
- **Secrets only in server `.env`** (`SHELLY_AUTH_KEY`, `SHELLY_HOST`, `NTFY_TOPIC`/token). The ntfy topic is effectively a password too.

## Data Flow

### Write path: one Shelly poll cycle

```
scheduler tick (monotonic deadline)
    |
    v
shelly.client.get_status(ids)          # enforces >=1.1 s gap, timeout, Retry-After
    |  raw envelope | typed error
    v
for each device: shelly.parse[model](raw) -> Report | ParseError     # PURE
    |
    v
store.should_insert(last_by_device[id], report)                       # PURE
    |
    v
BEGIN;  INSERT poll_log (ok, http_status, error_category, n_devices, n_new, latency_ms)
        INSERT OR IGNORE reading ...; INSERT OR IGNORE raw_status ...
COMMIT;                                # poll_log row is written for failures too
    |
    v
alerts.evaluate(db_state, now) -> actions -> ntfy POST (best effort)
    |
    v
sleep until next deadline (Event.wait; SIGTERM-interruptible)
```

A parse failure for one device does not discard the others: `ok=1`, `error='parse:<device>'`, the rest insert. A transport/auth failure writes `ok=0` and no readings.

### Write path: weather job

```
every 15-60 min:  GET forecast API  (past_days=2, forecast_days=0 or filter ts<=now,
                  hourly=temperature_2m,relative_humidity_2m,dew_point_2m,surface_pressure,precipitation,
                  timeformat=unixtime, timezone=GMT)
  -> parse (PURE) -> INSERT ... ON CONFLICT(ts,variable) DO UPDATE   # recent hours are revised
  -> poll_log row (kind='weather')
First run / empty table:  one request with past_days=92 (API maximum) covers backfill for a
logging start within the last ~3 months; older needs the separate Historical Weather API.
```

Open-Meteo facts (docs-verified): `past_days` 0-92; `forecast_days` 0-16; `timeformat=unixtime` gives UTC seconds; hourly temperature is instantaneous while precipitation is a preceding-hour sum; native 15-minute data exists only for Central Europe and North America (elsewhere interpolated). Recommendation: **hourly only for v1**; 15-minute is an upgrade that needs no schema change (`interval_s=900`), but mixing hourly and 15-min precipitation in one variable would be wrong, so use distinct variable names if you ever add it. Documented rate limits for free non-commercial use were not stated on the docs page, so keep the call rate low (the 15-60 min job is far below any sensible limit), and the non-commercial licence fits this personal project.

### Read path: chart request

```
browser GET /api/readings?from=&to=&devices=LR-1,LR-2&resample=none|5m|15m|1h&metrics=t,rh,emc,dew
    |
    v
web: validate + clamp (max range, max devices, min step)   -> 400 on abuse
    |
    v
query: connect_ro(); per device: carry-in row + range rows; outage intervals from poll_log;
       calibrate -> derive -> per-metric step series -> series.resample/downsample
    |
    v
JSON, columnar and per series: {"LR-1": {"t":[...], "temp":[...], "rh":[...], "emc":[...], "dew":[...]}, ...}
       "unknown":[[start,end],...]   # outage intervals; client breaks lines here
    |
    v
Cache-Control: public, max-age=30..60
client: uPlot with step paths (`uPlot.paths.stepped`), one chart per metric, synced cursor;
        different x arrays per device merged client-side (uPlot.join)
```

- **Outdoor** as `/api/outdoor?from=&to=&variables=` (or a block in `/api/readings`). Same carry-in rule, same grid options, rendered as extra series on the T / RH / dew-point charts. Precipitation is exposed as per-interval values, not forward-filled.
- **Derived metrics stay server-side** so the one unit-tested implementation feeds the charts, the stats table, and the CSV export. Do not duplicate EMC in JS.
- **Stats** (`/api/stats`): per device over the range, min / max / time-weighted mean of T, RH, EMC, % time outside RH band, last report time, battery.
- **Health** (`/api/health`): last successful poll time and age, per-device last-report age vs threshold, auth failure flag, outage summary, from `poll_log`/`reading`/`alert_state`. Exposes **error categories only**, never message text.

## Poller-First Deployment Path (no churn)

What must exist at first production deploy, and what is deferred:

| Present at first deploy (Phase "poller in production") | Added later (purely additive) |
|---|---|
| `0001_init.sql` complete schema | `0002+` only for genuinely new facts |
| `poller` service: shelly job, dedup, poll_log, raw_status, backoff, SIGTERM | weather job (fills the already existing `outdoor` table), alerts job |
| `web` service with `/api/health` and a one-line placeholder page | `/api/devices`, `/api/readings`, `/api/stats`, `/api/outdoor`, static UI |
| `compose.yaml` with BOTH services, Caddy site, `.env` | no `compose.yaml` change, hence no `bombur-onboard` re-run |
| Daily DB backup job | CSV export, calibration CLI, `reparse` |
| `devices.toml` with ids from `probe` | offsets filled in after the co-location calibration run |

Because `compose.yaml` is server-owned and only updated by `bombur-onboard`, **any later Compose change costs a manual onboard**. Declaring both services up front, and giving `web` the same image, makes the whole UI phase a pure image deploy through the normal `main` pipeline. If the roadmapper prefers a poller-only first deploy, the cost is one extra `bombur-onboard` run (it also rotates the deploy key) when `web` is added; acceptable but avoidable.

## Housekeeping and Backup

- **Backup is not "just copy the deploy directory" for SQLite in WAL.** Copying `shelog.db` alone while the poller runs can miss committed data still in `-wal` or capture an inconsistent set. The poller's daily housekeeping job does `VACUUM INTO './data/backup/shelog-YYYYMMDD.db'` (or the `sqlite3` backup API), keeping the last N (7-14). Copying `./data` then picks up a consistent snapshot. Size is tens of MB at most.
- No other pruning is needed: `reading`, `outdoor`, `raw_status`, `poll_log` all stay small for the life of the project (order of hundreds of MB would take decades).

## Alerts

- Evaluated in the poller after each cycle from DB state, so conditions are the same ones the health endpoint shows: `poll_down` (no successful poll > 1 h), `auth_rejected` (auth category on the latest poll), `silent:<id>` (no report > per-device threshold, with threshold at least a few multiples of the wake interval; tune from real data).
- `alert_state` provides: notify once on entry, optional re-notify after a long interval, send a recovery message on exit, and no repeat after restarts.
- **A dead poller cannot report its own death.** The poller crashing, the container being down, or the VPS being off are invisible to in-process alerts. Optional cheap fix: the poller pings an external dead-man's-switch URL (e.g. a healthchecks-style service) after each successful poll, configurable and off by default. Flag in the roadmap as a decision, not a requirement.

## Build Order (dependencies)

```
probe (needs real creds, local only)
   |  produces: field paths, ts semantics, response envelope, device ids, fixtures
   v
[1] shelly.parse + fixtures tests  ----------------------+
   |                                                      |
   v                                                      |
[2] db + 0001_init + store(should_insert) + shelly.client |   (all pure/testable parts built first)
   |                                                      |
   v                                                      v
[3] poller loop + poll_log + backoff + Dockerfile + compose(poller+web health) + CI  ==> DEPLOY #1 (collecting data)
   |
   +--> [4] alerts + backup job + healthcheck                      (reliability of unattended run; time-critical-ish)
   |
   +--> [5] weather job + backfill                                 (recoverable later via past_days / archive; not time-critical)
   |
   v
[6] metrics.py + series.py (pure, TDD)  -> query.py -> /api/*      (can start right after [3], needs real accumulated data to sanity check)
   |
   v
[7] static UI (uPlot, step paths, gaps, selection, stats table, health badge)
   |
   v
[8] CSV export, calibration CLI (needs a co-location window of raw data; offsets apply retroactively), reparse tool
   |
   (later) Real Time Events API investigation, cloud-stats backfill importer
```

Ordering rationale:
- **Probe is a hard prerequisite for everything.** Parsers, `ts` semantics, and even whether dedup can rely on a report timestamp depend on it. Nothing else on the write path can be finalised before it.
- **Deploy #1 includes the full schema** so the UI cannot force a migration afterwards.
- **Alerts (4) before weather (5):** once the poller is the only source of truth, silent failure is the main risk to the core value, whereas Open-Meteo can be backfilled afterwards (within the 92-day `past_days` window or via the historical API).
- **[6] can be developed in parallel with [4]/[5]** because it depends only on the schema and the `reading` rows already accumulating; metrics and series are pure and can start any time (the formula tests need no data).
- **Calibration offsets are not on the critical path**: raw is stored uncalibrated and the offsets apply at read time, so the co-location calibration can happen at any time, even after weeks of logging, with all history corrected retroactively. The calibration window must be in the stored data, which is a physical task, not a code dependency.

## Scaling Considerations

Not a scale problem; "what breaks first" for completeness:

| Scale | Architecture Adjustments |
|-------|--------------------------|
| Current (7 devices, 60 s poll, ~1-2k reports/day) | Single SQLite file, no caching layer, no rollups. Readings table reaches ~1M rows only after years |
| More devices / years of data | Still fine; add a covering index only if a query plan shows a scan. Shelly v2 handles up to 10 ids per call; >10 devices means chunked requests that must still respect the 1 req/s account limit |
| Public abuse of the unauthenticated API | Clamp range, device count, and minimum resample step in `web`; short `Cache-Control`; Caddy can add rate limits if ever needed |

First bottleneck realistically: **public API abuse** or an unbounded query, not data volume. Second: WAL growth from a stuck long reader (hence short read transactions).

## Anti-Patterns

### Anti-Pattern 1: Linear interpolation or bucket means on raw samples
**What people do:** Resample with `AVG()` per bucket or draw straight lines between reports.
**Why it's wrong:** The data is sample-and-hold with change-driven reports; averaging weights samples by report frequency and interpolation invents values that never existed.
**Do this instead:** Forward-fill to a grid; time-weighted integrals for means; min/max-preserving steps for downsampling.

### Anti-Pattern 2: Storing calibrated or derived values
**What people do:** Apply offsets or compute EMC at insert time.
**Why it's wrong:** A wrong offset or formula corrupts history permanently and cannot be undone.
**Do this instead:** Store raw; derive in `query` from config and pure functions.

### Anti-Pattern 3: Letting the web process poll or write
**What people do:** Start the poller as a background task in the web app's startup hook.
**Why it's wrong:** `--workers N` duplicates pollers, breaks the 1 req/s limit, and ties data collection to UI deploys and crashes.
**Do this instead:** Separate `poller` service, single writer, DB file as the only interface.

### Anti-Pattern 4: Mounting `./data` read-only in the web container, or using `immutable=1`
**What people do:** Try to enforce read-only at the mount.
**Why it's wrong:** WAL readers need to write `-shm`; `immutable=1` on a live database can return stale or inconsistent results.
**Do this instead:** Rw mount, `mode=ro` connection plus `PRAGMA query_only=ON`.

### Anti-Pattern 5: Leaking the Shelly auth key through logs and error text
**What people do:** Log or store `str(exception)` from the HTTP client.
**Why it's wrong:** Shelly's v2 endpoint takes `auth_key` in the query string (per SPEC.md); httpx exception messages and its INFO request logging contain the full URL. `poll_log.error` is read by the public `/api/health`.
**Do this instead:** Set the `httpx`/`httpcore` loggers to WARNING or higher, classify errors into a fixed category enum, store only the category, redact any URL in debug output. Check during `probe` whether the key can go in a header or body instead of the query string.

### Anti-Pattern 6: Copying a live WAL database as the backup
**What people do:** Rely on "backup = copy the deploy directory".
**Why it's wrong:** The `.db` file alone can be missing committed pages or be inconsistent.
**Do this instead:** Scheduled `VACUUM INTO` / backup API to `./data/backup/`.

### Anti-Pattern 7: Treating all flat stretches as the same
**What people do:** One gap threshold on device report time.
**Why it's wrong:** Stable rooms legitimately report rarely, while a poller outage silently forward-fills stale state.
**Do this instead:** Outage intervals from `poll_log` (unknown) plus a separate, longer device-silence threshold (suspicious but displayed).

### Anti-Pattern 8: Abstractions that this size does not need
**What people do:** ORM, async everywhere, message queue between poller and web, pre-aggregated rollup tables, a time-series DB.
**Why it's wrong:** Violates the minimal-footprint constraint; at tens of thousands of rows, plain SQL is faster than the machinery.
**Do this instead:** stdlib `sqlite3`, one small loop, one DB file.

## Integration Points

### External Services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| Shelly Cloud Control API v2 | `POST /v2/devices/api/get` with `{"ids":[...<=10], "select":["status"]}`, one call per cycle | 1 req/s account-wide; envelope and timestamps unverified until `probe`; key changes with account password -> auth category must surface loudly; host is account-specific and may be relocated |
| Open-Meteo forecast API | `GET` with `past_days`, `hourly=`, `timeformat=unixtime`, `timezone=GMT`, upsert | No key; non-commercial licence; recent hours get revised, so upsert not insert |
| ntfy.sh | `POST https://ntfy.sh/<topic>` | Topic acts like a secret; best-effort, never blocks the loop |
| Caddy | `reverse_proxy shelog-web:8000` over `proxy` network, site file created by `bombur-onboard` | Only `web` joins `proxy`; no `ports:` anywhere |
| GitHub Actions -> GHCR -> `bombur-deploy` | Image tagged by SHA; CI cannot ship compose changes | Compose changes need `bombur-onboard` |

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| `poller` <-> `web` | Only via `shelog.db` file (WAL) | No sockets, no shared memory, no queue |
| `shelly.client` -> `shelly.parse` | Raw dict per device | Parsers never do IO |
| `store` -> `alerts` | Alerts read DB state, not in-memory variables | Same truth as `/api/health` |
| `query` -> `metrics`/`series` | Plain lists in, plain lists out | Pure functions are the test surface |
| `cli export` / `calibrate` <-> `query` | Reuse the same `query` module as the API | CSV and charts cannot disagree |
| devices.toml -> everything | Loaded at process start | Same file in both services via the one image |

## Key Risks This Architecture Must Absorb

1. **Report-timestamp semantics unknown** (LOW confidence): resolved in `probe`, hedged by `polled_at` and `reparse`.
2. **Cross-container WAL**: expected to work; verify in the first deploy phase with an explicit writer-in-A / reader-in-B smoke test.
3. **Lost intermediate reports** (cloud holds last state): inherent to polling; mitigated by 60 s default and capped backoff; Real Time Events API is the later cure.

## Sources

- SQLite WAL documentation (https://sqlite.org/wal.html): same-host requirement, read-only WAL access rules (3.22.0+), checkpoint starvation by long readers, persistent WAL mode, shared-memory/chroot warning (HIGH)
- Open-Meteo forecast API docs (https://open-meteo.com/en/docs): `past_days` 0-92, `forecast_days` 0-16, `minutely_15` regional availability, `timeformat=unixtime`, preceding-hour precipitation semantics (HIGH for those facts; rate limits and archive-API delay NOT stated on the page, MEDIUM from background knowledge)
- Bombur conventions: `/home/ciakval/.claude/skills/bombur-infra/SKILL.md`, `assets/compose.yaml`, `/home/ciakval/GIT/bombur-deploy/bin/bombur-deploy` (runs `docker compose pull` and `up -d --remove-orphans`; compose is server-owned) (HIGH)
- Project context: `/home/ciakval/GIT/shelog/.planning/PROJECT.md`, `/home/ciakval/GIT/shelog/SPEC.md` §3-§7 (Shelly field paths are explicitly unverified) (HIGH for requirements, LOW for Shelly response shapes)
- `uPlot.join` and stepped paths for aligning series with different x arrays: from background knowledge of uPlot, not verified here (MEDIUM; confirm in UI phase)

---
*Architecture research for: cloud-polling sample-and-hold sensor logger*
*Researched: 2026-10-31*
