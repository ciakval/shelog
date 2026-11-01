---
phase: 01-probe-core-collector
plan: 05
subsystem: config-and-secret-hygiene
tags: [python, config, logging, redaction, leak-scan, tomllib, pytest]
requires:
  - phase: 01-01
    provides: "config tracer (Secret, Settings, Device, read_dotenv, load_settings), cli, conftest fixtures"
  - phase: 01-02
    provides: "poller.main(settings, devices, once=...), db.connect_rw/migrate, tmp_db fixture"
  - phase: 01-04
    provides: "db.set_meta"
provides:
  - "Strict config.load_settings / load_devices / normalize_host (same signatures as 01-01)"
  - "logging_setup.RedactingFormatter + setup_logging(level, secrets): one UTC stderr handler, key scrubbed from the fully formatted record"
  - "check.run_check / check.main and `shelog check`: leak scan of db/wal/shm and data/, counts and file names only"
  - "devices.example.toml (7 pseudo-id devices, max_silence_s) and .env.example (every variable, empty Shelly values)"
affects: [01-06, 01-07, 01-08]
tech-stack:
  added: []
  patterns:
    - "Redact the final formatted string, so msg, %-args, tracebacks and stack_info are covered whatever handler order"
    - "Chunked file scan with overlap of the longest needle minus one, so a key split across chunks is found"
    - "ConfigError carries a value-free message; CLI prints err.args[0] so the exception-text source scan stays strict"
    - "Example files are loaded by tests through the real validator so they cannot drift"
key-files:
  created:
    - src/shelog/logging_setup.py
    - src/shelog/check.py
    - devices.example.toml
    - .env.example
    - tests/test_config.py
    - tests/test_secrets.py
  modified:
    - src/shelog/config.py
    - src/shelog/cli.py
key-decisions:
  - "run_check resolves stdout per call (stdout=None default) instead of binding sys.stdout at import time, so capture and redirection work; the call signature is otherwise as specified"
  - "cli prints `config error: {err.args[0]}` rather than `{err}`: ConfigError messages are value-free by construction, and avoiding the `{err}` form keeps the source scan free of an allow-list"
  - "Host warning for non-*.shelly.cloud hosts goes through the `shelog.config` logger and never echoes the host; it fires before setup_logging so it uses logging's last-resort stderr handler (no secret in it)"
  - "rh_offset on flood_gen1 is rejected even when 0.0 (per research Pattern 7); the owner's real devices.toml was adjusted accordingly"
requirements-completed: [COLL-07, COLL-08]
duration: ~25 min
completed: 2026-11-01
status: complete
commits: 4
plan_head_before: 08dedf88ec8b26e2bb67318332f7c46167a397c4
plan_head_after: cba8504720e08d2e9246db4a8bbc4e8082c50faf
actuals:
  tokens: 11000
  tasks: 2
  commits: 4
---

# Phase 1 Plan 05: Strict config and secret hygiene Summary

**Settings and devices.toml are validated strictly with value-free errors, every log line passes through a formatter that scrubs the raw, quote and quote_plus forms of the Shelly key (tracebacks included), and `shelog check` proves against the real key that the DB, WAL, probe output and logs hold no trace of it.**

## Accomplishments

- Host: `normalize_host` strips `https://` and trailing `/`, rejects `http://`, and then requires `[a-z0-9.-]+`, which rules out `@`, `:`, `/`, `?`, `#` and whitespace. A host outside `*.shelly.cloud` is accepted with exactly one WARNING that does not contain the host.
- Key: empty, `CHANGE_ME` in any case, or shorter than 16 characters is refused; the message names the variable and carries neither the value nor its length (asserted in tests). Interval floor is 30 s and must be finite; log level must be one of DEBUG/INFO/WARNING/ERROR.
- Dotenv: `export` prefix, comments, single/double quotes and `=` inside values parse; `$OTHER` stays literal; real environment variables win; `os.environ` is untouched.
- Devices: unknown keys, duplicate ids (case-insensitive) and names, bad group/model, id regex (6 or 12 hex, lower-cased), bool/NaN/inf offsets, `rh_offset` on a flood, and non-positive/float/bool `max_silence_s` all fail at startup. Errors name the device (`device 'Alpha'`), falling back to `device #N`, and never the id (tested with an id that must not appear).
- `RedactingFormatter` renders the full record first and then replaces every secret form (raw, `quote(safe="")`, `quote_plus`, longest first), so message, args, `log.exception`, and `stack_info` are all scrubbed. Timestamps are UTC with a `Z` suffix; `httpx`, `httpcore`, `urllib3` are held at WARNING; `setup_logging` leaves exactly one root handler.
- `shelog check`: scans the DB, its `-wal` and `-shm`, and every file under `data/` recursively, in 1 MiB chunks with needle-length overlap (a test plants the quote_plus form straddling the chunk boundary). It reads `user_version` through a `mode=ro` URI only when the DB exists, so a missing DB is reported as `db schema: missing` and never created. Output is one summary line plus `hit: <relative path>` lines; no key, part, or length is printed.
- Source scan test: no `src/shelog` line passes an exception object or its `str()` to a log or print call (`str(e)`, `{e}`, `{exc}`, `{err}`, `%s, exc`); a companion test proves the patterns do flag the bad forms and pass `type(err).__name__`.
- Live result (real key, `scripts/live.sh check`, exit 0):
  `config: ok | devices: 7 | db schema: v1 | leak scan: 3 files, 0 hits`
  The 3 files are the tracer DB and its WAL/SHM; there was no probe or log file under `data/` at the time.
- Suite: 298 passed; ruff check and format clean; `uv.lock` untouched. `SHELOG_REQUIRE_REAL_ID_SCAN=1` fixture-safety scan against the real devices file: 12 passed (covers the two new example files, which use pseudo ids 020000000001..07 and empty secret values).

## Task Commits

1. Task 1 RED: `f233bb2` test(01-05): strict settings, host and devices.toml validation (RED)
2. Task 1 GREEN: `8eac596` feat(01-05): strict settings and devices.toml validation, example files
3. Task 2 RED: `cf8b252` test(01-05): key redaction, leak scanner and exception-text source scan (RED)
4. Task 2 GREEN: `cba8504` feat(01-05): redacting log formatter and shelog check leak scanner

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Owner's real devices.toml failed the stricter validator**
- **Found during:** Task 1 final verify command
- **Issue:** The gitignored real `devices.toml` had `rh_offset = 0.0` on the LR-Flood (flood_gen1) entry, which the spec forbids (a Flood has no humidity sensor).
- **Fix:** Removed that single no-op line from the real file (the plan's verify text permits "fix the validator rule or the real file"). The file is gitignored; nothing in the repo changed for this. Real file now loads with 7 devices.
- **Files modified:** devices.toml (untracked, local only)
- **Commit:** none (gitignored)

**2. [Rule 1 - Bug] `check.run_check` default `stdout=sys.stdout` bound at import time**
- **Found during:** Task 2 (CLI test saw empty captured output)
- **Issue:** A default of `sys.stdout` is evaluated at definition time, so output redirection/capture did not apply.
- **Fix:** Default is `None`, resolved to `sys.stdout` at call time. Callers passing `stdout=` explicitly are unaffected.
- **Files modified:** src/shelog/check.py
- **Commit:** cba8504

**Total deviations:** 2 (Rule 3 on a local file, Rule 1 small). **Impact:** none on frozen interfaces; `cli.main`, `poller.main` and `probe.main` call sites are unchanged apart from the new `check` branch.

## Notes

- `cli._configure_logging` was removed in favour of `logging_setup.setup_logging`; nothing else referenced it.
- The non-`*.shelly.cloud` host warning is emitted during `load_settings`, before `setup_logging` runs, so it goes out through logging's last-resort handler (stderr, message only). It contains no secret by construction.
- Plan 01-07 still owes `docs/shelly-api.md` the per-device `max_silence_s` values and sources, as referenced in `devices.example.toml`.

## Known Stubs

None.

## Threat Flags

None. No new network endpoint; `check` only reads local files and opens the DB read-only.

## Self-Check: PASSED

- Created files present: logging_setup.py, check.py, devices.example.toml, .env.example, test_config.py, test_secrets.py.
- Commits f233bb2, 8eac596, cf8b252, cba8504 are ancestors of HEAD.
- Acceptance criteria re-run: example devices load with 7 entries and `max_silence_s`; `pytest -k example` selects 3 tests and passes; real devices.toml loads (7); `scripts/live.sh check` output contains `, 0 hits` and `db schema: v1`; `grep -c 'setup_logging(' src/shelog/cli.py` = 1; `grep -c RedactingFormatter src/shelog/logging_setup.py` = 2; full suite 298 passed.
