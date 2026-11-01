---
phase: 01-probe-core-collector
plan: 04
subsystem: collector
tags: [python, scheduler, backoff, rate-limit, flock, urllib, pytest]
requires:
  - phase: 01-01
    provides: "config, clock, urllib transport, client (RateGuard, classify)"
  - phase: 01-02
    provides: "Poller.step, store.write_cycle, migrations, tmp_db fixture"
provides:
  - "poller.next_delay/run/main: deadline scheduler, jittered cadence, capped backoff, Retry-After, auth floor"
  - "Persisted auth state machine (meta shelly_auth, shelly_auth_since) with two-outcome confirmation"
  - "Loop-boundary 'internal' poll_log row, never-stored-device watchdog, bad_response excerpt logging"
  - "lock.acquire_instance_lock/AlreadyRunning (flock) shared by poll now and probe in 01-06"
  - "db.set_meta/get_meta"
  - "Real-socket transport tests and a full classifier table; synthetic_rate_limit.json fixture"
affects: [01-05, 01-06, 01-07, 01-08]
tech-stack:
  added: []
  patterns:
    - "Monotonic deadlines: ok cycles schedule from the previous deadline, failures from now, never a catch-up burst"
    - "Equal-jitter exponential backoff with injected random.Random for deterministic tests"
    - "Poller.cycle() is the loop boundary: only Interrupted escapes, every other exception becomes an 'internal' poll_log row logged by type name"
key-files:
  created:
    - src/shelog/lock.py
    - tests/test_poller.py
    - tests/test_client.py
    - tests/test_transport.py
    - tests/fixtures/shelly/synthetic_rate_limit.json
  modified:
    - src/shelog/poller.py
    - src/shelog/db.py
    - src/shelog/shelly/client.py
key-decisions:
  - "A non-auth outcome between two auth outcomes resets the auth streak (consecutive means consecutive)"
  - "Poller.cycle() (step + auth observation + internal capture) is shared by run() and main(once=True), so --once rows and auth state behave like the loop"
  - "main() installs SIGTERM/SIGINT handlers only on the main thread and restores the previous handlers on exit, so tests and embedding callers are not left with ours"
  - "Failure backoff is uncapped-exponent safe (exponent clamped at 30) so a multi-day outage cannot overflow"
requirements-completed: [COLL-01, COLL-02, COLL-05]
duration: ~30 min
completed: 2026-11-01
status: complete
commits: 2
plan_head_before: 890577663e796b36cdfed56ab847b758e39cf2ac
plan_head_after: f4daf9dae84aaf69499471c6bb5f342467023b9b
actuals:
  tokens: 11000
  tasks: 2
  commits: 2
---

# Phase 1 Plan 04: Hardened collector loop Summary

**The tracer's fixed-interval loop is now a deadline scheduler with jittered cadence, capped equal-jitter backoff, Retry-After and a persisted auth-rejected state, writes a poll_log row for every attempt (including its own bugs), and is guarded by a single-instance flock and clean SIGTERM shutdown.**

## Accomplishments

- `next_delay`: ok gives interval x U(0.9, 1.1); failures give base/2 + U(0, base/2) with base = min(300, interval x 2^(n-1)); `rate_limited` is raised to at least Retry-After (clamped to [1, 900] by the client); `auth` is floored at 300 s; everything is floored at 1.1 s.
- Observed failure delay sequence (seed 1234, interval 60, failures 1..6, category `network`): 59.0, 86.4, 120.9, 286.6, 290.9, 237.3 seconds. Pinned in `test_observed_failure_delay_sequence_for_seed_1234`.
- `run`: monotonic deadlines. After ok the next deadline is previous deadline + delay (no drift); if that is already past, resume at now + 1.1 s (no catch-up burst after an outage). After a failure it is now + delay. Waits go through `clock.wait(stop, ...)`, so SIGTERM ends the wait immediately and `run` returns 0.
- A 6-hour FakeClock simulation (about 300 sends, mixing 5xx, timeout, network, 429 with Retry-After 120, non-JSON body, 404, a RuntimeError, 418, empty device list, 401 and `isok:false max_req`) asserts: every send at least 1.1 s after the previous one, the send after the 429 at least 120 s later, exactly one poll_log row per send, exactly one `internal` row, every ok=0 row has an error. A second run pins cadence: N steady cycles take within N x 60 x [0.9, 1.1] and the first send after a 10-failure streak is at least 1.1 s after the last failure.
- Auth: one auth outcome never flips the state; two consecutive ones set `meta.shelly_auth='rejected'` plus `shelly_auth_since`, with at least 300 s between them; the next ok writes `ok`. Exactly one ERROR ("rejected the auth key") and one INFO ("accepted the auth key again") per transition, and the sentinel key never appears in captured logs.
- Loop boundary: any exception except `Interrupted` out of a cycle is logged by type name only (never `str(exc)`), written as `PollRecord(ok=0, error='internal')`, and counted as a failure for backoff.
- Watchdog: a configured device present in 10 ok cycles with no stored reading logs exactly one WARNING naming the device NAME (tested with an implausible `_updated`).
- `bad_response`: WARNING "unexpected response body (truncated)" with at most 200 characters (`%r`-escaped), via the new `Outcome.body_excerpt`; nothing from the body reaches the DB.
- `main`: lock beside the DB before any network call (exit 3 with a stderr message if held), startup failures (lock OSError, DB open/migrate error, `SchemaTooNew`) return 1 immediately with `--once` and after a 30 s signal-interruptible sleep in loop mode, so a restart policy cannot turn them into a request storm.
- Transport proven on a real loopback `ThreadingHTTPServer`: key only in the body, 429 headers lower-cased, 307 not followed (one request recorded), timeout maps to `timeout`, closed port to `network`, body capped at MAX_BODY + 1, `TransportError` text equals its category with no `__cause__`. The transport needed no code change.
- `synthetic_rate_limit.json`: three clearly synthetic envelopes (429 with Retry-After, 429 without, 200 `isok:false errors.max_req`), all classify to `rate_limited`. The real throttle envelope remains unverified and is never provoked live.
- Suite: 211 passed; ruff check and format clean; `uv.lock` untouched. `SHELOG_REQUIRE_REAL_ID_SCAN=1` fixture-safety scan against the real devices file: 12 passed.

## Task Commits

1. Task 1: `c4e9d66` test(01-04): prove transport on real sockets, pin classifier table and synthetic rate-limit fixture
2. Task 2: `f4daf9d` feat(01-04): deadline scheduler, capped backoff, auth state, internal-error capture, instance lock

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Task 1 tests passed for the transport without code changes**
- **Found during:** Task 1
- **Issue:** The plan expected fixes to transport.py; the tracer transport already satisfied every behavior (no redirect, cap, mapping, `from None`). Only `client.classify` needed the new `body_excerpt`.
- **Fix:** Left transport.py untouched; added `Outcome.body_excerpt` and a `_bad_response` helper in client.py.
- **Files modified:** src/shelog/shelly/client.py
- **Commit:** c4e9d66

**2. [Rule 3 - Blocking] `Poller.cycle()` added as a shared entry point**
- **Found during:** Task 2
- **Issue:** The plan specifies the loop-boundary capture and auth observation in `run`, but `main(once=True)` also needs them (otherwise `--once` skips auth state and internal rows).
- **Fix:** Put the boundary in a new `Poller.cycle(stop) -> (category, retry_after_s)` used by both `run` and `main`. Frozen signatures are unchanged.
- **Files modified:** src/shelog/poller.py
- **Commit:** f4daf9d

**Total deviations:** 2 (both Rule 3, additive). **Impact:** none on frozen interfaces; 01-05 still calls `poller.main(settings, devices, once=...)`.

## Test-design notes

- The plan's "one row per send plus one for the RuntimeError" is realized as rows == sends, with the raising send being the one that carries the `internal` row (a raising send never reaches the normal row writer).
- Tests that make the dir unwritable skip when run as root.

## Known Stubs

None.

## Threat Flags

None. No new network endpoint or trust boundary; the new lock file `poller.lock` lives in the data directory.

## Self-Check: PASSED

- Created files present: lock.py, test_poller.py, test_client.py, test_transport.py, synthetic_rate_limit.json.
- Commits c4e9d66 and f4daf9d are ancestors of HEAD.
- Acceptance criteria re-run: constants tuple, backoff cap <= 300, `acquire_instance_lock` referenced in poller.py, ERROR_CATEGORIES slug check, rate-limit fixture check, real-socket test present, full suite passing.
