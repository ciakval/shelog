# Phase 2: Collection Live on Bombur - Research

**Researched:** 2026-11-01
**Domain:** Docker/Compose deployment on the Bombur pipeline, a Starlette read-only health service, stdlib alert/ping/backup/import code on top of the Phase 1 poller
**Confidence:** HIGH (the deploy tooling, Docker image, compose file, SQLite behaviour, ntfy publish and the import SQL were read or executed this session; remaining gaps are server-side facts only the owner can see, see Assumptions Log)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Plan order is strictly deploy-first. Wave 1 = everything needed for the first production deploy: Dockerfile, `.dockerignore`, `compose.yaml` (both `poller` and `web` already listed, because compose is server-owned and changing it means re-onboarding), CI workflow (test job gating build-and-push -> reusable bombur-deploy), a minimal `web` service with `/healthz` and `/api/health`, and the owner onboarding checklist. Alerts, dead-man's switch, backups, import and docs follow in later waves and ship as ordinary redeploys (no compose change).
- **D-02:** Everything that later waves need from compose (env vars passed to each service, mounts, stop grace period) must be decided in wave 1 so `bombur-onboard` runs once. The `poller` uses `env_file: .env`; `web` gets an explicit `environment:` list with no `SHELLY_*`, no `NTFY_*`, no healthchecks URL.
- **D-03:** Owner actions (protect `main`, `bombur-onboard shelog 8000`, fill server `.env`, copy `devices.toml` to the server, subscribe ntfy topic, healthchecks.io account, approve reboot test) are presented as a copy-pasteable checklist at a checkpoint; Claude never changes shared server config, Caddy or GitHub secrets itself.
- **D-04:** Starlette + uvicorn (no `[standard]` extra), as in the stack research, used only by `shelog serve`. The poller and the rest of the runtime stay stdlib-only (Phase 1 replaced httpx2 with urllib; do not reintroduce httpx2 at runtime). Port 8000, container name `${CONTAINER_NAME:-shelog-web}`, `--workers 1`, plain `def` handlers.
- **D-05:** `web` opens SQLite read-write-capable but sets `PRAGMA query_only=ON` (no `mode=ro`), mounts the whole `./data` directory, never the db file alone.
- **D-06:** `/api/health` returns coarse status only: last successful poll time, per-device silent yes/no keyed by device *name* (never id), auth state (`ok`/`rejected`/`unknown`), overall status. No error text, ids, MACs, coordinates, raw_status. `Cache-Control: public, max-age=30`.
- **D-07:** One image (`python:3.13-slim`, pinned uv image tag, non-root user, `VERSION`/`BUILD_ID` build args), entrypoint `shelog`, subcommands `poll` and `serve`. Explicit `WORKDIR` and explicit `SHELOG_DB_PATH`/`SHELOG_DEVICES_FILE` so nothing depends on an implicit cwd (review IN-05). `devices.toml` is bind-mounted read-only from the server deploy dir (repo is public; file is gitignored). Exec-form command so SIGTERM reaches the process; `stop_grace_period` long enough for a bounded in-flight request (review WR-02).
- **D-08:** Container UID must be able to write the bind-mounted `./data` (Bombur pitfall): document a one-time `chown` or match the deploy user's UID.
- **D-09:** Production starts on an empty DB as soon as it deploys. Then the local poller is stopped (Claude does this once production is confirmed polling) and the owner copies the local `data/shelog.db` to the server; a new `shelog import <path>` command merges readings into the production DB with the same dedup rule (insert only when the report time is new for that device; never overwrite), plus poll_log rows, in one transaction, idempotent on re-run, runnable inside the poller container (`docker compose exec poller shelog import ...` or `run --rm`). Import reports counts only.
- **D-10:** Never run the local and production pollers for longer than needed together (shared 1 req/s account budget): stop the local one as soon as production poll_log shows successful cycles.
- **D-11:** ntfy on the public `ntfy.sh` with a long random topic in the server `.env` (`NTFY_TOPIC`; optional `NTFY_TOKEN`, `NTFY_SERVER`). Alerts on transitions only via the existing `alert_state` table: `poll_failing` (> 1 h of failed polls), `auth_rejected` (immediate), `device_silent:<name>` (per-device `max_silence_s` from devices.toml, sensible default), `battery_low:<name>` (threshold Claude's discretion, ~20 %), each with a recovery message. Messages carry device names and categories only. Delivery failure is logged and retried next cycle; it can never raise out of the poll loop or block a poll.
- **D-12:** External dead-man's switch (ROB-01 pulled forward, owner approved): poller pings a healthchecks.io check URL (`HEALTHCHECK_PING_URL` in server `.env`, secret) after each successful cycle (rate-limited, e.g. at most once per 5 min), with a short timeout; ping failures never affect polling. healthchecks.io notifies the owner if pings stop (owner configures its ntfy/email integration).
- **D-13:** Verification forces at least the auth-rejected alert and its recovery (e.g. with a dummy key in a scratch DB / local run), per success criterion 4.
- **D-14:** Daily consistent snapshot by the poller via `Connection.backup()` (or `VACUUM INTO`) into `data/backup/`, keeping a small rotation (e.g. 7 daily). Verified by opening the snapshot and running `PRAGMA integrity_check`.

### Claude's Discretion
- Exact plan split beyond "wave 1 = first deploy", module names (`web.py`, `alerts.py`, `backup.py`, `importer.py`), housekeeping cadence inside the poller loop, battery threshold, default silence threshold, backup retention count, health JSON field names.
- Fixes from the Phase 1 code review (WR-01..06, IN-01, IN-03) are being applied before planning; IN-05 readiness items are covered by D-07.

### Deferred Ideas (OUT OF SCOPE)
- OTLP/OpenTelemetry export (no collector on Bombur yet).
- Staging environment (`shelog-test`).
- UI, weather, resampling, stats, CSV export (Phases 3-5).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| OPS-01 | `poller` + `web` from one image, GH Actions -> GHCR -> bombur-deploy on push to `main`, tests gate the build | Dockerfile (verified build), compose.yaml (verified `docker compose config`), CI workflow, "Bombur onboarding mechanics" |
| OPS-02 | Data survives redeploy/restart/reboot (db inside bind-mounted `./data`) | UID/ownership section, `restart: unless-stopped`, verified clean SIGTERM + WAL checkpoint, verification map |
| OPS-03 | `web` never receives the Shelly key and reads the DB while the poller writes | Config split (serve must not call `load_settings`), verified cross-container `query_only` read |
| OPS-04 | Consistent daily backup | Backup section (verified `backup()` and `VACUUM INTO` under concurrent writes) |
| OPS-05 | ntfy alerts + recoveries (poll failing, auth rejected, device silent, battery low) | ntfy section (verified JSON publish), `alert_state` state machine |
| OPS-06 | DEPLOYMENT.md / README | Owner checklist content, day-2 commands, restore procedure |
| DATA-05 | `GET /api/health` coarse status | Starlette section, shared `health.evaluate()` |
| ROB-01 (pulled forward) | healthchecks.io dead-man's switch | healthchecks ping section |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- Never read the real secrets file in any form (a hook blocks it); verify commands must not contain the literal text of that filename (`.env.example` is fine). No Shelly requests from research; any later drill uses a dummy key and a non-Shelly host.
- Repo is public: no real device IDs, MACs, IPs, SSIDs, coordinates in committed files. `devices.toml` and `.env` are gitignored; the real-id/key scans in `tests/test_fixture_safety.py` cover every committable file (so the new Dockerfile, compose, workflow, DEPLOYMENT.md, docs are scanned too).
- Never exceed 1 request/second to Shelly. Stdlib-only runtime; owner approved starlette + uvicorn for `shelog serve` only. Logs: device names, categories, counts only; `poll_log.error` is a closed category enum.
- Compose is server-owned: `web` gets an explicit `environment:` list (no `env_file`), no `ports:`, only `web` joins `proxy`.
- Integer unix seconds UTC everywhere; sample-and-hold semantics (not relevant to this phase beyond silence maths).
- Ruff `S` rules are on (`select = ["E","F","I","UP","B","SIM","RUF","DTZ","S"]`); `pytest` runs with `filterwarnings = ["error"]`, so an unclosed `sqlite3.Connection` (ResourceWarning) fails a test. Always close connections.

## Summary

The Bombur pipeline is simple and fully readable: `bombur-onboard shelog 8000` tars `./compose.yaml` and `./.env.example` plus a fresh deploy key to the server; `bombur-add-app` creates `/home/deploy/DOCKER/shelog/{,data}` (owner `deploy`, `data` mode 755), installs the compose file, creates `.env` **once** from `.env.example` (only lines ending exactly `=CHANGE_ME` get a random alphanumeric value; it then appends `GITHUB_REPOSITORY=` and `CONTAINER_NAME=shelog-web`), installs the forced-command key, and writes the Caddy site `shelog.remesh.cz -> shelog-web:8000`. Every deploy only does `IMAGE_TAG=<sha>` in `.env`, `docker compose pull`, `docker compose up -d --remove-orphans`. Because `.env` is never regenerated, **every variable any later wave reads must already be in `.env.example` at first onboarding**, and everything is optional-with-loud-warning in code so a missing line never crashes the poller.

I prototyped the whole container design in a scratch copy: a two-stage image (uv build stage, slim runtime without uv) is 184 MB, includes the SQL migrations in the wheel, runs as non-root, `shelog poll` exits 0 in 0.27 s on `docker stop` (so PID 1 signal handling works), a second container with the same UID reads the live WAL database through the same bind mount with `PRAGMA query_only=ON` (and gets "attempt to write a readonly database" on a write), and `uvicorn.run(app, log_config=None, ...)` also exits 0 in 0.4 s on SIGTERM. A clean checkout runs the full suite green with the two real-id/key scans skipping cleanly (392 passed, 2 skipped), so CI needs no special handling.

The one real code obstacle: `cli.main` calls `load_settings()` (requires `SHELLY_AUTH_KEY`/`SHELLY_HOST`) before dispatching, so `shelog serve` in the key-less `web` container would exit 2. Settings loading must be split per subcommand (see Pitfall 1). Second subtlety: the import's dedup rule must be primary-key based ("skip if `(device_id, ts, source)` exists"), not "ts newer than the last stored", otherwise local history older than production's first reading would be dropped.

**Primary recommendation:** Ship wave 1 as: Dockerfile + `.dockerignore` + final `compose.yaml` + complete `.env.example` + CI (test job -> build -> reusable deploy with `health_path: /healthz`) + `serve` with `/healthz` and `/api/health` on a key-free config path; then the owner checklist; alerts/ping/backup/import/docs ship afterwards as plain redeploys.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Shelly polling, dedup, storage | Poller service (backend, sole writer) | SQLite | Unchanged from Phase 1; fault-isolated from the public process |
| Alert evaluation + ntfy send | Poller service (housekeeping after each cycle) | `alert_state` table | Needs `NTFY_*` secrets, which `web` must never get |
| Dead-man's switch ping | Poller service | healthchecks.io (external) | An external party is the only thing that can notice a dead VPS/poller |
| Health status computation | Shared pure module `health.py` (reads SQLite) | `web` (serves it), `alerts.py` (reuses it) | One definition of "silent / auth state / stale" for the endpoint and the alerts |
| `/healthz`, `/api/health` | `web` service (read-only, public) | Caddy (TLS, compression) | OPS-03: no Shelly key in this process |
| Daily snapshot | Poller service | `data/backup/` on the bind mount | The poller already owns the write connection and the schedule |
| One-off import | CLI subcommand run inside the poller container | SQLite | Same code/image, no extra service |
| TLS, domain, routing | Caddy (server-owned) | `proxy` Docker network | Created by `bombur-add-app`, nothing in the repo |

## Bombur onboarding mechanics (verified by reading the scripts)

[VERIFIED: ~/GIT/bombur-deploy/sbin/bombur-add-app, bin/bombur-onboard, bin/bombur-deploy, .github/workflows/deploy.yml; all opened this session]

| Fact | Evidence (verbatim) | Consequence |
|------|--------------------|-------------|
| Onboard sends only `compose.yaml` and `.env.example` from the repo root, plus a new key | `for f in compose.yaml .env.example; do if [ -f "$f" ]; then cp "$f" "$T/"; fi; done` | Run from the repo root; files need not be committed. `devices.toml` is NOT sent: the owner places it |
| Deploy dir and data dir ownership | `install -d -o $DEPLOY_USER -g $DEPLOY_USER -m 755 "$D/data"` | `data` is `deploy:deploy` 755. The container UID must equal deploy's UID (or `chown`) to write it |
| `.env` is created once, never overwritten | `if [ ! -f "$D/.env" ]; then` ... | Later `.env.example` edits never reach the server; owner must add new vars by hand. Put ALL variables in the first `.env.example` |
| Only lines ending exactly `=CHANGE_ME` are randomized | `*=CHANGE_ME) echo "${line%%=*}=$(openssl rand -base64 48 | tr -d '/+=\n')" ;;` | `NTFY_TOPIC=CHANGE_ME` becomes a 60-64 char alphanumeric (valid ntfy topic). A commented `# X=CHANGE_ME` is also rewritten (harmless, avoid). Use `SET_ME` for `SHELLY_AUTH_KEY`/`SHELLY_HOST`/`HEALTHCHECK_PING_URL` |
| Two keys are appended after the example's content | `printf 'GITHUB_REPOSITORY=%s\nCONTAINER_NAME=%s-web\n' "$REPO" "$APP" >> "$D/.env"` | Do NOT define `GITHUB_REPOSITORY`/`CONTAINER_NAME` in `.env.example` (avoids duplicates) |
| Each deploy edits only `IMAGE_TAG` and runs pull + up | `docker compose pull --quiet` / `docker compose up -d --remove-orphans` | No `down`; both services are recreated when the tag changes; compose interpolation uses the deploy-dir `.env` |
| Caddy site is generated | `printf '%s.%s {\n\treverse_proxy %s-web:%s\n}\n' "$APP" "$DOMAIN" "$APP" "$PORT"` | `container_name` of web MUST resolve to `shelog-web` on port 8000 and the service must join `proxy` |
| Reusable workflow takes `health_path` and polls `url + health_path` up to 20 x 3 s | inputs `url`, `health_path` in deploy.yml | Use `url: https://shelog.remesh.cz` and `health_path: /healthz` |
| Deploy dir is `750 deploy:deploy` | `install -d ... -m 750 "$DEPLOY_HOME/DOCKER" "$D"` | Owner places `devices.toml` with `sudo install -o deploy -g deploy -m 640`; the container reads it only if its UID is deploy's UID (see UID section) |

**Onboard prerequisites** (from `bombur-onboard`): `gh` authenticated for the repo (`gh repo view` must resolve; repo `ciakval/shelog` exists and is PUBLIC [VERIFIED: `gh repo view` this session]), SSH as `ciakval` to port 10022, `main` protected (currently NOT protected [VERIFIED: gh api returned "Branch not protected"]; the `production` environment only accepts protected branches).

### UID / ownership decision (D-08)

Recommended: run both services as the deploy user's numeric id via compose `user: "${APP_UID:-1000}:${APP_GID:-1000}"`, with `APP_UID`/`APP_GID` documented in `.env.example` (compose reads the same `.env` for interpolation, the same mechanism `GITHUB_REPOSITORY` already uses). Owner step 0: `ssh -p 10022 ciakval@bombur.remesh.cz id deploy`, put the numbers in the server `.env`. Rationale: `data/` (755, deploy-owned) is then writable, the deploy user can still `scp` an import file into it, `devices.toml` (640 deploy) is readable, and no `chown` of a deploy-owned tree is needed. Fallback (D-08 alternative): leave UID 1000 and `sudo chown 1000:1000 data`, `chmod 644 devices.toml`. Both services must use the SAME UID, otherwise the reader cannot create/read `-shm` [verified same-UID case only]. The deploy user's real UID is unknown here (Assumption A1). The image creates `app` (1000) but any numeric `user:` works: Python needs no passwd entry and nothing writes to `$HOME`.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| python:3.13-slim | 3.13 | runtime base | Bombur convention; matches `.python-version` [VERIFIED: file read] |
| ghcr.io/astral-sh/uv | 0.13.6 (pin the tag) | build-stage dependency installer | Image tag exists [VERIFIED: `docker manifest inspect ghcr.io/astral-sh/uv:0.13.6`]; matches `uv_build>=0.13.6,<0.14` in pyproject. Local uv is 0.12.19, irrelevant for the image |
| starlette | >=1.10.0 (lock: 1.10.0) | `serve` routes | Owner-approved (D-04). Pure Python, first release 2018, 206 releases [VERIFIED: PyPI JSON] |
| uvicorn (no extra) | >=0.54.0 (lock: 0.54.0) | ASGI server | Owner-approved (D-04). First release 2017 [VERIFIED: PyPI JSON] |
| transitive (lock) | anyio 4.16.0, click 8.5.0, h11 0.16.0, idna 3.20, typing-extensions 4.16.0 | starlette/uvicorn deps | All pure Python; resolved by `uv add starlette uvicorn` in a scratch copy [VERIFIED: uv output this session] |

### Supporting (dev group only)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| httpx2 | >=2.16 (dev group) | required by `starlette.testclient.TestClient` | Starlette 1.10's TestClient raises "requires the httpx2 package" without it [VERIFIED: ran it]. Dev group only; NOT in `[project].dependencies`; does not violate D-04 (runtime). Adds httpcore2, truststore. Alternative if the owner objects: test handlers by calling the ASGI app directly or by running uvicorn in a thread and fetching with urllib |

**Installation:**
```bash
uv add starlette uvicorn          # runtime
uv add --dev httpx2               # TestClient only
```

## Package Legitimacy Audit

`package-legitimacy check` returned SUS for every package, but only for `too-new` (latest release date within weeks, as expected for actively released packages) and `unknown-downloads` (the seam has no download feed for PyPI) and `no-repository` (anyio metadata). These are artefacts, not red flags: all are long-established (ages below from PyPI first-upload dates). starlette and uvicorn are already owner-approved in CONTEXT D-04, so the planner needs no extra `checkpoint:human-verify` for them; `uv.lock` hashes pin what is installed. `httpx2` is the only genuinely new name (first release 2026-05-11 per project stack notes, repo github.com/pydantic/httpx2) and is dev-only; keep it flagged.

| Package | Registry | Age (first upload) | Downloads | Source Repo | Verdict | Disposition |
|---------|----------|-----|-----------|-------------|---------|-------------|
| starlette | PyPI | 2018-06 (206 releases) | n/a | github.com/Kludex/starlette | SUS (too-new, unknown-downloads: artefact) | Approved by owner (D-04) |
| uvicorn | PyPI | 2017-06 (204 releases) | n/a | github.com/Kludex/uvicorn | SUS (unknown-downloads: artefact) | Approved by owner (D-04) |
| anyio | PyPI | 2018-10 | n/a | none in metadata | SUS (artefact) | Transitive, locked |
| click | PyPI | 2014-04 | n/a | github.com/pallets/click | SUS (artefact) | Transitive, locked |
| h11 | PyPI | 2016-05 | n/a | github.com/python-hyper/h11 | SUS (artefact) | Transitive, locked |
| idna | PyPI | 2013-07 | n/a | github.com/kjd/idna | SUS (artefact) | Transitive, locked |
| httpx2 | PyPI | 2026-05 | n/a | github.com/pydantic/httpx2 | SUS (too-new) | Dev group only; planner adds `checkpoint:human-verify` before `uv add --dev httpx2` or uses the no-dependency test route |

**Packages removed due to SLOP:** none. **Flagged SUS:** all of the above (see note); `httpx2` is the one to confirm with the owner.

## Architecture Patterns

### System Architecture Diagram

```
 git push main
      |
 GitHub Actions: [test: uv sync --locked; ruff check; ruff format --check; pytest]
      | needs: test
 [build-and-push -> ghcr.io/ciakval/shelog:<sha>] --needs--> [deploy-production -> ssh deploy@bombur "deploy <sha>"]
                                                                   | bombur-deploy: IMAGE_TAG=<sha>; compose pull; up -d
                                                                   v
  /home/deploy/DOCKER/shelog/ {compose.yaml, .env, devices.toml, data/}
        |                                   |
   service poller  (default net)       service web (default + proxy, container shelog-web)
   env_file .env (key, NTFY, ping URL)  explicit environment: only (no secrets)
   user APP_UID, stop_grace 30s         user APP_UID (same), healthcheck /healthz
        |  writes (sole writer, WAL)         ^ reads, PRAGMA query_only=ON
        |                                    |
        +------------ ./data/shelog.db (+ -wal/-shm, poller.lock, backup/, import/) ------+
        |
        | each cycle: Shelly all_status -> write_cycle -> housekeeping:
        |    health.evaluate() -> alerts (ntfy JSON POST) ; ping (healthchecks.io, <= 1 per 5 min) ; daily backup()
        v
   ntfy.sh (phone)            hc-ping.com (owner notified if pings stop)

 Internet -> Caddy (shelog.remesh.cz, auto-TLS) -> proxy net -> shelog-web:8000 -> /healthz, /api/health
```

### Recommended Project Structure
```
Dockerfile  .dockerignore  compose.yaml  .env.example  DEPLOYMENT.md  README.md
.github/workflows/deploy.yml
src/shelog/
  config.py      # split: common (db path, devices file, log level) vs poller-only (Shelly key/host, NTFY_*, HEALTHCHECK_PING_URL)
  health.py      # NEW pure: evaluate(con, devices, now) -> status dict (shared by web + alerts)
  web.py         # NEW create_app(db_path, devices, clock) + serve(); sync def handlers
  alerts.py      # NEW state machine over alert_state + ntfy sender
  notify.py      # NEW (or inside alerts) ntfy + healthchecks senders on transport.send
  backup.py      # NEW daily snapshot + rotation; also `shelog backup --out PATH`
  importer.py    # NEW `shelog import PATH`
  cli.py         # add serve, import, backup; settings loaded per subcommand
```

### Pattern 1: Image (two-stage, verified build)
**What:** build stage uses uv to create `/app/.venv` with `--no-editable`; runtime stage copies only the venv (no uv, no build tools), sets explicit paths, drops to non-root, exec-form entrypoint.
**Example (built and run this session, 184 MB):**
```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
ARG VERSION=dev
ARG BUILD_ID=unknown
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    SHELOG_DB_PATH=/app/data/shelog.db SHELOG_DEVICES_FILE=/app/config/devices.toml \
    VERSION=${VERSION} BUILD_ID=${BUILD_ID}
RUN useradd --system --uid 1000 --no-create-home app \
    && mkdir -p /app/data /app/config && chown app:app /app/data
COPY --from=build /app/.venv /app/.venv
WORKDIR /app
USER app
EXPOSE 8000
ENTRYPOINT ["shelog"]
CMD ["poll"]
```
Verified in the built image: `from shelog.db import _migrations` finds the migration (so uv_build packages the `.sql`), package is under `/app/.venv/lib/python3.13/site-packages/shelog`, `id` = uid 1000, cwd `/app`. `.dockerignore`: `.git .github .env .env.* !.env.example data/ .venv/ __pycache__/ *.db *.db-wal *.db-shm .planning/ tests/ devices.toml` (the bombur template list plus the repo's secret/data files; `tests/` excluded because the test job runs in CI, not in the image).

### Pattern 2: compose.yaml (validated with `docker compose config`; bind-mount failure mode exercised)
**What:** one YAML anchor for the shared bits, `poller` on `env_file: .env`, `web` with an explicit `environment:`, devices file via long-syntax bind with `create_host_path: false`.
```yaml
x-shelog: &shelog
  image: ghcr.io/${GITHUB_REPOSITORY:-ciakval/shelog}:${IMAGE_TAG:-latest}
  restart: unless-stopped
  user: "${APP_UID:-1000}:${APP_GID:-1000}"
  volumes:
    - ./data:/app/data
    - type: bind
      source: ./devices.toml
      target: /app/config/devices.toml
      read_only: true
      bind:
        create_host_path: false   # missing file = loud error, not an empty root-owned directory

services:
  poller:
    <<: *shelog
    command: ["poll"]
    env_file: .env
    environment:
      SHELOG_DB_PATH: /app/data/shelog.db
      SHELOG_DEVICES_FILE: /app/config/devices.toml
    stop_grace_period: 30s
  web:
    <<: *shelog
    container_name: ${CONTAINER_NAME:-shelog-web}
    command: ["serve"]
    environment:                      # explicit list: NO SHELLY_*, NTFY_*, HEALTHCHECK_*
      SHELOG_DB_PATH: /app/data/shelog.db
      SHELOG_DEVICES_FILE: /app/config/devices.toml
      SHELOG_LOG_LEVEL: ${SHELOG_LOG_LEVEL:-INFO}
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
      interval: 30s
      timeout: 5s
      retries: 3
    networks: [default, proxy]

networks:
  proxy:
    external: true
```
Verified: `docker compose config` renders (merge key works, `user: 1001:1000` from `.env`); with `devices.toml` absent `docker compose up --no-start` fails with `invalid mount config for type "bind": bind source path does not exist` (so a forgotten devices file fails the deploy visibly in CI). Do not mount `/app/config` as a directory created by Docker. `stop_grace_period: 30s` covers the 15 s Shelly deadline (WR-02). The poller sets `SHELOG_*` explicitly in `environment:` (these win over `.env` values from `env_file`, so a stale path in `.env` cannot redirect the DB). Optional OTEL env lines from the template may be added but are inert.

### Pattern 3: CI workflow
Template (`assets/deploy.yml`) + a `test` job; `build-and-push` gets `needs: test`; deploy job unchanged except `url: https://shelog.remesh.cz` and `health_path: /healthz`.
```yaml
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v10      # latest release v10.3.3 [VERIFIED: gh api]; major tag existence ASSUMED
        with: { version: "0.13.6" }
      - run: uv sync --locked
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run pytest
```
Verified locally in a clean `git archive` checkout with its own `git init` and no `devices.toml`/key: `392 passed, 2 skipped` (`SKIPPED ... devices.toml not present`, `... no auth key configured`); ruff check and format clean. **Do not set `SHELOG_REQUIRE_REAL_ID_SCAN` in CI**; local enforcement (`SHELOG_REQUIRE_REAL_ID_SCAN=1` plus the owner's devices file) stays unchanged. Keep `concurrency` group and `fetch-depth: 0` from the template. Branch protection on `main` (owner) makes "failing test blocks the deploy" real for PRs; the `needs:` chain makes it real for pushes. Action majors: template uses checkout@v6, buildx@v3, login@v4, metadata@v6, build-push@v7; checkout v7.0.1 and build-push v7.4.0 are the newest releases [VERIFIED: gh api]; staying on the template's majors is fine.

### Pattern 4: Settings per subcommand (key-free `serve`)
`cli.main` today does `settings = load_settings(); devices = load_devices(...)` for every subcommand and `setup_logging(level, [settings.auth_key.reveal()])`. `load_settings` calls `_require(merged, "SHELLY_AUTH_KEY")`. Split into (a) `load_common()` -> db path, devices file, log level (no Shelly values), used by `serve`, `import`, `backup`; (b) `load_settings()` (poller/probe/check) extended with optional `NTFY_TOPIC`, `NTFY_TOKEN`, `NTFY_SERVER` (default `https://ntfy.sh`), `HEALTHCHECK_PING_URL`. Note the existing env filter only admits two prefixes: `merged.update({k: v for k, v in env.items() if k.startswith(("SHELLY_", "SHELOG_"))})` [VERIFIED: src/shelog/config.py `load_settings`], so `NTFY_*` and `HEALTHCHECK_*` must be added to that filter or they are silently ignored. Add the topic, token and ping URL to the `setup_logging` secrets list (both appear in URLs/headers; ntfy's response body echoes the topic, never log response bodies). `serve` must not read `.env` from cwd either (none exists in the image; keep behavior). `import` against an alert-less config must not require the Shelly key either.

### Pattern 5: Starlette app + uvicorn run (verified)
```python
# web.py
def create_app(db_path, devices, clock) -> Starlette:
    def open_ro() -> sqlite3.Connection:
        # mode=rw (NOT ro, D-05): never creates a missing file; query_only blocks writes
        con = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=5)
        con.execute("PRAGMA query_only = ON")
        con.execute("PRAGMA busy_timeout = 5000")
        return con
    def healthz(request): return PlainTextResponse("ok")          # no DB: liveness only
    def api_health(request):                                      # plain def -> threadpool
        try: con = open_ro()
        except sqlite3.Error: return JSONResponse({"status": "unknown"}, headers=CACHE)
        try: body = health.evaluate(con, devices, int(clock.time()))
        except sqlite3.Error: body = {"status": "unknown"}
        finally: con.close()
        return JSONResponse(body, headers={"Cache-Control": "public, max-age=30"})
    return Starlette(routes=[Route("/healthz", healthz), Route("/api/health", api_health)])

def serve(app) -> int:
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1, log_config=None,
                access_log=False, server_header=False, timeout_graceful_shutdown=5)
    return 0
```
Verified: with `mode=rw` and no DB file the endpoint answers `{'status': 'unknown'}` without creating a file; once the poller has migrated it returns data; `uvicorn.run(..., log_config=None)` keeps the app's redacting logger, exits 0 on SIGTERM in 0.4 s. `access_log=False` keeps IPs out of logs (Caddy has the access log). Close every connection (ResourceWarning is an error under pytest). Open one connection per request (traffic is trivial). Reader and writer must run as the same UID. Health fields (discretion): `status` (`ok|degraded|down|unknown`), `last_poll_ok_ts` (unix s, null if none), `auth` (`ok|rejected|unknown`), `devices` (`{ "<name>": {"silent": bool} }`), optionally `generated_at`. Never include `error` text; `poll_log.error` is a category enum but still omit it (D-06). Use read-side defaults for silent threshold: device `max_silence_s` if set, else by model (54000 for `ht_gen1`/`flood_gen1`, 9000 for `ht_gen3`, matching `devices.example.toml` and docs/shelly-api.md).

### Pattern 6: ntfy publish via the existing `transport.send` (no new code in the socket module)
`transport.send(url, json_body=..., headers=..., timeout=...)` already POSTs JSON with a whole-exchange deadline, no redirects. ntfy accepts JSON at the server **root**, so the topic never appears in the URL. Verified live against ntfy.sh with a throwaway topic:
```python
send(f"{server.rstrip('/')}/", json_body={"topic": topic, "title": title, "message": msg,
     "priority": 4, "tags": ["warning"]}, headers={"Authorization": f"Bearer {token}"} if token else None, timeout=8)
```
Response: HTTP 200 and a JSON body `{"id":...,"topic":"<topic>",...}` (the topic is echoed: do not log the body). Invalid topic: `HTTP 400 {"code":40009,"http":400,"error":"invalid request: topic invalid"}` [VERIFIED: curl this session]. Docs: `title` string, `priority` integer 1-5 (3 default), `tags` array of strings, Bearer token header `Authorization: Bearer tk_...`, inline message limit 4,096 bytes [CITED: docs.ntfy.sh/publish]. Status 2xx = delivered; anything else or `TransportError` = failed (log category only).

### Pattern 7: healthchecks.io ping
`POST {HEALTHCHECK_PING_URL}` with an empty body (transport.send's no-body branch). HEAD/GET/POST all allowed; success = HTTP 200 body `OK`; `{url}/fail` signals failure; limit ~5 pings/min/check, excess may be dropped and extreme cases blocked [CITED: healthchecks.io/docs/http_api]. Use timeout 5-8 s; swallow all errors. Ping after a successful cycle, at most once per 5 min (track `last_ping_monotonic` in memory). Suggested check settings for the owner: period 10 min, grace 20-60 min, integration to ntfy/email. Note the trade-off: success-only pings mean a long Shelly outage also fires healthchecks.io (duplicates the `poll_failing` ntfy alert); acceptable, owner can lengthen the grace.

### Pattern 8: alert state machine over the existing `alert_state` table
Schema [VERIFIED: src/shelog/migrations/0001_init.sql]: `key TEXT PRIMARY KEY, since INTEGER NOT NULL, notified_at INTEGER` (note: no `active`/`last_sent` columns, unlike the stack-research sketch). Use: row exists = raised condition; `since` = when first seen; `notified_at` = unix time the raise message was delivered, NULL until delivery succeeds. Each housekeeping pass, per key: desired = condition holds?
- desired and no row: INSERT (since=now, notified_at NULL).
- row with `notified_at IS NULL` and desired: try send; on 2xx set `notified_at`. (Retry next pass on failure.)
- row exists, not desired: send recovery; delete the row **only after** a 2xx (if the recovery send fails the row stays and is retried; if it was never notified, just delete silently).
- poller restart: state is in SQLite, so a deploy never re-sends.
No migration needed. Keys: `poll_failing`, `auth_rejected`, `device_silent:<name>`, `battery_low:<name>` (names are unique per config validation). Conditions (all from `health.evaluate` inputs so web and alerts agree): `poll_failing` = now - max(ts of ok shelly poll_log row, else first poll_log ts) > 3600; `auth_rejected` = `meta.shelly_auth == 'rejected'` (the poller already requires 2 consecutive auth outcomes, constants `AUTH_OK = "ok"`, `AUTH_REJECTED = "rejected"`, `META_AUTH = "shelly_auth"` [VERIFIED: src/shelog/poller.py top-level constants]); `device_silent` = now - MAX(reading.ts) > threshold, **suppressed while the last ok poll is older than ~15 min** (otherwise one outage fires 7 per-device alerts) and for a never-reported device measured from the first poll_log ts; `battery_low` = latest non-NULL battery < 20, cleared at >= 25 (hysteresis; discretion). Housekeeping runs after `poller.cycle` inside `run()` wrapped in a catch-all (log the exception type; use `exc_info` only through the redacting formatter), with sender timeouts <= 8 s and honours the stop event; it must never change the `OK/failure` category returned to the scheduler. Priorities: `auth_rejected`/`poll_failing` 4 with tag `rotating_light`/`warning`, silent/battery 3, recoveries 3 with `white_check_mark`. Message text: "<name>: silent for N h", "polling failing for N h (category)", never ids/hosts/URLs.

### Pattern 9: Backup (D-14), both APIs verified under a concurrent writer
In a stress test (a writer thread committing 50-row batches continuously) `src.backup(dst)` produced a snapshot that passed `PRAGMA integrity_check` ('ok') while the writer progressed, as did `VACUUM INTO` (compacted, journal mode `delete`). Recommendation: `sqlite3.Connection.backup()` (default `pages=-1`, single step, no restart on concurrent writes) from a fresh read connection into `data/backup/shelog-YYYYMMDD.db.tmp`; then open the tmp file, run `PRAGMA integrity_check` (must be `ok`), `os.replace` to the final name, `chmod 0600` (raw_status holds SSID/IP/MAC per review IN-07), delete all but the newest N (7) by filename sort. Trigger: poller housekeeping when today's UTC file does not exist yet (restart-safe, no meta needed). Failures log a category and retry next pass; never raise. Expose the same function as `shelog backup --out PATH` (used for the pre-import snapshot of the local DB and for manual pre-maintenance backups). Restore (document): `docker compose stop poller web`, copy the snapshot over `data/shelog.db`, delete `data/shelog.db-wal` and `-shm`, `docker compose start`.

### Pattern 10: Import (D-09), SQL verified for idempotence and id remapping
Design points proven in a scratch run (local rows older/overlapping prod rows; run twice gave `[3, 2, 2]` then `[0, 0, 0]`, `poll_id` remapped to production ids):
1. Open the production DB with `connect_rw` + `migrate`; open with `uri=True` semantics (`sqlite3.connect(path, uri=True, isolation_level=None)`) so `ATTACH DATABASE 'file:<src>?mode=ro' AS src` works; ATTACH **before** `BEGIN IMMEDIATE` (ATTACH cannot run inside a transaction). Refuse if the resolved source path equals the live DB path; check source `PRAGMA src.user_version` <= known and that the tables exist; use explicit column lists (never `SELECT *`).
2. Dedup rule = primary key, not "newer than last": `INSERT INTO main.reading(...) SELECT ... FROM src.reading s WHERE true ON CONFLICT DO NOTHING` (SQLite requires a `WHERE` on an INSERT...SELECT that carries an upsert clause to avoid a parse ambiguity). A "ts > max(ts)" rule would drop all local history older than production's first reading.
3. poll_log has no natural unique key and different ids per DB: insert source rows `WHERE NOT EXISTS (SELECT 1 FROM main.poll_log p WHERE p.kind = s.kind AND p.ts = s.ts)` ordered by `s.id`; do not copy `id`. Remap `reading.poll_id` via `(SELECT p.id FROM main.poll_log p WHERE p.kind='shelly' AND p.ts = s.polled_at)` (NULL if none; the column is nullable). Copy `raw_status` with `ON CONFLICT DO NOTHING`. Do NOT import `meta` or `alert_state` (they hold production auth/alert state).
4. Counts come from `cursor.rowcount` per statement; print counts only. Run all four statements in one transaction, `DETACH` after.
5. Operational: stop the local poller first (a clean stop checkpoints the WAL, leaving a single self-contained `.db`; verified earlier that a clean `docker stop` leaves only `shelog.db` and `poller.lock`), take a snapshot with `shelog backup --out` for good measure, `scp` it to `data/import/local.db` on the server, then `docker compose exec poller shelog import /app/data/import/local.db` (exec avoids the lock check; import does not take `poller.lock`: it is a single `BEGIN IMMEDIATE` transaction with `busy_timeout`, safe next to the sole writer). The running poller's in-memory `_last_states` may be older than the imported max; this self-heals (a duplicate insert is `ON CONFLICT DO NOTHING`, counted unchanged, and `n_new != len(rows)` triggers `load_last_states`). Delete the import file afterwards (it contains raw_status).

### Anti-Patterns to Avoid
- **`load_settings()` in `serve`/`import`** (requires the Shelly key): breaks OPS-03 or crashes `web`. Split settings (Pattern 4).
- **Hard failure on missing optional alert config** (`NTFY_TOPIC` unset, ping URL unset): log one clear WARNING at startup ("alerts disabled") and continue; the server `.env` is hand-edited and never regenerated.
- **`mode=ro` or mounting `shelog.db` alone** for web: see Pitfall 6.
- **`env_file: .env` on `web`**: puts the key in the public process (D-02).
- **`ports:` on any service**; **`docker compose down` in deploy**; **`docker system prune`** (all Bombur rules).
- **Sending the alert message on every failed poll or every cycle**: only on transitions (D-11).
- **Importing `ts > last` only**: loses older local history (Pattern 10).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| HTTP for ntfy/healthchecks | a second HTTP client or raw sockets | existing `shelly/transport.send` (JSON/empty-body POST, whole-exchange deadline, no redirects, categories not text) | Already hardened (WR-02, IN-03); the topic and ping URL are secrets, so no exception text may leak |
| Consistent snapshot | `cp`/rsync of the live DB | `Connection.backup()` + `integrity_check` + `os.replace` | Verified consistent under concurrent writes |
| Merging two DBs | row-by-row Python loop with manual id maps | `ATTACH` + `INSERT ... SELECT ... ON CONFLICT DO NOTHING` in one transaction | Verified idempotent; fewer failure modes |
| Request routing/ASGI server | `http.server` | Starlette + uvicorn (owner decision D-04) | Timeouts, graceful shutdown, ETag handling not worth rewriting for a public endpoint |
| Scheduling | APScheduler/cron | the existing deadline loop + a housekeeping hook | Restart-safe, already tested; backup/alerts triggers derive from DB state, not timers |
| Secret redaction | per-call string scrubbing | add the new secrets to `setup_logging(secrets)` | `RedactingFormatter` already covers messages, args and tracebacks in raw and URL-quoted forms |

## Runtime State Inventory

Not a rename/refactor phase (greenfield deployment). The only cross-environment state is the local interim DB, covered by D-09 and Pattern 10: **Stored data:** local `data/shelog.db` (readings since 2026-11-01 09:05 UTC) -> data migration via `shelog import`. **Live service config:** none yet (ntfy topic and healthchecks check are created by the owner). **OS-registered state:** none; reboot survival relies on `restart: unless-stopped` plus the Docker service being enabled at boot (A3). **Secrets/env vars:** new server `.env` keys listed below. **Build artifacts:** none.

## Common Pitfalls

### Pitfall 1: `serve` dies on missing Shelly key
**What goes wrong:** `web` exits 2 ("missing required setting SHELLY_AUTH_KEY") because `cli.main` loads full settings first; the "fix" of passing the key to `web` violates OPS-03.
**How to avoid:** per-subcommand settings (Pattern 4); a test that runs `cli.main(["serve", ...])` with a scrubbed environment and asserts it reaches the server start.
**Warning signs:** `web` restart-looping with `config error: missing required setting SHELLY_AUTH_KEY`.

### Pitfall 2: UID mismatch on `./data` or `devices.toml`
**What goes wrong:** `startup failed: PermissionError`/`OperationalError` loops, or `devices file not found`/permission error because the container UID is not deploy's UID. Note `connect_rw` does `path.parent.mkdir(..., mode=0o700)`: if the directory exists (always, `bombur-add-app` creates it) this is a no-op.
**How to avoid:** `user: "${APP_UID}:${APP_GID}"`, owner checks `id deploy` first (Owner checklist step 0); add a startup self-check that logs a one-line actionable message if `data/` is not writable.
**Warning signs:** poller restart loop with `startup failed: OSError`/`PermissionError`; `web` `/api/health` always `unknown`.

### Pitfall 3: Server `.env` lacks a variable added in a later wave
**What goes wrong:** `.env` is created once; new vars appear in `.env.example` but not on the server, so ntfy/ping silently never run.
**How to avoid:** the first `.env.example` contains every variable (below); code treats them as optional with a startup WARNING naming the missing feature; DEPLOYMENT.md has the "add var to the server .env" command; a `shelog check` line reports "alerts: configured/not configured" (names only).

### Pitfall 4: `CHANGE_ME` / placeholder collisions
`bombur-add-app` randomizes only lines ending exactly `=CHANGE_ME`. `SHELLY_AUTH_KEY=SET_ME` passes through, and `config._check_key` rejects short/`change_me` keys (a 6-char `SET_ME` is rejected as "placeholder or too short", good: the poller fails loudly until the owner fills it). Keep `NTFY_TOKEN=` and `HEALTHCHECK_PING_URL=` empty or `SET_ME` (empty = feature off; `SET_ME` must not be treated as a URL: validate `https://` scheme or treat as unset with a warning). Never write `CHANGE_ME` inside comment text on its own line ending.

### Pitfall 5: Crash-loop on bad config hammers Shelly
Config errors exit 2 immediately and `restart: unless-stopped` restarts forever (Docker backs off, but still). The first Shelly request only happens after config loads, so a bad `.env` itself sends nothing. For the poller, optionally sleep `STARTUP_FAIL_SLEEP_S` before returning 2 in container mode. Review IN-05 (first request at process start, per-process `RateGuard`) means each redeploy sends one request promptly: harmless at 1/min, but add the >= 1.1 s startup delay `live.sh` already uses (or persist last-request time) if the planner wants the guard across restarts.

### Pitfall 6: SQLite WAL across containers
Works only on the same host with a directory bind mount (both containers share `-shm`/`-wal`; verified). `mode=ro`/`:ro` mounts fail when sidecars do not exist (review IN-08; stack research). Reader and writer need the same UID. `PRAGMA journal_mode=WAL` persists in the file, so the reader must not set it. Use `mode=rw` in the URI so an absent DB is not created by `web`.

### Pitfall 7: Reboot test conclusions
`restart: unless-stopped` restarts containers after reboot only if the Docker daemon starts at boot (A3, owner-verifiable with `systemctl is-enabled docker`). After the reboot the poll_log shows a gap (outage window) and the first request goes out at container start. Bombur also auto-reboots at 04:30 after unattended upgrades [CITED: bombur-infra SKILL.md], so this is a real recurring event, not just a test.

### Pitfall 8: Alert noise and loops
Seven device-silent alerts during one cloud outage (suppress, Pattern 8); a failed ntfy send retried every cycle with a stale 10 s timeout (cap timeouts, count failures, log category once per N); ntfy response echoes the topic (do not log bodies); alert flapping at the battery threshold (hysteresis). Silence thresholds for Gen1 are 12 h x 1.25; docs/shelly-api.md says Phase 2 should tune from observed gaps ("observed" source): do this from `reading` gaps after a few production days and not as a blocker for the first deploy.

### Pitfall 9: First-deploy ordering
`docker compose up -d` fails the whole deploy if `devices.toml` is missing (good, loud) or `proxy` network is absent (it exists on Bombur). The `.env` must be filled **before** the first push or the poller restart-loops on the placeholder key; `web` is fine regardless. After the first successful deploy the Caddy site already exists, so `https://shelog.remesh.cz/healthz` works at once.

## Code Examples

### `.env.example` (all variables from day one; verified against the CHANGE_ME rule)
```bash
# Copy of this file becomes ~deploy/DOCKER/shelog/.env on first onboarding (never regenerated).
# GITHUB_REPOSITORY and CONTAINER_NAME are appended by bombur-add-app; IMAGE_TAG is set by bombur-deploy.

# --- Container user: must equal `id -u deploy` / `id -g deploy` on Bombur (owner: check and edit) ---
APP_UID=1000
APP_GID=1000

# --- Shelly Cloud (secret; owner fills in: Shelly app > User settings > Authorization cloud key) ---
SHELLY_AUTH_KEY=SET_ME
SHELLY_HOST=SET_ME
SHELOG_POLL_INTERVAL_S=60
SHELOG_LOG_LEVEL=INFO

# --- Alerts via ntfy (topic is effectively a password; randomized on onboarding) ---
NTFY_TOPIC=CHANGE_ME
NTFY_TOKEN=
NTFY_SERVER=https://ntfy.sh

# --- Dead-man's switch: healthchecks.io ping URL (secret; empty = disabled) ---
HEALTHCHECK_PING_URL=
```
`SHELOG_DB_PATH` / `SHELOG_DEVICES_FILE` are deliberately not here (set in compose `environment:`). The owner reads the generated topic with `ssh -p 10022 ciakval@bombur.remesh.cz sudo -u deploy grep ^NTFY_TOPIC /home/deploy/DOCKER/shelog/.env` and subscribes in the ntfy app.

### Owner checklist skeleton (D-03; copy-pasteable)
0. `ssh -p 10022 ciakval@bombur.remesh.cz id deploy` -> note uid/gid; `systemctl is-enabled docker`.
1. Protect `main` (GitHub: Settings > Branches, or `gh api -X PUT repos/ciakval/shelog/branches/main/protection ...`).
2. From the repo root: `~/GIT/bombur-deploy/bin/bombur-onboard shelog 8000`.
3. Edit the server `.env`: `SHELLY_AUTH_KEY`, `SHELLY_HOST`, `APP_UID`/`APP_GID`; read `NTFY_TOPIC`.
4. Place devices: `scp -P 10022 devices.toml ciakval@bombur.remesh.cz:/tmp/devices.toml` then `ssh -p 10022 ciakval@bombur.remesh.cz 'sudo install -o deploy -g deploy -m 640 /tmp/devices.toml /home/deploy/DOCKER/shelog/devices.toml && rm /tmp/devices.toml'`.
5. Push to `main`; watch Actions; `curl -fsS https://shelog.remesh.cz/healthz`.
6. Later: subscribe ntfy topic; create the healthchecks.io check and put its URL in `.env` as `HEALTHCHECK_PING_URL`, then `docker compose up -d poller` (env_file is read at container creation, so recreate, not just restart); reboot test.
(Per CLAUDE.md the repo must not contain the owner's device ids: DEPLOYMENT.md uses placeholders only.)

## State of the Art

| Old Approach | Current Approach | Impact |
|--------------|------------------|--------|
| Phase 1 stack notes: httpx2 for HTTP | stdlib `urllib` via `shelly/transport` (Phase 1 decision) | ntfy/healthchecks reuse it; no HTTP dependency at runtime |
| Stack notes: `alert_state(key, active, since, last_sent)` | actual schema `(key, since, notified_at)` | State machine must use the real columns (Pattern 8) |
| Template Dockerfile with uv in the runtime image and `:latest` | two-stage, uv pinned, venv only in the runtime | smaller image, reproducible |
| Starlette TestClient on httpx | on httpx2 (httpx deprecated) | test-only dev dependency |

**Deprecated/outdated:** `on_startup`/route decorators removed in Starlette 1.0 (use `routes=[...]`, `lifespan`); OTLP deferred (D-scope).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The `deploy` user's UID/GID on Bombur is unknown (likely 1001, not 1000); the checklist makes the owner look it up | UID decision | Wrong UID = poller cannot write `data/` (loud restart loop); mitigated by step 0 and a startup writability check |
| A2 | Caddy does not cache responses, so `Cache-Control: public, max-age=30` only helps clients; a tiny in-process TTL cache (5-10 s) for `/api/health` may be wanted to spare SQLite under load | Pattern 5 | Low: queries are trivial |
| A3 | Docker daemon is enabled at boot on Bombur (reboot test depends on it) | Pitfall 7 | Containers would not return after reboot; owner verifies with `systemctl is-enabled docker` |
| A4 | `astral-sh/setup-uv@v10` major tag exists (latest release v10.3.3 verified, tag form not) | Pattern 3 | CI fails fast; pin `@v10.3.3` instead |
| A5 | healthchecks.io check settings (period 10 min, grace 20-60 min) are sensible for a 1 min poll with pings throttled to 5 min | Pattern 7 | Late or noisy dead-man alerts; owner-tunable |
| A6 | Default battery alert threshold 20 % (clear at 25 %) and housekeeping cadence "after every cycle" for alerts, daily for backup | Pattern 8/9 | Discretion area; wrong values only change alert timing |
| A7 | GHCR package visibility is handled by the token piped by `bombur-deploy` (private package pull works per the skill doc); not exercised here | Onboarding | First deploy fails at `pull`; fix is package visibility or `docker login` |
| A8 | ntfy.sh public rate limits are not verified (alert volume is a few messages a day) | Pattern 6 | Negligible |

## Open Questions

1. **Does the owner accept `httpx2` as a dev-group dependency for `TestClient`?**
   - Known: TestClient hard-requires it. Alternative is an ASGI-call helper or a uvicorn-thread + urllib e2e test.
   - Recommendation: dev-group `httpx2`, with a `checkpoint:human-verify` since it is flagged SUS (young package); fall back to the no-dependency route if declined.
2. **Silence thresholds from observed gaps (docs/shelly-api.md says recompute in Phase 2).**
   - Recommendation: ship alerts with the current `max_silence_s`; add a small `scripts/` helper (aggregate output, names only) after ~3 days of production data; not a wave-1 blocker.
3. **Should `health.evaluate` also drive the dead-man's switch semantics?** Keep ping tied to a successful cycle (D-12); revisit only if the owner finds the duplicate alert during Shelly outages annoying.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Docker + Compose (local) | image/compose verification | yes | 29.9.1 / v5.6.0 | none needed |
| uv | locking, CI parity | yes | 0.12.19 local (image pins 0.13.6) | n/a |
| gh CLI | repo/protection checks, onboard | yes | authenticated (repo query succeeded) | n/a |
| ghcr.io/astral-sh/uv:0.13.6 | image build | yes | manifest present | n/a |
| ntfy.sh | alerts | yes | public JSON publish returned 200 | self-host later |
| hc-ping.com / healthchecks.io | dead-man's switch | docs verified; no account yet | n/a | owner creates the check |
| Bombur (SSH, Docker, Caddy, `proxy` network) | deployment | not probed (shared server, owner-only per D-03) | n/a | owner checklist |

**Missing dependencies with no fallback:** none for planning. Owner-side prerequisites are the checklist.

## Verification Map (workflow.nyquist_validation is false: compact version, not the full Nyquist section)

Test framework: pytest 9.2 (existing), offline, `FakeClock`/`FakeSend`/`tmp_db` from `tests/conftest.py`. Quick: `uv run pytest tests/test_x.py -x`; full: `uv run pytest && uv run ruff check . && uv run ruff format --check .`.

| Req / criterion | How to verify | Automated? |
|-----------------|---------------|-----------|
| OPS-01 / SC1 test gate and deploy | CI run order (`needs`), failing-test PR blocked; post-deploy `curl -fsS https://shelog.remesh.cz/healthz` (also the workflow's `health_path`); production `poll_log` shows ok cycles and 7 devices via `scripts/collection_report.py --expect-devices 7` (owner/Claude with DB access via `docker compose exec poller python ...`/copied backup) | CI + owner |
| Image/compose correctness | local: `docker build`, `docker compose config` with a scratch `.env`, `docker compose up` with dummy key + `.invalid` host + the example devices file; `docker stop` exit 0 within the grace period; `web` has no `SHELLY_*` (`docker compose exec web env | grep -c SHELLY` = 0) | script (no Shelly traffic) |
| OPS-02 / SC2 durability | after redeploy, `docker compose restart`, and reboot: `SELECT COUNT(*) FROM reading` only grows, `poll_log` has rows explaining the gap | owner (reboot approval needed) |
| OPS-03 | unit: `serve` starts with a scrubbed env; compose test asserts `web` env keys; local two-container run: `web` reads while `poller` writes | pytest + local docker |
| DATA-05 | `TestClient` tests on a temp DB: empty DB -> `unknown`; fresh ok poll -> `ok`; stale poll -> `degraded/down`; silent device by name; auth `rejected`; response contains no ids/error text (assert against the fixture ids and `poll_log.error` values); `Cache-Control` header | pytest |
| OPS-05 / SC4 | pytest with `FakeSend`: raise/recovery for each key, retry after failed send, no resend after restart, silence suppression, no secrets in messages; forced drill: dummy key in a scratch DB reaches `rejected` -> one ntfy message -> restore -> recovery message (use a throwaway ntfy topic) | pytest + drill |
| ROB-01 | pytest: ping only after ok cycle, throttled to 1/5 min, errors swallowed; real: owner checks the healthchecks.io dashboard shows pings | pytest + owner |
| OPS-04 | pytest: `backup()` creates a file that passes `integrity_check`, rotation keeps N, idempotent same-day; production: `ls data/backup/` next day | pytest + owner |
| D-09 import | pytest: idempotent (second run zeros), preserves older rows, remaps poll_id, refuses live path; local drill on two scratch DBs | pytest |
| OPS-06 | DEPLOYMENT.md walk-through: every command copy-pasteable; real-id scan passes (`SHELOG_REQUIRE_REAL_ID_SCAN=1 SHELOG_DEVICES_FILE=<owner file> uv run pytest tests/test_fixture_safety.py`) | pytest + review |

Wave 0 gaps: `tests/test_web.py`, `tests/test_health.py`, `tests/test_alerts.py`, `tests/test_backup.py`, `tests/test_import.py`, `tests/test_notify.py`, a compose/Dockerfile sanity test (parse YAML key presence needs PyYAML, which is not a dependency: use plain text assertions, or a shell script run in CI as a non-pytest step).

## Security Domain

`security_enforcement` is on (ASVS level 1, block on high).

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no (public read-only endpoint; outbound token only for ntfy) | Bearer token in header, never URL; token in env only |
| V3 Session Management | no | n/a |
| V4 Access Control | yes | `web` has no secrets and no write capability (`query_only=ON`), no `env_file`, explicit environment, only `web` on `proxy`, no `ports:` |
| V5 Input Validation | minimal | `/api/health` and `/healthz` take no input; ignore query strings; validate `NTFY_SERVER`/`HEALTHCHECK_PING_URL` scheme (`https://`) and topic charset at config load |
| V6 Cryptography | no hand-rolled | TLS terminated by Caddy; outbound TLS via urllib default context |
| V7 Error handling/logging | yes | redacting formatter gets NTFY topic/token and ping URL; never log ntfy response bodies; `access_log=False`, `server_header=False` |
| V14 Configuration | yes | non-root user, pinned base/uv tags, `.dockerignore` excludes `.env`/`data/`/`devices.toml`, no secrets in image or workflow |

### Known Threat Patterns

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Info disclosure via health JSON (ids, error text, raw_status) | Information disclosure | whitelist-built response dict with device names, booleans, ints only; test asserts absence of ids/`error` |
| Shelly key / ntfy topic / ping URL in logs, images or git | Information disclosure | redacting logger secrets list; `.dockerignore`; fixture-safety scan covers all committable files incl. new files |
| Public SQLite endpoint abused for load | DoS | trivial indexed queries, per-request connection with busy_timeout, Cache-Control, optional 5-10 s in-process cache |
| Backup/import files exposing raw_status (SSID/IP/MAC) | Information disclosure | `chmod 0600` on backups; backups live under `data/backup/`, not under any served path; delete import file after use; web serves no files from `data/` |
| CI-controlled compose or key abuse | Elevation | unchanged Bombur design (forced command, server-owned compose); never add a normal SSH key to CI |
| Stale/rejected auth key silently ignored | Repudiation/availability | `auth_rejected` immediate alert plus health `auth` field; dead-man's switch for total silence |

## Sources

### Primary (HIGH confidence)
- Files read this session: `~/GIT/bombur-deploy/{README.md, bin/bombur-deploy, bin/bombur-onboard, sbin/bombur-add-app, .github/workflows/deploy.yml}`; `~/.claude/skills/bombur-infra/{SKILL.md, assets/{compose.yaml, deploy.yml, env.example}, references/{dockerfiles.md, observability.md}}`; repo `src/shelog/{cli,config,db,lock,poller,store,logging_setup,clock}.py`, `shelly/{transport,client}.py`, `migrations/0001_init.sql`, `tests/{conftest,test_fixture_safety}.py`, `pyproject.toml`, `.gitignore`, `devices.example.toml`, `.env.example`, Phase 1 review IN-05/WR-02/IN-07/IN-08.
- Executed this session (scratch copy of HEAD under the session scratchpad, no Shelly traffic): `uv add starlette uvicorn` (lock set), `docker build` of the Dockerfile above, `docker run` poll with dummy key against a `.invalid` host (SIGTERM exit 0 in 0.27 s; WAL DB created; second container read with `query_only`, write blocked), uvicorn programmatic run (SIGTERM exit 0), `docker compose config` and `up --no-start` with a missing bind source, clean-checkout `pytest` (392 passed, 2 skipped) + ruff, SQLite `backup()` / `VACUUM INTO` under a concurrent writer, ATTACH-based import SQL run twice, `TestClient` with `-W error`.
- Live ntfy.sh JSON publish (throwaway topic) returning HTTP 200 and the 400/40009 invalid-topic response.
- PyPI JSON API (first-upload dates, versions, dependency lists); `gh api` for action release tags, repo visibility and branch protection state; `docker manifest inspect` for the uv image tag.

### Secondary (MEDIUM confidence)
- https://docs.ntfy.sh/publish/ (JSON publishing at server root, `title`/`priority`/`tags` types, Bearer token, 4,096-byte inline limit): CITED, fetched in two parts.
- https://healthchecks.io/docs/http_api/ (ping URL forms, GET/HEAD/POST, 200 `OK`, `/fail`, ~5 pings/min limit): CITED.
- https://docs.github.com/en/actions/using-workflows/reusing-workflows (`needs` on reusable-workflow jobs, `secrets: inherit`, environment handled inside the reusable workflow): CITED.

### Tertiary (LOW confidence)
- Deploy-user UID, Docker-enabled-at-boot, setup-uv major tag form, healthchecks period/grace values: see Assumptions Log.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH (resolved and built locally; owner pre-approved the runtime additions)
- Architecture / compose / CI: HIGH (executed or read); server-side behavior MEDIUM until the first real onboarding
- Alerts/ping/backup/import designs: HIGH for mechanics (executed), MEDIUM for thresholds (discretion)
- Pitfalls: HIGH (most reproduced locally)

**Research date:** 2026-11-01
**Valid until:** 2026-12-01 (Starlette/uvicorn/actions release frequently; the Bombur scripts are stable)
