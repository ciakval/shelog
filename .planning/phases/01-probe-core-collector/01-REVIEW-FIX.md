---
phase: 01-probe-core-collector
fixed_at: 2026-11-01T00:00:00Z
review_path: .planning/phases/01-probe-core-collector/01-REVIEW.md
iteration: 1
findings_in_scope: 8
fixed: 8
skipped: 0
status: all_fixed
---

# Phase 1: Code Review Fix Report

**Fixed at:** 2026-11-01
**Source review:** .planning/phases/01-probe-core-collector/01-REVIEW.md
**Iteration:** 1

**Summary:**
- Findings in scope: 8 (WR-01..WR-06, plus Info items IN-01 and IN-03 as requested)
- Fixed: 8
- Skipped: 0

Verification ran in the main checkout (no worktree; commits made directly on `main` as approved).
Final gate: `uv run ruff check . && uv run ruff format --check . && SHELOG_REQUIRE_REAL_ID_SCAN=1 uv run pytest -q` all green. No live commands were run, and `data/`, `devices.toml` and the secrets file were not touched.

## Fixed Issues

### WR-01: HTML body check runs before the status triage

**Files modified:** `src/shelog/shelly/client.py`, `tests/test_client.py`
**Commit:** fc59a29
**Applied fix:** `classify` now maps HTML to `endpoint_gone` only for a 200 (or 404/410); 401/403 become `auth`, 429 `rate_limited` (keeping `Retry-After`), 5xx `http_5xx`, regardless of body. Added parametrized HTML-body tests for 401, 403, 429, 500, 502, 503, 418, 404, 410 plus a `Retry-After` test.

### WR-02: A request has no overall deadline

**Files modified:** `src/shelog/shelly/transport.py`, `tests/test_transport.py`
**Commit:** 4006cb2
**Applied fix:** `send(timeout=)` is now a wall-clock bound over DNS, connect, headers and body. The exchange runs in a daemon worker thread and the caller stops waiting at the deadline (raising `TransportError("timeout")`). The body is read in chunks with `read1` and a deadline check, so an abandoned worker ends itself. Stdlib only. New loopback tests cover a slow-drip body (no single socket read times out) and a connection that accepts but never answers.
**Note:** The `timeout` is still 15 s. Phase 2 compose should set `stop_grace_period` of about 20-30 s (the SIGTERM handler still only sets `stop`, so shutdown during an in-flight request can wait up to the timeout). The request is not interrupted by `stop`; only bounded.

### WR-03: `Poller` construction outside the startup-failure guard

**Files modified:** `src/shelog/poller.py`, `tests/test_poller.py`
**Commit:** 26141ad
**Applied fix:** `Poller(...)` is wrapped so `sqlite3.Error` goes through `_startup_failed` (exit 1, `STARTUP_FAIL_SLEEP_S` pause in loop mode, type-only log). Regression test patches `load_last_states` to raise `DatabaseError` with a sentinel key in the message and asserts exit 1, the sleep, no sends, and no key in the log.

### WR-04: Internal errors logged by type only

**Files modified:** `src/shelog/poller.py`, `tests/test_poller.py`
**Commit:** b4a9e26
**Applied fix:** Deviation from the review's suggestion (`exc_info`): a new `where(err)` helper logs the exception type plus the innermost four call sites as `file.py:line function`, with no message text and no source lines. Exception messages (for example `KeyError('<device id>')`) can carry payload ids, which would contradict WR-05 and the "ids never in logs" invariant. Applied to the cycle-level internal error, the "could not record internal error" log and the per-device parse-error warning. Tests assert the location format and that neither the sentinel key nor an id in the message is logged.

### WR-05: `bad_response` excerpt can contain device ids

**Files modified:** `src/shelog/shelly/client.py`, `src/shelog/poller.py`, `tests/test_client.py`, `tests/test_poller.py`
**Commit:** 96cb3d4
**Applied fix:** `Outcome.body_excerpt` is replaced by `Outcome.body_shape`, for example `"500 bytes, not json"` or `"6 bytes, json list"`: size and kind only, never content or key names. The poller logs `unexpected response body: <shape>`. Tests assert a body containing a device id and a MAC leaks neither into the outcome nor into the log. `poll_log.error` is unchanged (closed enum).

### WR-06: Leak scan fails open on unreadable files

**Files modified:** `src/shelog/check.py`, `tests/test_secrets.py`
**Commit:** 53b401f
**Applied fix:** `_contains` returns `None` on `OSError`. The summary line is now `leak scan: N files, H hits, K unreadable`, each unreadable file gets an `unreadable: <path>` line, and the exit code is 1 when there are hits or unreadable files. Regression test uses a `chmod 000` file (skipped as root).

### IN-01: `SHELOG_POLL_INTERVAL_S` has no upper bound

**Files modified:** `src/shelog/config.py`, `tests/test_config.py`, `.env.example`, `CLAUDE.md`
**Commit:** d456432
**Applied fix:** Interval must be within 30..86400 seconds; larger values (`86401`, `1e9`, `1e300`) raise `ConfigError` at load. Tests extended; `.env.example` and CLAUDE.md state the maximum.

### IN-03: Non-`OSError` `http.client` exceptions escape the transport

**Files modified:** `src/shelog/shelly/transport.py`, `tests/test_transport.py`
**Commit:** 387a370
**Applied fix:** `http.client.HTTPException` is caught alongside `OSError` and mapped to `TransportError("network")`. Regression test serves a malformed status line (confirmed to fail before the fix with `BadStatusLine`).

## Skipped Issues

None in scope. Not attempted by instruction: IN-02, IN-04, IN-05, IN-06, IN-07, IN-08.

---

_Fixed: 2026-11-01_
_Fixer: Claude (gsd-code-fixer)_
_Iteration: 1_
