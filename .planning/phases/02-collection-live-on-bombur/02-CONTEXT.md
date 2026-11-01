# Phase 2: Collection Live on Bombur - Context

**Gathered:** 2026-11-01 (smart discuss in /gsd-autonomous; 3 owner questions answered)
**Status:** Ready for planning

<domain>
## Phase Boundary

The Phase 1 collector runs unattended in production on Bombur (`shelog.remesh.cz`) via the standard Bombur pipeline (GitHub Actions → GHCR → `ciakval/bombur-deploy`), data survives redeploys/restarts/reboots, `/api/health` is served by a separate read-only `web` service, the owner gets ntfy alerts plus an external dead-man's switch, data collected locally since 2026-11-01 is merged in, and DEPLOYMENT.md/README cover operations. Covers OPS-01..06, DATA-05, and pulls ROB-01 (dead-man's switch) forward. No UI, weather, resampling or stats (Phases 3-5).

</domain>

<decisions>
## Implementation Decisions

### Ordering: deploy first (owner: "I want live collection to be running ASAP")
- **D-01:** Plan order is strictly deploy-first. Wave 1 = everything needed for the first production deploy: Dockerfile, `.dockerignore`, `compose.yaml` (both `poller` and `web` already listed, because compose is server-owned and changing it means re-onboarding), CI workflow (test job gating build-and-push → reusable bombur-deploy), a minimal `web` service with `/healthz` and `/api/health`, and the owner onboarding checklist. Alerts, dead-man's switch, backups, import and docs follow in later waves and ship as ordinary redeploys (no compose change).
- **D-02:** Everything that later waves need from compose (env vars passed to each service, mounts, stop grace period) must be decided in wave 1 so `bombur-onboard` runs once. The `poller` uses `env_file: .env`; `web` gets an explicit `environment:` list with no `SHELLY_*`, no `NTFY_*`, no healthchecks URL.
- **D-03:** Owner actions (protect `main`, `bombur-onboard shelog 8000`, fill server `.env`, copy `devices.toml` to the server, subscribe ntfy topic, healthchecks.io account, approve reboot test) are presented as a copy-pasteable checklist at a checkpoint; Claude never changes shared server config, Caddy or GitHub secrets itself.

### Web stack
- **D-04:** Starlette + uvicorn (no `[standard]` extra), as in the stack research, used only by `shelog serve`. The poller and the rest of the runtime stay stdlib-only (Phase 1 replaced httpx2 with urllib; do not reintroduce httpx2 at runtime). Port 8000, container name `${CONTAINER_NAME:-shelog-web}`, `--workers 1`, plain `def` handlers.
- **D-05:** `web` opens SQLite read-write-capable but sets `PRAGMA query_only=ON` (no `mode=ro`), mounts the whole `./data` directory, never the db file alone.
- **D-06:** `/api/health` returns coarse status only: last successful poll time, per-device silent yes/no keyed by device *name* (never id), auth state (`ok`/`rejected`/`unknown`), overall status. No error text, ids, MACs, coordinates, raw_status. `Cache-Control: public, max-age=30`.

### Container / runtime
- **D-07:** One image (`python:3.13-slim`, pinned uv image tag, non-root user, `VERSION`/`BUILD_ID` build args), entrypoint `shelog`, subcommands `poll` and `serve`. Explicit `WORKDIR` and explicit `SHELOG_DB_PATH`/`SHELOG_DEVICES_FILE` so nothing depends on an implicit cwd (review IN-05). `devices.toml` is bind-mounted read-only from the server deploy dir (repo is public; file is gitignored). Exec-form command so SIGTERM reaches the process; `stop_grace_period` long enough for a bounded in-flight request (review WR-02).
- **D-08:** Container UID must be able to write the bind-mounted `./data` (Bombur pitfall): document a one-time `chown` or match the deploy user's UID.

### Seeding production from the local run
- **D-09:** Production starts on an empty DB as soon as it deploys. Then the local poller is stopped (Claude does this once production is confirmed polling) and the owner copies the local `data/shelog.db` to the server; a new `shelog import <path>` command merges readings into the production DB with the same dedup rule (insert only when the report time is new for that device; never overwrite), plus poll_log rows, in one transaction, idempotent on re-run, runnable inside the poller container (`docker compose exec poller shelog import ...` or `run --rm`). Import reports counts only.
- **D-10:** Never run the local and production pollers for longer than needed together (shared 1 req/s account budget): stop the local one as soon as production poll_log shows successful cycles.

### Alerts (ntfy) and dead-man's switch
- **D-11:** ntfy on the public `ntfy.sh` with a long random topic in the server `.env` (`NTFY_TOPIC`; optional `NTFY_TOKEN`, `NTFY_SERVER`). Alerts on transitions only via the existing `alert_state` table: `poll_failing` (> 1 h of failed polls), `auth_rejected` (immediate), `device_silent:<name>` (per-device `max_silence_s` from devices.toml, sensible default), `battery_low:<name>` (threshold Claude's discretion, ~20 %), each with a recovery message. Messages carry device names and categories only. Delivery failure is logged and retried next cycle; it can never raise out of the poll loop or block a poll.
- **D-12:** External dead-man's switch (ROB-01 pulled forward, owner approved): poller pings a healthchecks.io check URL (`HEALTHCHECK_PING_URL` in server `.env`, secret) after each successful cycle (rate-limited, e.g. at most once per 5 min), with a short timeout; ping failures never affect polling. healthchecks.io notifies the owner if pings stop (owner configures its ntfy/email integration).
- **D-13:** Verification forces at least the auth-rejected alert and its recovery (e.g. with a dummy key in a scratch DB / local run), per success criterion 4.

### Backups
- **D-14:** Daily consistent snapshot by the poller via `Connection.backup()` (or `VACUUM INTO`) into `data/backup/`, keeping a small rotation (e.g. 7 daily). Verified by opening the snapshot and running `PRAGMA integrity_check`.

### Claude's Discretion
- Exact plan split beyond "wave 1 = first deploy", module names (`web.py`, `alerts.py`, `backup.py`, `importer.py`), housekeeping cadence inside the poller loop, battery threshold, default silence threshold, backup retention count, health JSON field names.
- Fixes from the Phase 1 code review (WR-01..06, IN-01, IN-03) are being applied before planning; IN-05 readiness items are covered by D-07.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `config.load_settings` / `Secret` / `load_devices` (devices carry `max_silence_s`), `logging_setup` (key scrubbing), `db` (pragmas, migrations; schema 0001 already has `alert_state`, `meta`, `poll_log`, `reading`, `raw_status`), `store` (dedup rule — reuse for import), `poller` (run loop with stop Event and signal handlers), `lock` (single-instance lock), `clock` (injectable).
- `scripts/collection_report.py` (aggregate acceptance report; usable for production checks).

### Established Patterns
- Stdlib-only runtime, offline tests with `FakeClock`/`FakeSend`, closed error-category enums, aggregate-only logs, fixture-safety scans over every committable file.

### Integration Points
- New CLI subcommands in `cli.py` (`serve`, `import`), housekeeping hooks in the poller loop (alerts, ping, backup), new deps in `pyproject.toml` (starlette, uvicorn) + `uv.lock`.
- Bombur templates: `~/.claude/skills/bombur-infra/assets/{compose.yaml,deploy.yml,env.example}`, `references/dockerfiles.md`; deploy tool `~/GIT/bombur-deploy`.

</code_context>

<specifics>
## Specific Ideas

- Domain `shelog.remesh.cz`, app name `shelog`, port 8000, deploy dir `/home/deploy/DOCKER/shelog`.
- `.env.example` must not use `CHANGE_ME` for `SHELLY_AUTH_KEY`/`SHELLY_HOST` (bombur-add-app would randomize them); use e.g. `SET_ME`. `NTFY_TOPIC` may use `CHANGE_ME` (a random topic is exactly right).
- A local poller is currently running from the main checkout (since 2026-11-01 09:05 UTC); it holds the instance lock for `data/`.

</specifics>

<deferred>
## Deferred Ideas

- OTLP/OpenTelemetry export (no collector on Bombur yet).
- Staging environment (`shelog-test`).
- UI, weather, resampling, stats, CSV export (Phases 3-5).

</deferred>
