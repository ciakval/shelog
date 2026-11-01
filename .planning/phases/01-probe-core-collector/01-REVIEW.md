---
phase: 01-probe-core-collector
reviewed: 2026-11-01T00:00:00Z
depth: standard
files_reviewed: 24
files_reviewed_list:
  - .env.example
  - .gitignore
  - devices.example.toml
  - pyproject.toml
  - scripts/collection_report.py
  - scripts/live.sh
  - src/shelog/__init__.py
  - src/shelog/check.py
  - src/shelog/cli.py
  - src/shelog/clock.py
  - src/shelog/config.py
  - src/shelog/db.py
  - src/shelog/lock.py
  - src/shelog/logging_setup.py
  - src/shelog/migrations/0001_init.sql
  - src/shelog/poller.py
  - src/shelog/probe.py
  - src/shelog/sanitize.py
  - src/shelog/shelly/__init__.py
  - src/shelog/shelly/client.py
  - src/shelog/shelly/parse.py
  - src/shelog/shelly/transport.py
  - src/shelog/store.py
  - tests/conftest.py
  - tests/test_*.py (safety-critical paths only)
findings:
  critical: 0
  warning: 6
  info: 8
  total: 14
status: issues_found
---

# Phase 1: Code Review Report

**Reviewed:** 2026-11-01
**Depth:** standard
**Files Reviewed:** 24 (tests skimmed for weak assertions on safety paths)
**Status:** issues_found

## Summary

Static review only. No live commands and no network were used. `uv run pytest -q` passes (all tests) and `uv run ruff check .` is clean.

The collector core is solid. Writes are single-transaction and idempotent, with `ON CONFLICT DO NOTHING` and rollback on any `BaseException`. Dedup compares the cloud report time to the last stored one. The rate guard sits on every request path, including the probe's wrong-host step. The key travels only in the POST body (or the v2 probe query) and is scrubbed by a formatter that also covers tracebacks. Redirects are refused. Every loop-level exception becomes an `internal` poll_log row instead of killing the process. I found no provable data-loss or secret-leak defect, so there are no BLOCKERs.

The findings that matter for a multi-month unattended run:
- HTML error pages mask the HTTP status in `classify`, so a 401, 403 or 429 HTML page is treated as `endpoint_gone`.
- A request has no overall deadline. This is worse for SIGTERM handling under Docker.
- `Poller` construction sits outside the startup-failure guard.
- Internal errors are logged without a traceback.
- The logged body excerpt can carry device ids.
- The leak scan fails open.

## Warnings

### WR-01: HTML body check runs before the status triage, masking 401/403/429/5xx

**File:** `src/shelog/shelly/client.py:111`
**Issue:** `classify` returns `ENDPOINT_GONE` whenever the body starts with `<`, before it looks at the status. Verified offline: `classify(RawResponse(s, {"retry-after": "30"}, b"<html>..</html>"), 0)` gives `endpoint_gone` for 401, 403, 429, 502 and 503. A CDN, WAF or load-balancer HTML page (typical for 502/503, and for 403 or 429 challenge pages) therefore has these effects:
- A 429 loses `Retry-After` handling.
- A 401/403 never reaches the `auth` category. The persisted `rejected` state and its loud-failure invariant are bypassed.
- A transient 5xx is mislabelled as a permanent "endpoint gone", which Phase 2 alerting would treat as an API change.

The classify test table only uses empty or non-HTML bodies for 4xx/5xx, so this is untested.
**Fix:**
```python
if status in (404, 410) or (status == 200 and raw.body.lstrip().startswith(b"<")):
    return Outcome(ENDPOINT_GONE, status)
```
Then let the 429/401/403/5xx branches run for any body. Add parametrized cases for HTML bodies with 401, 403, 429 and 503.

### WR-02: A request has no overall deadline; slow or hung connections can outlast the 15 s "timeout" and the Docker stop grace

**File:** `src/shelog/shelly/transport.py:70-71`
**Issue:**
- `urlopen(timeout=15)` is a per-socket-operation timeout, not a wall-clock cap. The `resp.read(MAX_BODY + 1)` loop can run unboundedly against a slow-drip server.
- DNS resolution (`getaddrinfo`) is not covered by the timeout at all.
- The SIGTERM handler only sets `stop`. A signal received during a blocking socket call does not interrupt it (PEP 475 retries the call), so shutdown waits for the request to finish.
- Compose's default `stop_grace_period` is 10 s, which is less than the 15 s per-operation timeout. In the worst case the process is SIGKILLed mid-request.
- SQLite makes SIGKILL safe for the data, but "clean exit" is not guaranteed.

For a poller whose value is never hanging, a stuck cycle also delays the next sample indefinitely.
**Fix:** Enforce a total deadline. For example, read in chunks and compare against `time.monotonic()`, and run the request in a worker thread that the main loop waits on with `stop.wait(...)`. Alternatively use `http.client` with an explicit socket deadline. Document `stop_grace_period: 30s` in the Phase 2 compose file, or lower the timeout to about 8 s.

### WR-03: `Poller` construction is outside the startup-failure guard, so a DB error here crash-loops with a raw traceback

**File:** `src/shelog/poller.py:335` (also 93, 96)
**Issue:** `Poller.__init__` runs `store.load_last_states(con)` and `get_meta(con, ...)`. Only `connect_rw` and `migrate` are wrapped in `except (OSError, sqlite3.Error, SchemaTooNew)`. A `sqlite3.DatabaseError` here (corruption, locked DB, `no such table` after a partially applied state) propagates out of `main`. The result is a default-excepthook traceback, which bypasses the redacting formatter, and exit code 1 with no `STARTUP_FAIL_SLEEP_S` pause. Under `restart: unless-stopped` that gives a tight restart loop, and every restart sends a new first request immediately (see IN-05).
**Fix:** Move `Poller(...)` construction inside the same `try` as `connect_rw`/`migrate`, or add `sqlite3.Error` to a guard around it, and route it through `_startup_failed`.

### WR-04: Internal errors are logged by exception type only: no location or traceback for a multi-month unattended run

**File:** `src/shelog/poller.py:111-114` (also 214-216, 133-134)
**Issue:** An unexpected exception in a cycle is logged as `internal error in poll cycle: KeyError` and stored as `internal`. For a bug that appears weeks into the run (an unforeseen Shelly payload shape, a DB edge case) there is nothing to diagnose it from. The "type only" rule was presumably adopted for key safety, but `RedactingFormatter` is documented to scrub tracebacks too. Transport exceptions already drop their causes (`from None`), so they cannot leak URLs.
**Fix:** Use `log.error("internal error in poll cycle", exc_info=err)`. The traceback is scrubbed by the formatter. Add a test with a sentinel key in the exception message and assert it is absent from the output.

### WR-05: The logged `bad_response` excerpt can contain device ids and other identifiers

**File:** `src/shelog/poller.py:201-202`, `src/shelog/shelly/client.py:103-105`
**Issue:** The first 200 characters of an unexpected 200 body are logged at WARNING. A malformed-but-real `all_status` envelope starts with `{"isok":true,"data":{"devices_status":...`, and its first keys are real device ids (and MACs). This contradicts the stated invariant "Logs carry device names, categories and counts only" and the public-repo caution. Phase 2 will ship these logs to Docker, and possibly to OTLP later. The redactor only knows the key.
**Fix:** Mask hex ids, MACs and IPs in the excerpt before logging (reuse the sanitizer's regexes). Alternatively, log the excerpt only when the body is not `isok`-shaped, or log just the top-level key names.

### WR-06: Leak scan fails open on unreadable files and does not cover the probe dump's key placements

**File:** `src/shelog/check.py:61-62`
**Issue:** `_contains` returns `False` on `OSError`. A file that cannot be read (permissions, race with deletion) is counted in "N files" and reported as clean. A check that exists to prove "no trace of the key" should not report success for a file it could not read. The summary line prints `leak scan: N files, 0 hits` with no indication that some were skipped.
**Fix:** Track unreadable files and report them (for example `unreadable: K`). Return non-zero if any exist:
```python
except OSError:
    unreadable.append(path)
    return None   # caller treats None as "could not scan" and exits 1
```

## Info

### IN-01: `SHELOG_POLL_INTERVAL_S` has no upper bound; a huge value crashes the run loop

**File:** `src/shelog/config.py:146-153`, `src/shelog/poller.py:61,271-273`
**Issue:** `float("1e300")` passes `isfinite` and `>= 30`. `Event.wait(1e300)` raises `OverflowError: timestamp out of range for C PyTime_t` (verified). That exception is outside `cycle()`'s try, so it escapes `run`. Owner-controlled input only, but it is a crash path.
**Fix:** Cap the interval (for example `<= 86400`) in `_parse_interval`.

### IN-02: Backoff cap is below the normal interval when `interval > 300`

**File:** `src/shelog/poller.py:39,63-64`
**Issue:** `base = min(CAP_S, interval * 2**exponent)` with `CAP_S = 300`. With interval 600, a failure retries after 150-300 s, which is faster than the healthy cadence. This is the inverse of backoff. It is harmless at the default 60 s.
**Fix:** `base = min(max(CAP_S, interval), interval * 2**exponent)`.

### IN-03: Non-`OSError` `http.client` exceptions escape the transport and are recorded as `internal`

**File:** `src/shelog/shelly/transport.py:77-84`
**Issue:** `http.client.HTTPException` subclasses (`BadStatusLine`, `LineTooLong`, `IncompleteRead` on chunked bodies, `InvalidURL`) are not `OSError`. They are not mapped to `network`, so a flaky upstream produces `internal` rows and "internal error" logs. That misleads diagnosis, because `internal` should mean our bug. They do not crash the loop.
**Fix:** `except (OSError, http.client.HTTPException): raise TransportError("network") from None`.

### IN-04: `n_rejected` is inflated every cycle for an unchanged report with a bad field

**File:** `src/shelog/poller.py:222`
**Issue:** `n_rejected += parsed.field_rejects` happens before the dedup decision. A persistently out-of-range or garbage field is re-counted on every poll even when the report is unchanged (the cloud holds the same last state), while the warning is logged only on insert. The count therefore does not mean "reports rejected".
**Fix:** Add `field_rejects` only when `decision.action == INSERT`.

### IN-05: Phase 2 readiness: relative paths, per-directory lock, immediate first request

**File:** `src/shelog/config.py:158,179-180`, `src/shelog/poller.py:270`, `src/shelog/lock.py`
**Issue:** Notes for the Docker/PID 1 work:
- `.env`, `devices.toml` and `data/shelog.db` all resolve against the cwd. The image needs `WORKDIR` set deliberately, or the `SHELOG_*` path variables set.
- `devices.toml` is gitignored, so it must be bind-mounted (or the plan changed). A missing file exits 2 immediately, which gives a restart loop.
- The first request goes out at process start (`deadline = clock.monotonic()`). Rapid crash and restart cycles can send two requests less than 1.1 s apart across processes, because `RateGuard` is per-process. The `flock` is per data directory, not per account. During the cutover, the interim local poller and the VPS poller will both run (each at 1 req/min, so the budget is fine, but not enforced).
- Signal handlers are correctly installed, so PID 1 works. Use exec-form `command:` and `stop_grace_period` (see WR-02).
- Uncaught exceptions print through the default excepthook, which bypasses `RedactingFormatter`.

**Fix:** Persist the last request time (for example in `meta`) and have `RateGuard` honour it at startup. Add a startup delay of at least 1.1 s. Consider a top-level `try/except` in `cli.main` that logs via the redacting logger.

### IN-06: Sanitizer SSID and coordinate detection is exact-key and numeric-only (fail-open for the public repo)

**File:** `src/shelog/sanitize.py:132,250,354,364`
**Issue:** SSIDs are discovered and scanned only under the lowercase key `ssid`. Variants (`SSID`, `sta_ssid`, `ssid1`) would be neither mapped nor flagged, since short strings pass the 20+ character `_LONG_TOKEN` rule. Coordinates are redacted and scanned only when numeric. A string-typed `lat`/`lng` would pass untouched. The residual protection is the real-id scan, which does not cover SSIDs or coordinates. The current live captures are presumably fine, but a firmware change could regress this silently.
**Fix:** Match keys case-insensitively and by suffix (`key.lower().endswith("ssid")`). In `scan`, flag any non-pseudo value under such keys and any non-zero or string value under coordinate keys.

### IN-07: `raw_status` stores the full device block (SSID, IP, MAC) in the DB

**File:** `src/shelog/shelly/parse.py:177`, `src/shelog/store.py:160`
**Issue:** `raw_status.body` keeps `wifi_sta`, IP, MAC and similar fields. The DB is private and gitignored, which is acceptable now. The Phase 3 public web service must never expose `raw_status`, and it must not be included in any public export or backup. Worth recording as a constraint now. The DB files are also created with the default umask (0644), and only the directory is 0700.
**Fix:** Note in the Phase 3 plan that `raw_status` is private and the web role gets explicit column lists only. Optionally `os.chmod` the DB file to 0600.

### IN-08: `collection_report.py` opens the DB with `mode=ro`, which fails if the WAL sidecars do not exist

**File:** `scripts/collection_report.py:55-57`
**Issue:** `CLAUDE.md` itself lists `mode=ro` as an anti-pattern for WAL databases. The poller leaves `-wal`/`-shm` alive while running, so this works. After a clean poller shutdown (sidecars removed) the report may fail with `unable to open database file` and exit 2. The failure is loud, not wrong, so this is low impact. The `mean_gap_s` 50-70 pass range is also hard-coded to the 60 s default interval.
**Fix:** Open `mode=rw` with `PRAGMA query_only=ON`, as the stack notes recommend, and derive the gap range from an `--interval` option.

---

_Reviewed: 2026-11-01_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
