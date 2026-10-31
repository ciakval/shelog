<!-- GSD:project-start source:PROJECT.md -->

## Project

**shelog (shelly-climate-log)**

A small service on the owner's VPS (Bombur) that continuously logs temperature and humidity from 7 battery-powered Shelly sensors in an apartment under renovation — four lying on the exposed subfloor of the living room/kitchen, one Shelly Flood in its centre, and two in reference rooms that still have wooden floors — together with outdoor weather for the same location. A public read-only web page at `shelog.remesh.cz` shows long, fine-grained, overlaid history and wood-flooring metrics, so the owner can judge with data whether the subfloor is dry enough for a new wooden floor.

Full original specification: `SPEC.md` (repo root). This document supersedes it where they differ.

**Core Value:** **Collect a complete, trustworthy, fine-grained (~5 min) climate history from every sensor, starting as soon as possible.** Every day not logged is lost forever; visualization and analysis can be built later on top of good data.

### Constraints

- **Deployment**: Bombur standard pipeline (Docker image tagged by SHA, `compose.yaml`, `bombur-onboard`, reusable `ciakval/bombur-deploy` workflow) — owner's chosen infrastructure; replaces the systemd plan in SPEC §4
- **Rate limit**: never exceed 1 request/second to Shelly Cloud — account-wide API limit
- **Secrets**: Shelly auth key and host only in server `.env`; never in git, logs, or the public UI/API
- **Timeline**: poller must be collecting in production before UI work starts — data is time-critical
- **Footprint**: minimal dependencies, no heavyweight stacks — owner preference
- **Public UI**: everything served is world-readable; must not leak secrets or device auth details

<!-- GSD:project-end -->

<!-- GSD:stack-start source:research/STACK.md -->

## Technology Stack

## Headline Decisions

## Recommended Stack

### Core Technologies

| Technology | Version | Purpose | Why Recommended | Confidence |
|------------|---------|---------|-----------------|------------|
| Python | 3.13 (`python:3.13-slim`, verified 3.13.18 + SQLite 3.46.1 on Debian trixie) | Runtime | Matches Bombur convention. SQLite 3.46 gives WAL, STRICT tables, `unixepoch()`, UPSERT, window functions. 3.14 is available but buys nothing here. | HIGH |
| uv | 0.13.6 (pin the Docker image tag) | Env/deps/lockfile/build | Bombur standard. Pin `ghcr.io/astral-sh/uv:0.13.6`, not `:latest` (uv docs call pinning best practice; the Bombur template uses `:latest`, deviate). | HIGH |
| httpx2 | 2.16.0 (`>=2.16,<3`) | Sync HTTP client for Shelly, Open-Meteo, ntfy | Pydantic-maintained continuation of httpx (first release 2026-05-11, now releasing every 1-2 weeks). Original `httpx` last stable release was 0.28.1 on 2024-12-06; only `1.0.devN` pre-releases since; the httpx2 README says httpx has "limited activity". Starlette >=1.2 made `TestClient` build on httpx2 and deprecates plain httpx, so httpx2 is already in the dependency tree for tests. One HTTP lib, strict timeouts, `MockTransport` for tests. Deps: anyio, httpcore2, idna, truststore. Smoke-tested in `python:3.13-slim` (TLS to Open-Meteo OK, MockTransport OK). | MEDIUM-HIGH (young package, but same code lineage and a credible maintainer) |
| Starlette | 1.10.0 (`>=1.10,<2`) | Read-only JSON API + static files | The API is ~5 GET routes and `StaticFiles`. Starlette's only hard dependency is `anyio`. Starlette 1.0 shipped 2026-03-22 and is now release-stable; the 1.0 removals (`on_startup`, route decorators, old `TemplateResponse`) do not matter when you write `routes=[...]` and `lifespan` from day one. Plain `def` handlers run in the anyio threadpool, which is exactly right for blocking `sqlite3` calls. | HIGH |
| uvicorn | 0.54.0 (`>=0.54,<1`), **without** the `[standard]` extra | ASGI server | Deps: click, h11 (pure Python). Skipping uvloop/httptools/watchfiles/websockets avoids compiled wheels; traffic is a handful of requests per minute behind Caddy. Run `--workers 1`. | HIGH |
| SQLite (stdlib `sqlite3`) | 3.46.1 (bundled in the image) | Storage | Single-writer (poller), many-reader (web), WAL, gigabytes of headroom for this volume (7 devices x a few hundred reports/day). No ORM, no migrations framework: a `schema_version` pragma (`PRAGMA user_version`) plus an ordered list of SQL strings is enough. | HIGH |
| uPlot | 1.6.32 (released 2025-03-14; MIT; 51 KB min / 22 KB gzip, verified) | Browser charts | Fits every UI requirement natively: `uPlot.paths.stepped()` for sample-and-hold, `null` points break the line for gaps, `cursor.sync` for linked crosshairs across charts, `bands` for the RH target band, multiple scales/axes, drag-to-zoom + `setScale`, `uPlot.join()` to merge per-series tables onto one x axis, `uPlot.tzDate` for local-time axes. Handles 100k+ points. Last release is 19 months old; the library is mature/feature-complete, not abandoned. | HIGH |
| zoneinfo / `tomllib` / `argparse` | stdlib | TZ, config, CLI | No `click`/`typer`/`pydantic-settings`/`python-dotenv` needed. | HIGH |

### Database

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| SQLite file `data/shelog.sqlite3` | WAL mode | Readings, weather, poll_log, alert_state, optional raw_status | See pragmas below. Back up with `Connection.backup()` / `VACUUM INTO`, not by copying a live WAL file. |

# poller (the only writer)

# web (reader): do NOT use mode=ro

### Infrastructure (Docker / Bombur)

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| Docker image `python:3.13-slim` | trixie-based | Single image, entrypoint `shelog` with subcommands `poll`, `serve`, `probe`, `export`, `calibrate`, `healthcheck` | One build, one tag per SHA, two roles. Matches Bombur conventions (non-root, `VERSION`/`BUILD_ID` args, stdout logging). |
| Docker Compose | per Bombur template | Two services: `poller` (default network only), `web` (default + `proxy`, `container_name: ${CONTAINER_NAME:-shelog-web}`) | See "Process model" below. `bombur-deploy` runs `docker compose pull` + `up -d --remove-orphans` (verified in `~/GIT/bombur-deploy/bin/bombur-deploy`), which handles multi-service files without changes. |
| Caddy (existing) | n/a | TLS, `encode zstd gzip`, upstream `shelog-web:8000` | Keep compression in Caddy; do not add gzip middleware to the app. |
| GitHub Actions -> GHCR -> `ciakval/bombur-deploy@v1` | per `assets/deploy.yml` | CI/CD | Add a `test` job (ruff + pytest) that `build-and-push` depends on. |

### Supporting Libraries

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| (none at runtime beyond the three above) | | | The default is to add nothing. |
| `opentelemetry-*` | latest at the time | OTLP traces/metrics | **Defer.** Bombur convention says "assume OTLP" but also "must work when the endpoint is unset", and no collector exists yet. Adding `opentelemetry-distro` + exporter pulls ~15 packages including protobuf, which contradicts the owner's minimal-dependency preference. When a collector appears, add it as an optional dependency group and switch on via the entrypoint-script pattern in `references/observability.md`. Until then: stdlib `logging` to stdout + `/healthz`. |

### Development Tools

| Tool | Version | Purpose | Notes |
|------|---------|---------|-------|
| pytest | 9.2.0 | Tests | Plain sync tests. No `pytest-asyncio` (handlers are sync `def`). No `pytest-httpx`/`respx`: use `httpx2.MockTransport` injected into the client (the client factory takes a `transport=` argument). Use Starlette `TestClient` for API tests and an in-memory/tmp-path SQLite. |
| ruff | 0.17.3 | Lint + format | One tool for both. Select `E,F,I,UP,B,SIM,RUF` plus `S` for the public-facing code. Format with `ruff format`. |
| mypy | 2.4.1 | Type checking (optional, `strict` on the pure `metrics`/`parsers` modules first) | `ty` (Astral) is still 0.0.x (0.0.95) and `pyright` needs Node; revisit `ty` once it is 1.0-ish. Don't block the poller phase on type-checker setup. |
| uv `dependency-groups` | | `dev` group | `uv sync --frozen --no-dev` in the image; `uv run pytest` / `uv run ruff check` in CI. |
| uv_build | 0.13.x | Build backend | Default `uv init --package` backend; pin to the same minor as uv. Use `src/shelog/` layout. |

## Process Model: two Compose services from one image

- **Core Value is "never lose a sample".** A web crash, a bad request handler, an uvicorn restart, or a UI-only bug must not stop collection. With a poller thread inside the web process, any of those take down data collection; a silently dead thread (unhandled exception) is the classic failure and `restart: unless-stopped` cannot see it.
- **"Poller first, UI later"** (PROJECT.md constraint) maps naturally: phase 1 ships a compose file with only `poller`; adding `web` later is a pure addition. The thread design would force restructuring the entrypoint.
- **Least privilege on a public service.** Only `web` joins `proxy`. `web` gets an explicit `environment:` list (no `SHELLY_AUTH_KEY`, no `NTFY_TOPIC`) instead of `env_file: .env`, so a bug or SSRF-style leak in the public process cannot expose the Shelly key. (This deviates from the Bombur template's `env_file: .env` on `web`, deliberately.)
- The Bombur convention already says workers are separate services in the app's own compose project.
- Every deploy restarts both services (same image tag changes). The poller loop must therefore be restart-safe: on start it just resumes; the dedup rule makes re-polling idempotent; a 60 s polling gap during deploy is acceptable because the cloud holds the last state.
- `/api/health` must not depend on talking to the poller. It reads `poll_log`/`reading`/`alert_state` from SQLite. The database is the only integration point between the two services (no sockets, no queues).
- Changing `compose.yaml` requires re-running `bombur-onboard` (compose is server-owned), per the infra skill.

## Scheduling (poller)

- One process, one thread. A small `due: dict[str, float]` of `monotonic` deadlines for three jobs: `shelly` (default 60 s, +/- jitter), `weather` (hourly), `housekeeping/alerts` (every few minutes, plus a daily `Connection.backup()` snapshot to `data/backup/`).
- Sleep with `threading.Event.wait(timeout)`, set by a `SIGTERM`/`SIGINT` handler, so `docker stop` exits cleanly within the grace period (Compose sends SIGTERM; PID 1 in a container ignores default signal handlers unless installed, so install them and `exec` the process; use the exec-form `command:`).
- Use `time.monotonic()` for scheduling intervals and `time.time()` only for stamping data. Compute the next deadline from the previous deadline (not from "now") to avoid drift, but clamp so a long outage does not cause a burst of catch-up polls.
- Shelly guard: `min_gap = 1.1 s` between any two Shelly requests (matters only if the v1 per-device fallback is used; the v2 call is one request per cycle). Honour `429` and `Retry-After`; exponential backoff with cap (e.g. 60 s -> 15 min) on 5xx/timeouts/network errors; an auth failure (401 / error body) is a distinct state that raises an immediate alert and slows polling to a safe rate rather than hammering with a bad key.
- Timeouts: `httpx2.Timeout(15.0, connect=5.0)`; a hung weather call must not starve the Shelly poll. If that ever matters, run weather in its own thread (still no asyncio).
- Wrap every job in a try/except that logs and records to `poll_log`; only `KeyboardInterrupt`/shutdown exits the loop. Never let ntfy being down break polling.

## Configuration

- `config/devices.toml` (device id, name, group, model, `t_offset`, `rh_offset`) lives in the repo and is baked into the image (`/app/config/devices.toml`, path overridable by `SHELOG_DEVICES_FILE`). Rationale: deploys are push-to-`main`; offsets are data-adjacent and should be versioned; the Bombur `.env` is for secrets. Device IDs are not usable without the auth key, but if the repo is public the IDs are public; if the owner dislikes that, bind-mount the file from the server instead.
- Environment (documented in `.env.example`, secrets as `CHANGE_ME`): `SHELLY_HOST`, `SHELLY_AUTH_KEY` (secret), `SHELOG_POLL_INTERVAL` (default 60), `SHELOG_LAT`, `SHELOG_LON` (and optional `SHELOG_ELEVATION`; keep them in `.env`, not git, and round to 2 decimals: the home location stays private and Open-Meteo's grid is coarser anyway), `NTFY_TOPIC` / `NTFY_TOKEN` (secrets), `NTFY_SERVER` (default `https://ntfy.sh`), `SHELOG_KEEP_RAW` (0/1), `SHELOG_DB`, `LOG_LEVEL`, plus the `OTEL_*` vars from the Bombur template.
- Validate everything at startup and exit non-zero with a clear message (Compose will restart-loop visibly rather than run misconfigured). Redact `auth_key` from every log line and exception: httpx2 puts query strings into exception messages and request logs, and Shelly requires the key as a **query parameter** (`?auth_key=`; the v2 docs mention no header alternative), so configure the `httpx2`/`httpcore2` loggers at WARNING and scrub URLs before logging errors.
- Safe dev defaults in code so `git clone && uv run shelog serve` works with no `.env` (empty DB, no poller).

## Open-Meteo (outdoor weather)

| Purpose | Endpoint | Key params |
|---------|----------|-----------|
| Live + backfill (use this for both) | `https://api.open-meteo.com/v1/forecast` | `latitude`, `longitude`, `hourly=temperature_2m,relative_humidity_2m,dew_point_2m,surface_pressure,precipitation`, `past_days=<0..92>`, `forecast_days=1`, `timeformat=unixtime` (add `elevation=<m>` if the flat's floor differs notably from the DEM) |
| Optional finer live data | same endpoint | `minutely_15=<same vars>&past_minutely_15=8&forecast_minutely_15=0` (native 15-min only in Central Europe/North America, interpolated from hourly elsewhere) |
| Long-term / older than 92 days | `https://archive-api.open-meteo.com/v1/archive` | `start_date`, `end_date` (`yyyy-mm-dd`), same `hourly` and `timeformat`. ERA5-based, 5-day delay on pure ERA5 though the default "best match" returned data up to the current hour when probed. Different grid cell than the forecast API (probe returned 50.0879/14.4755 vs 50.0775/14.4272 for the same request), so expect small offsets at the seam. |
| (Not recommended) | `https://historical-forecast-api.open-meteo.com/v1/forecast` | Stitched first hours of past model runs from 2022 on; the docs say it is "not suitable for long time series due to model version changes". |
- **One endpoint covers the project's backfill.** Logging starts now and `past_days` goes back 92 days, so the first run does `past_days=92` (or "days since the first reading + 1", capped), later runs `past_days=2`. Use the archive API only if a backfill older than 92 days is ever wanted (YAGNI).
- **Upsert, don't insert-once.** Models revise recent hours. Key the `weather` table by `(ts)` (add `source` to the key only if archive data is ever mixed in) and `INSERT ... ON CONFLICT DO UPDATE`. Re-fetching the trailing 48 h every hour makes outages self-healing.
- **Store hourly as the canonical outdoor series.** Hourly is what backfill gives you; 15-minute `precipitation` is a 15-min sum while hourly is a 1-hour sum, so mixing them in one column is a bug. If 15-min is added later, give it its own table or an `interval_s` column.
- Timestamps with `timeformat=unixtime` are integer seconds UTC (verified). Without it, they are ISO strings in the (default GMT) timezone and the DST pitfall in the docs applies.
- Parse defensively: arrays may contain `null` (missing hour); response `hourly.time` and each variable array are parallel lists.
- Free tier: non-commercial use, 600 calls/min, 5,000/h, 10,000/day (monthly cap not enforced); the app makes ~1 call/hour. **Licence is CC BY 4.0: attribution is required.** Put "Weather data by Open-Meteo.com" (linked) in the public page footer (and CSV header comment).
- Humidity is integer %, temperature to 0.1 C. Don't over-interpret sub-degree differences against sensor data; outdoor is model data, not a measurement at the flat.

## Web API and frontend

- **Starlette `routes=[...]`** with `Route("/api/devices")`, `/api/readings`, `/api/stats`, `/api/health`, `/healthz`, `/api/export.csv` plus `Mount("/", StaticFiles(directory=..., html=True))`. Plain `def` endpoints (threadpool). Parse and bound query params by hand (`from`/`to` ints, `devices` list, `resample` in an enum); reject unbounded ranges. No Pydantic, no OpenAPI docs endpoint on a public service.
- Response size: downsample server-side to <= ~5k points/series (step-preserving min/max per bucket, as in SPEC section 6). Send arrays-of-arrays (columnar, uPlot's native `AlignedData` shape) instead of arrays of objects: ~3x smaller JSON and zero client reshaping. Caddy compresses it.
- Cache headers: `Cache-Control: public, max-age=30` on `/api/*` is plenty and protects SQLite from a public page being hammered; `StaticFiles` provides ETag/Last-Modified.
- Single-page frontend: vanilla ES modules, no bundler, no framework. Files: `index.html`, `app.js`, `style.css`, `vendor/uPlot.iife.min.js`, `vendor/uPlot.min.css`.
- **uPlot delivery: vendor the two files** (53 KB total) into `src/shelog/static/vendor/uplot-1.6.32/` with a README noting the version and source. Reasons: public page with no third-party runtime request (privacy, availability, no CDN supply-chain surprise), works offline in dev, still "no build step". If a CDN is preferred anyway, pin the exact version and use SRI:
- uPlot specifics the UI phase must know: it wants one shared, ascending x array per chart (server resamples with forward-fill onto a common grid, or the client uses `uPlot.join`); gaps are `null` (set `spanGaps: false`) so the server/client must insert `null` where the report gap exceeds the threshold; stepped paths via `paths: uPlot.paths.stepped({align: 1})`; wheel-zoom/pan is not built in (drag-zoom is) and needs a ~30-line plugin (uPlot ships a demo); sync charts with `cursor: {sync: {key: "shelog"}}`; use `tzDate` with `Europe/Prague` (or `Intl` resolved zone) for axis labels; watch the dark/light theme for series colors.

## Alerting (ntfy.sh)

- Topic on the public server is effectively a password ("the topic is essentially a password" per ntfy docs): use a long random topic in `.env` (`NTFY_TOPIC`), never in git or the UI. If the owner has an account/self-hosted instance, send `Authorization: Bearer tk_...` (`NTFY_TOKEN`).
- Keep an `alert_state(key, active, since, last_sent)` table; send on **transitions** (raised, cleared) with an optional slow reminder, not on every failed poll. Alert keys: `poll_failing` (> 1 h), `auth_rejected` (immediate), `device_silent:<id>` (threshold per device, longer for battery sleepers), `db_error`. This also makes the poller restart-safe (no repeat alert after every deploy).
- Alert delivery failure is logged and retried next cycle; it must never raise out of the loop. Public-server rate limits were not verified, but alert volume here is a few messages a day.
- Add a once-daily or startup "shelog online" heartbeat only if the owner wants dead-man's-switch behaviour (no alert can fire if the container itself is dead; consider a healthchecks-style external ping later).

## Installation

# project bootstrap

# day to day

# vendor uPlot (one time; commit the two files)

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| httpx2 | plain `httpx` 0.28.1 | Works today and is API-identical in spirit, but last stable release was Dec 2024 and Starlette now deprecates it for `TestClient`. Acceptable fallback if httpx2 misbehaves; the code surface is small so swapping is a one-line import change. |
| httpx2 | stdlib `urllib.request` | If the owner wants literally zero HTTP dependency. Costs: hand-rolled timeouts/error mapping, no `MockTransport` (inject a fake `fetch` callable instead). Defensible, but httpx2 is already pulled in by Starlette's test tooling, so the saving is mostly at runtime (4 small packages). |
| Starlette + uvicorn | FastAPI 0.162.0 | If request bodies, auth, or OpenAPI docs become needed. Adds pydantic + pydantic-core (compiled) for five read-only GETs. FastAPI is Starlette plus validation, which this API doesn't need. |
| Starlette + uvicorn | stdlib `http.server.ThreadingHTTPServer` | Zero dependencies and workable behind Caddy, but no static-file caching headers/ETag, no timeouts by default, HTTP/1.0 unless configured, and the stdlib docs say it is not for production. The saved deps (starlette, uvicorn, anyio, click, h11) don't justify the hand-rolled HTTP surface on a public endpoint. |
| Starlette + uvicorn | Litestar 2.25.0, Flask + waitress, Bottle | Nothing here needs their features; Litestar is heavier, Flask adds Werkzeug/Jinja/Click/etc. |
| Separate `poller` + `web` services | Poller as a thread/lifespan task in the web process | One container, one log stream, simplest compose. Acceptable only if the owner values operational simplicity over fault isolation; then run the loop as a daemon thread started from Starlette `lifespan`, add a thread-liveness check to `/healthz`, and accept that web restarts interrupt polling. Not recommended given the Core Value. |
| Hand-written loop | APScheduler 3.11.3, Celery, cron + `poll --once` | cron/`--once` is tempting but loses in-process backoff state, jitter and the sub-minute cadence (cron min granularity is 1 min and startup cost per run). |
| uPlot | Chart.js 4.6.1 | If the owner prefers a friendlier API and accepts needing `chartjs-adapter-*` for time axes and `chartjs-plugin-zoom` for zoom/pan (more files, more deps) and slower rendering past ~10k points. Fine at <= 5k downsampled points. |
| uPlot | Apache ECharts 6.1.0 | Best built-in dataZoom/tooltip/step-line/area-band support, but ~1 MB and a big API; overkill for a "minimal" owner. Reasonable if hand-writing the uPlot wheel-zoom/tooltip plugins becomes annoying. |
| uPlot | Plotly (`plotly.js-dist-min` 4.2.0) | Several MB, slow with many traces; listed as "acceptable" in SPEC only as a fallback. |
| uPlot | dygraphs 2.2.2, lightweight-charts 5.2.1 | dygraphs is dated and lacks gap/step/band ergonomics; lightweight-charts is finance-oriented (candles, single price scale). |
| Plain Open-Meteo HTTP | `openmeteo-requests` 1.7.5 | Only if switching to its FlatBuffers wire format for very large pulls. Irrelevant at ~170 rows/call. |
| `sqlite3` + raw SQL | SQLAlchemy / aiosqlite 0.22.1 / sqlite-utils | An ORM adds nothing for four tables; `aiosqlite` only matters for async handlers, which we avoid via sync `def` routes. |
| Raw `dataclass` config | pydantic-settings, dynaconf | Only if config grows well beyond ~15 keys. |

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `pandas` / `numpy` for resampling | 30-100 MB of wheels for a forward-fill over a few thousand rows; step-preserving min/max bucketing is clearer in 40 lines of Python and trivially unit-testable | Plain Python generators over `sqlite3.Row` |
| `uvicorn[standard]`, `gunicorn`, multiple workers | Compiled extras and prefork complexity for negligible traffic; multiple workers multiply SQLite connections for no benefit | `uvicorn` plain, `--workers 1` |
| `file:...?mode=ro` or `:ro` bind mount for the web's DB | WAL readers need to open/create `-shm`; fails unpredictably when sidecars don't exist | rw mount of the `data/` directory + `PRAGMA query_only=ON` |
| Mounting the `.sqlite3` file alone into containers | `-wal`/`-shm` live beside it; a single-file mount breaks WAL | Mount the `./data` directory |
| `cp data/shelog.sqlite3` as backup | Copy of a live WAL database can be inconsistent/miss `-wal` content | `sqlite3.Connection.backup()` or `VACUUM INTO 'data/backup/..'` from the poller (daily), then Bombur's "copy the deploy dir" is safe |
| `docker compose` `ports:` on `web` | Bypasses Caddy/HTTPS and the DOCKER-USER firewall (Bombur rule) | `proxy` network + Caddy `reverse_proxy shelog-web:8000` |
| `env_file: .env` on the public `web` service | Puts the Shelly auth key in the process environment of the world-facing container | Explicit `environment:` list for `web` |
| Sending Shelly `auth_key` anywhere it can be logged | It is a query parameter; default exception/log output includes the URL | Redact in a central helper; log device ids and HTTP status only |
| Linear interpolation / bucket means of raw samples | Values are sample-and-hold; interpolation invents data (SPEC section 2) | Forward-fill + `uPlot.paths.stepped`, time-weighted means |
| `datetime.utcnow()` / naive datetimes / local-time storage | Deprecated and DST-prone | Integer unix seconds UTC everywhere; convert only at the UI edge |
| Open-Meteo `archive` and `forecast` series mixed silently | Different grid cells/models give seam offsets | One endpoint for the routine series; tag `source` if you ever splice |
| Grafana/Influx/Prometheus client libs in the app | Out of scope per PROJECT.md; adds deps | `/api/health` JSON; optional OTLP later |
| Polling the Shelly v1 per-device endpoint in a tight loop | Burns the account-wide 1 req/s budget | One v2 `devices/api/get` for all ids (max 10 per call) |

## Stack Patterns by Variant

- Fall back to v1 `POST /device/status` per device with >= 1.1 s spacing (7 devices = ~8 s per cycle; keep the interval >= 30 s).
- Because the poller is a sync loop with an explicit min-gap, this is a parser/fetcher swap, not an architecture change.
- Add `wsproto` via `httpx2[ws]` or the `websockets` package inside the `poller` service only; keep SQLite as the integration point. Poller design (single writer, dedup on report time) already fits.
- Use the thread-in-lifespan variant described in Alternatives; keep the `db.py` single-writer discipline (poller thread owns the write connection, request handlers use per-request `query_only` connections).
- Add Caddy-level caching or rate limiting before touching the app; keep `Cache-Control: max-age` on API responses.

## Version Compatibility

| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| starlette 1.10.0 | anyio 4.15.1; Python >=3.11 | Only hard dependency is `anyio` (plus `typing-extensions` below 3.13). |
| starlette `TestClient` (>=1.2) | httpx2 >=2.0 | httpx2 is preferred, httpx supported but deprecated. We already depend on httpx2 at runtime so no extra test dependency. Verified working together in a scratch venv (Python 3.14) and in `python:3.13-slim`. |
| uvicorn 0.54.0 | h11, click | No extras. Python >=3.10. |
| httpx2 2.16.0 | httpcore2 2.16.0 (exact pin), anyio >=4.10, idna >=3.18, truststore >=0.10 | TLS trust via the OS store (`truststore`); `python:3.13-slim` has `/etc/ssl/certs/ca-certificates.crt` (verified). Pin `httpx2<3` and let `uv.lock` freeze exact versions. |
| ruff 0.17.3, pytest 9.2.0, mypy 2.4.1 | Python 3.13 | All current as of 2026-10-27/-31. |
| uPlot 1.6.32 | Any evergreen browser | `uPlot.iife.min.js` exposes global `uPlot`; no module build in `dist/` needed. |
| SQLite 3.46.1 (image) | WAL, STRICT, UPSERT, `unixepoch()`, `RETURNING` | All available (>= 3.38 needed for `unixepoch`). Dev machines with older system SQLite should test via `uv run` on the pinned Python. |

## Open Items for Later Phases

- **Shelly response shapes (v2, Gen1 and Gen3), which timestamp marks the real report time, `online` flag semantics:** unverified; the v2 doc page shows only a Gen2 example and does not mention `_updated`. This is what `probe` + committed fixtures must settle before parsers are written. (LOW confidence on anything about field paths until then.)
- Whether `bombur-deploy`'s forced-command flow needs anything special for a second service in `compose.yaml` (read of the script suggests no: `pull` + `up -d --remove-orphans`).
- OTLP: revisit when a collector exists on Bombur.
- ntfy public-server rate limits were not verified (the "Limitations" section was not reachable); irrelevant at this alert volume.

## Sources

- PyPI JSON API (queried 2026-10-31): httpx 0.28.1 (2024-12-06; only 1.0.devN since), httpx2 2.16.0 (2026-10-21; first release 2026-05-11), starlette 1.10.0 (2026-10-24), uvicorn 0.54.0 (2026-09-25), fastapi 0.162.0, litestar 2.25.0, pytest 9.2.0 (2026-10-27), ruff 0.17.3 (2026-10-27), mypy 2.4.1, ty 0.0.95, uv 0.13.6, uv-build 0.13.6, openmeteo-requests 1.7.5, apscheduler 3.11.3, aiosqlite 0.22.1: HIGH
- npm registry (2026-10-31): uplot 1.6.32 (2025-03-14), chart.js 4.6.1, echarts 6.1.0, dygraphs 2.2.2, lightweight-charts 5.2.1, plotly.js-dist-min 4.2.0: HIGH
- https://github.com/pydantic/httpx2 README (relationship to httpx, "limited activity", BSD-3): MEDIUM-HIGH
- https://raw.githubusercontent.com/Kludex/starlette/main/docs/release-notes.md and docs/testclient.md (1.0.0 on 2026-03-22, removals, TestClient on httpx2): HIGH
- https://open-meteo.com/en/docs, /en/docs/historical-weather-api, /en/docs/historical-forecast-api, /en/pricing (endpoints, variables, `past_days` 0-92, limits, CC BY 4.0, non-commercial): HIGH; plus live calls to api.open-meteo.com and archive-api.open-meteo.com on 2026-10-31 confirming response shapes with `timeformat=unixtime`: HIGH
- https://shelly-api-docs.shelly.cloud/cloud-control-api/communication-v2 (`POST /v2/devices/api/get`: `ids` 1-10, `select`, `pick`, `auth_key` query param, 1 req/s, array response with `id,type,code,gen,online`): HIGH for the envelope, nothing verified for per-model status fields
- https://docs.ntfy.sh/publish/ (POST/PUT, JSON publishing at server root, Bearer tokens, topic-as-password): HIGH for what is cited
- https://docs.astral.sh/uv/guides/integration/docker/ (pin uv, cache mounts, `--no-editable` multi-stage): HIGH
- https://github.com/leeoniya/uPlot (features: stepped, bands, sync, multi-axis; MIT) plus inspection of the downloaded 1.6.32 bundle for `join`, `sync`, `tzDate`, `stepped` symbols and computed size/SRI: HIGH
- Docker run of `python:3.13-slim` (Python 3.13.18, SQLite 3.46.1, CA bundle present, httpx2 + Starlette smoke test): HIGH
- `bombur-infra` skill (SKILL.md, references/dockerfiles.md, observability.md, assets/compose.yaml) and `~/GIT/bombur-deploy/bin/bombur-deploy`: deployment conventions: HIGH

<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->

## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->

## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->

## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->

## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-fast` for a trivial task inline, with no subagents and no PLAN.md
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->

<!-- GSD:profile-start -->

## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
