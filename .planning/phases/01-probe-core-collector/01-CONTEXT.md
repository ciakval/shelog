# Phase 1: Probe & Core Collector - Context

**Gathered:** 2026-10-31 (from the new-project conversation + a live discovery run; no separate discuss-phase)
**Status:** Ready for planning

<domain>
## Phase Boundary

Owner runs the collector locally against the real Shelly account and gets deduplicated, raw, correctly timestamped readings from all seven sensors in SQLite; every poll attempt is explained and the auth key never leaks. Covers PROBE-01..05 and COLL-01..08. Deployment (Docker/compose/CI/Bombur), alerts, weather, API and UI are later phases — but the schema and package layout must already accommodate them (full schema in migration 0001, two-service layout: `poller` writer + read-only `web`).

</domain>

<decisions>
## Implementation Decisions

### Owner stance
- **D-01:** Owner cares about the result; technical choices in SPEC.md and research are recommendations, not mandates. Prefer minimal dependencies and clean, well-automated code.
- **D-02:** Repo is public. `devices.toml` (real device IDs) and `.env` (secrets) are gitignored and live on the server in production; only `devices.example.toml` and `.env.example` are committed.

### Secrets & live runs
- **D-03:** The app loads `./.env` from its working directory by itself (no `.env` path on the command line). A Claude Code hook blocks any command mentioning `.env` — never `cat`/`grep`/source `.env`, never pass it as an argument. Live runs are `uv run shelog probe`, `uv run shelog poll --once`, etc. Real env vars override `.env` values.
- **D-04:** Env var names: `SHELLY_AUTH_KEY`, `SHELLY_HOST` (host may be given with or without `https://`; normalize). Further vars (DB path, poll interval, log level) are Claude's discretion with safe dev defaults.
- **D-05:** The auth key must never be printed: send it in the POST body (form field `auth_key`) where the endpoint accepts it; redact URLs/keys in logs and exception text; silence httpx/httpcore INFO logging; `poll_log.error` stores only a coarse category enum, never raw exception text.

### API endpoint & real response shape (verified live 2026-10-31)
- **D-06:** `POST https://<host>/device/all_status` with form body `auth_key=<key>` WORKS: returns `{"isok": true, "data": {"devices_status": {<id>: <status>, ...}}}` with all 7 devices in one call. This is a valid primary (one request per cycle, no id list needed). The v2 `POST /v2/devices/api/get` is still to be probed (PROBE-01/04) and compared; choose whichever is reliable, keep the other as fallback only if cheap. Rate limit 1 req/s account-wide applies to everything.
- **D-07:** Report timestamp: `_updated` is a string `"YYYY-MM-DD HH:MM:SS"` in **UTC** (verified: equals device `unixtime` within 1 s where present). Gen1 `unixtime` is **0** on some devices (H&T LR-3, Flood LR-Flood). Gen3 `sys.unixtime` equals `_updated`; `sys.time` is local CET (ignore). → Use `_updated` (ts_source `cloud`) for all models; device clock is only a cross-check. Validate plausibility (reject 0/negative/future > small skew/ancient; count rejections).
- **D-08:** Field paths (verified):
  - Gen1 H&T: `tmp.tC` (NOT `tmp.value`, which follows configured units), `tmp.is_valid`, `hum.value`, `hum.is_valid`, `bat.value` (%), `bat.voltage`, `sensor_error`, `is_valid`, `_sleeping`, `act_reasons` (e.g. `["sensor"]`), `getinfo.fw_info.device` (e.g. `shellyht-XXXXXX`).
  - Gen1 Flood: `tmp.tC`, `flood` (bool), `bat.value`, `rain_sensor`, no humidity.
  - Gen3 H&T: `temperature:0.tC`, `humidity:0.rh`, `devicepower:0.battery.percent`, `devicepower:0.battery.V`, `devicepower:0.external.present`, `sys.unixtime`, top-level `ts`, `code`.
  - No `_dev_info`/model code in the v1 envelope → model comes from `devices.toml` (`model = ht_gen1 | ht_gen3 | flood_gen1`), optionally cross-checked against keys.
- **D-09:** A raw snapshot of the live response exists at the scratchpad path `/tmp/claude-1000/-home-ciakval-GIT-shelog/835d21f7-af00-4c70-9137-da6bea6a204e/scratchpad/all_status.json` (contains MACs, SSIDs, IPs, real IDs — must be sanitized before anything from it is committed). It may not survive; the `probe` command must be able to regenerate it.

### Devices (owner-confirmed; real IDs ONLY in gitignored `devices.toml` at repo root — never write real device IDs into committed files)
- **D-10:** LR-1 (outer corner, assumed worst), LR-2 (near corner), LR-3, LR-4 (Gen3), LR-Flood (centre) — group `subfloor`; Work, Bedroom — group `reference`. All Gen1 except LR-4 (Gen3) and Flood. Fields: `id, name, note, group, model, t_offset, rh_offset` (+ optional per-device silence threshold).
- **D-11:** Reporting cadence is long and per model (Gen1 battery ~12 h max wake period unless a 0.5 °C / 0.5 %RH threshold is crossed; Gen3 ~2 h). Silence thresholds are per device/model, never a flat 6 h. Observed first look: one sensor last reported 4 h earlier.

### Storage
- **D-12:** SQLite WAL, single writer (poller). Full schema in migration `0001` from day one (readings, raw_status, outdoor long-format, poll_log, alert_state, meta) so later phases add no breaking migrations. Store raw, uncalibrated values; NULL stays NULL (never coerced to 0). Raw status JSON stored when a reading row is inserted (on change), enough to reparse later.
- **D-13:** Dedup: insert only when `_updated` advanced for that device (unchanged re-reports are dropped — acceptable); count ignored/rejected rows.

### Process/tooling (recommended, from research)
- **D-14:** Python 3.13, uv, package `shelog`, CLI `shelog` with subcommands (`probe`, `poll [--once]`, later `serve`, `export`, `calibrate`). Runtime deps minimal: `httpx2` (fallback `httpx`) — or stdlib urllib if simpler; stdlib sqlite3/tomllib/argparse/logging. Dev: pytest, ruff (incl. DTZ). Single-threaded sync poll loop on monotonic deadlines, jitter, capped exponential backoff (~2–5 min cap, since the cloud keeps only last state), honour 429/Retry-After, distinct auth-rejected state, SIGTERM-clean, single-instance flock.
- **D-15:** Host Python is 3.14 (WSL); target 3.13 for the image — use `uv` with a pinned `requires-python`/`.python-version` so dev matches production.

### Claude's Discretion
- Exact module layout, CLI flag names beyond those above, env var names other than D-04, error category enum names, fixture file naming, whether to keep a v2 code path at all after probing.

</decisions>

<specifics>
## Specific Ideas

- Owner context: ground-floor flat, slab in ground contact (no cellar) under the living room; previous wooden floor there warped. Two industrial dehumidifiers have run in the living room since ~2026-10-24, toggled by workers at unknown times — a confounder, but tracking it is deferred (no extra hardware).
- First readings: LR-1/LR-2 at 98.5–100 %RH, LR-3/LR-4 46–54 %, references 58–67 %, all ~23–26 °C.
- Phase 1 ends with an hour of local `poll` running against the real account (owner or Claude runs `uv run shelog poll`) — schedule this so data collection effectively starts locally even before deploy.

</specifics>

<canonical_refs>
## Canonical References

- `SPEC.md` — original spec (§2 devices/reporting, §3 Shelly API, §5 data model, §8 CLI); tech choices are recommendations
- `.planning/PROJECT.md` — context, constraints, key decisions
- `.planning/REQUIREMENTS.md` — PROBE-01..05, COLL-01..08
- `.planning/research/SUMMARY.md` — consolidated research (overrides individual research files where they differ, e.g. devices.toml location)
- `.planning/research/PITFALLS.md` — timestamp/dedup, auth-key leakage, 429/isok, null handling
- `.planning/research/ARCHITECTURE.md` — schema 0001, component boundaries, poller loop
- `.planning/research/STACK.md` — versions, SQLite pragmas
- `CLAUDE.md` — secrets rule (never read `.env`)
- `/home/ciakval/.claude/skills/bombur-infra/SKILL.md` — deployment conventions (Phase 2, but layout must fit: non-root UID, `./data` bind mount, env-configured)

</canonical_refs>

<code_context>
## Existing Code Insights

Greenfield — no source code yet. Repo contains SPEC.md, CLAUDE.md, .gitignore (ignores .env, devices.toml, data/, *.db*), .planning/, and the gitignored real `devices.toml`.

</code_context>
