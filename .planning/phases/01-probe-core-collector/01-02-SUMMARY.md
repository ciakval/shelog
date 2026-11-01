---
phase: 01-probe-core-collector
plan: 02
subsystem: collector
tags: [python, sqlite, shelly-cloud, parser, dedup, poller, migrations]
requires:
  - phase: 01-01
    provides: "config, clock, rate-guarded ShellyClient, cli skeleton, shared test fakes"
provides:
  - "shelog poll --once: one rate-guarded all_status request lands readings, raw JSON and a poll_log row in SQLite in one transaction"
  - "Schema 0001 (poll_log, reading, raw_status, outdoor, alert_state, meta; STRICT) with PRAGMA user_version migration runner, shipped in the wheel"
  - "shelly/parse.py per-model parsers keyed on _updated; store.py decide()/write_cycle(); poller.py Poller.step()/main()"
  - "tests/conftest.py tmp_db and envelope fixtures; synthetic all-models envelope; test_tracer.py"
affects: [01-03, 01-04, 01-05, 01-06]
tech-stack:
  added: []
  patterns:
    - "Single BEGIN IMMEDIATE transaction per cycle; poll_log counts corrected from real rowcounts"
    - "Per-device try/except around parsing; logs carry device NAMES and categories only"
    - "Package-resource migrations NNNN_*.sql applied with executescript inside an explicit transaction"
key-files:
  created:
    - src/shelog/migrations/0001_init.sql
    - src/shelog/db.py
    - src/shelog/shelly/parse.py
    - src/shelog/store.py
    - src/shelog/poller.py
    - tests/test_tracer.py
    - tests/fixtures/shelly/synthetic_envelope_all_models.json
  modified:
    - src/shelog/cli.py
    - tests/conftest.py
key-decisions:
  - "After a partial insert (rowcount 0 on some rows) the poller reloads last_states from the DB instead of guessing which rows landed"
  - "A response containing none of the configured devices is turned into the no_devices failure category (ok=0)"
requirements-completed: [COLL-01, COLL-04, COLL-05, COLL-06]
duration: ~20 min
completed: 2026-11-01
status: complete
commits: 2
plan_head_before: 618abda699d282d80fc17d4e50a184fc29f94c28
plan_head_after: aeae7e3876084d88a9e29dde3fd679039909a96e
actuals:
  tokens: 14000
  tasks: 2
  commits: 2
---

# Phase 1 Plan 02: Poll slice Summary

**`shelog poll --once` now takes one rate-guarded `POST /device/all_status` through per-model parsers, dedup on the cloud `_updated` time, and a single SQLite transaction that writes raw readings, raw per-device JSON and a poll_log row.**

## Accomplishments

- Live tracer verified against the real account: `scripts/live.sh poll --once` exited 0 after exactly one request. Aggregate results in the main checkout's gitignored `data/shelog.db`: 7 distinct devices in `reading`, last poll_log row `ok=1, n_devices=7, n_new=7, n_unchanged=0, n_rejected=0, latency 88 ms`, `reading` count equals `raw_status` count, 0 readings with `ts_source <> 'cloud'`, `PRAGMA user_version` = 1. One reading (the Flood sensor) has NULL rh as designed; none has NULL temperature.
- Schema 0001 holds all six tables from day one and ships inside the wheel (`0001_init.sql` found in the built wheel).
- Offline tracer test (11 tests) locks the cycle: exact values with NULL rh for the flood sensor, replay inserts nothing and counts 3 unchanged, one advanced `_updated` inserts exactly one row, key only in the form body, HTTP 503 yields a `http_5xx` row with no readings, no-devices and timeout categories, migrate idempotence plus SchemaTooNew, and a raising parser for one device not aborting the cycle (n_rejected 1, other two stored).
- Full suite: 21 passed; ruff check and format clean.

## Task Commits

1. Task 1 (tracer): `f02df97` feat(01-02): shelog poll --once tracer
2. Task 2: `aeae7e3` test(01-02): offline tracer tests over a synthetic all-models envelope

## Deviations from Plan

None - plan executed as written. Note: Task 2 is `tdd="true"` but, as in plan 01-01, the plan puts the implementation in Task 1, so the tests were written after it and passed on first run (no RED commit). The plan type is `execute`, so no TDD gate applies.

## Authentication Gates

None.

## Known Stubs

None. The parser covers only the plan's minimal rules; the `sensor_error`, absurd-value and missing-`_updated` fallback refinements are explicitly assigned to plan 01-03, and the loop/backoff/lock to plan 01-04.

## Threat Flags

None. Parameterized SQL only; `raw_status` holds LAN details but the DB is gitignored (as modelled in T-01-04/T-01-05/T-01-37).

## Issues Encountered

None.

## Next Phase Readiness

Seams (`parse_device`, `decide`, `write_cycle`, `Poller.step`, `main`, `tmp_db`, `envelope`) match the interfaces block for the wave-3 plans. `data/shelog.db` already holds the first real readings.

## Self-Check: PASSED

- Files exist: all 7 created and 2 modified files present.
- Commits `f02df97` and `aeae7e3` are ancestors of HEAD.
- `uv run ruff check .`, `uv run ruff format --check .` and `uv run pytest` (21 passed) green; the wheel contains `0001_init.sql`.
