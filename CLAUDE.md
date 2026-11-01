# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Phase 1 (probe and core collector) is built: `shelog poll` runs locally against the real account and stores deduplicated, raw readings from all seven sensors in SQLite, with every attempt explained in `poll_log`. Phase 2 (deploy the poller on the VPS) is next, per `.planning/ROADMAP.md`. The web service, resampling, derived metrics, alerts and UI are not built yet: their design is in `SPEC.md` and `.planning/ROADMAP.md`, and will be described here once they exist.

## Secrets and live credentials

`.env` (Shelly key/host) and `devices.toml` are gitignored. A hook blocks Claude from reading `.env` in any form — don't try to work around it. The app loads `./.env` from its working directory by itself, so Claude may run live commands (`uv run shelog …`, `scripts/live.sh …`) autonomously — the owner approved this (2026-10-31). Those commands must never print the key, and the real key is only ever sent to the configured `SHELLY_HOST` (drills and wrong-host tests use a dummy key). Never exceed 1 request/second to Shelly, including in probes/tests — no deliberate bursts. The repo is public: never write real device IDs, MACs, IPs, SSIDs or coordinates into committed files (fixtures must be sanitized). Verify commands must not contain the literal text `.env` (`.env.example` is fine).

## What this is

`shelog` polls Shelly Cloud for 7 battery-powered temperature/humidity sensors (H&T Gen1/Gen3, Flood Gen1) that monitor a drying subfloor and stores the readings in SQLite. `docs/shelly-api.md` records what the live API actually returns (the findings behind the parsers).

## Tooling

Python 3.13 (pinned in `.python-version`), `uv` for env and lockfile, stdlib `urllib` for HTTP and stdlib `sqlite3` + `tomllib`: zero runtime dependencies. Dev tools: `pytest` and `ruff`. The CLI is `shelog`.

```
uv sync
uv run pytest                          # all tests (offline; no network)
uv run pytest tests/test_x.py::test_y  # single test
uv run ruff check . && uv run ruff format --check .
uv run shelog probe [--errors] [--wrong-host] [--save-fixtures DIR]
uv run shelog poll [--once]
uv run shelog check                    # config summary + leak scan of data/
uv run python scripts/collection_report.py --db data/shelog.db --since UNIX_TS --expect-devices 7
```

Live use: `scripts/live.sh <subcommand>` from any checkout or worktree (it runs the app from the main checkout root, where the config and `data/` live), or `uv run shelog <subcommand>` from the main checkout root. Both are governed by the Secrets paragraph above. `scripts/collection_report.py` is an aggregate-only acceptance report (no device ids); `--help` lists its options.

Configuration comes from the secrets file and `SHELLY_*` / `SHELOG_*` environment variables (real environment wins): `SHELLY_AUTH_KEY`, `SHELLY_HOST`, `SHELOG_POLL_INTERVAL_S` (default 60, minimum 30), `SHELOG_DB_PATH` (default `data/shelog.db`), `SHELOG_DEVICES_FILE` (default `devices.toml`), `SHELOG_LOG_LEVEL`. The device inventory lives in the gitignored `devices.toml` (see `devices.example.toml`) - never hard-code devices.

## Module map (`src/shelog/`)

- `cli` - argparse entry; `config` - settings, `Secret` wrapper, device inventory; `logging_setup` - stderr logging with the key scrubbed from every line; `clock` - injectable clock.
- `db` + `migrations/` - connection pragmas (WAL), ordered SQL migrations via `PRAGMA user_version`; schema `0001` already holds the tables later phases use.
- `shelly/transport` (the only socket code), `shelly/client` (rate guard, outcome categories, `all_status` call), `shelly/parse` (per-model parsers).
- `store` - dedup decision and the one-transaction cycle writer; `poller` - poll cycle, run loop, backoff, auth state; `lock` - single-instance lock shared by poll and probe.
- `probe` - live API checks with a raw dump to `data/probe/`; `sanitize` - the only way fixtures are produced; `check` - key leak scan.
- `scripts/live.sh`, `scripts/collection_report.py`. Tests are offline: `tests/conftest.py` provides `FakeClock`, `FakeSend`, a sentinel key and a temporary DB.

## Architecture as built

One poller process is the sole writer of the SQLite DB (WAL). Each interval (default 60 s, jittered) it makes one rate-guarded `POST /device/all_status` that returns all devices, parses each configured device, and writes the cycle in one transaction. Failures back off exponentially (capped), honour `Retry-After`, and an auth failure sets a persisted `rejected` state in `meta` with a slower retry. Every attempt writes a `poll_log` row, so gaps are explainable. The single-instance lock stops two pollers (or a poller and a probe) sharing the account's request budget.

## Non-obvious invariants

- **Sample-and-hold data.** Devices sleep and push only on change/interval; the cloud keeps only the last state. Whatever consumes readings later must use forward-fill/step semantics and time-weighted means, never linear interpolation.
- **Timestamp:** the report time is `_updated` (UTC), stored with `ts_source = 'cloud'`; the poll time is used (`'poll'`) only when `_updated` is absent.
- **Dedup on insert:** a row is inserted only when the report time advanced; an unchanged report time is counted as unchanged, a regressed or implausible one is rejected and counted.
- **Store raw, uncalibrated values**; NULL stays NULL (never coerced to 0). Offsets and derived metrics are applied at read time in later phases.
- **`raw_status`** keeps the per-device JSON of every inserted reading.
- **`poll_log.error` is a closed category enum** (`ERROR_CATEGORIES` in `shelly/client`), never exception text, URLs or ids. Logs carry device names, categories and counts only.
- **`RateGuard` spaces any two Shelly requests at least 1.1 s apart**, across hosts and across probe steps.
- **Fixtures only via the sanitizer.** The real-id and key scans in `tests/test_fixture_safety.py` cover every tracked and committable file (run with `SHELOG_REQUIRE_REAL_ID_SCAN=1` and `SHELOG_DEVICES_FILE` pointing at the owner's inventory so a skipped scan counts as a failure).
- The auth key rotates when the account password changes and Shelly can relocate the host: auth failures must stay loud (log line and `meta` state now; health endpoint and banner later).
