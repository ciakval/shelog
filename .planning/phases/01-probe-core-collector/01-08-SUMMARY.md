---
phase: 01-probe-core-collector
plan: 08
subsystem: acceptance
tags: [leak-test, acceptance-report, live-run, drills, claude-md]
requires:
  - phase: 01-07
    provides: "pinned classifier, fixtures, findings doc, sanitizer"
  - phase: 01-04
    provides: "poller loop, backoff, auth state"
  - phase: 01-05
    provides: "shelog check, redacting logger"
provides:
  - "tests/test_leak_e2e.py: sentinel-key leak test across every poll outcome and the probe"
  - "scripts/collection_report.py: aggregate-only, id-free acceptance report (reused for the Phase 2 production check)"
  - "CLAUDE.md describing the built collector"
  - "Live acceptance numbers: one-hour run, wrong-key drill, outage drill, real-key leak scan"
affects: [phase-02-deploy]
tech-stack:
  added: []
  patterns:
    - "Acceptance is a read-only aggregate SQL report with PASS/FAIL per check and a non-zero exit on failure"
key-files:
  created:
    - tests/test_leak_e2e.py
    - scripts/collection_report.py
    - tests/test_collection_report.py
  modified:
    - CLAUDE.md
key-decisions:
  - "collection_report gained --min-attempts (default 2) and --now (testing) beyond the planned --db/--since/--expect-devices; an empty window fails instead of passing vacuously"
  - "The duplicate check groups by (device_id, ts) across sources, stricter than the table key (device_id, ts, source)"
  - "The leak test imports the probe step matrix from tests/test_probe.py rather than duplicating it"
requirements-completed: [COLL-01, COLL-02, COLL-03, COLL-04, COLL-05, COLL-06, COLL-07]
duration: ~100 min (of which about 62 min was the live hour run)
completed: 2026-11-01
status: complete
commits: 3
plan_head_before: 0262b0a1d81b964f8c19494d6932ed6ec2ffaa58
plan_head_after: c2ad692a24c408e53371ca0d46a2c6cf0660a94f
actuals:
  tokens: 12000
  tasks: 2
  commits: 3
---

# Phase 1 Plan 08: Whole-system leak proof, acceptance report, live hour run and drills Summary

**The collector ran against the real account for 62 minutes (63 attempts, 98.4 % ok, mean gap 59.9 s, never two requests in the same second, 7 devices, 64 readings each with raw JSON); wrong-key and outage drills behaved as designed; a sentinel-key end-to-end test and a live real-key scan both found zero traces of the key.**

## Accomplishments

- **End-to-end leak test.** `tests/test_leak_e2e.py` drives `poller.run` (FakeClock/FakeSend) through 14 scripted requests: ok, ok unchanged, auth, auth, 429 with Retry-After 120, HTTP 200 `isok:false max_req`, 503, 418, timeout, network, HTML 200, NaN body, `devices_status: []`, and a callable raising a RuntimeError that embeds the full request form (a simulated leaky library). All ten error categories appear in `poll_log`, `meta shelly_auth` is `rejected`, the send after the 429 is at least 120 s later, and no gap is under 1.1 s. The probe is then run through the default, `--errors` and `--wrong-host` groups with the same sentinel. The sentinel (raw, `quote`, `quote_plus`) is absent from captured stdout/stderr, `shelog.db`, `-wal`, `-shm` and every probe capture file; calls to a non-configured host never carry it; `run_check` returns 0 with `0 hits`. Sensitivity was proven by a throwaway mutation (log the exception text and disable redaction): the test failed, then the mutation was reverted.
- **Acceptance report.** `scripts/collection_report.py` (stdlib, `mode=ro`) prints `name | value | verdict` per check plus a verdict line, exit 0/1 (2 if the DB is missing or unreadable). 12 tests cover a healthy DB and each planted failure (same-second pair, uncategorised failure, low ok ratio, wrong device count, implausible ts, duplicate across sources, missing raw_status, empty window) and assert no device id appears in output.
- **CLAUDE.md.** Rewritten to the built design (Python 3.13, stdlib urllib, `shelog` CLI, module map, poller architecture, invariants, `scripts/live.sh`). The `## Secrets and live credentials` paragraph is kept verbatim and is the single live-run rule; the pre-code web/resampling/derived-metric/development-order sections were removed and replaced by one pointer to SPEC.md and the ROADMAP.

## Live acceptance (aggregate only)

One-hour run, `timeout --preserve-status 3720 scripts/live.sh poll` from the main checkout; exit status 0 (clean SIGTERM stop). Log first/last timestamps 08:01:54Z / 09:03:50Z (3716 s).

| check | value | verdict |
|---|---|---|
| attempts | 63 | PASS |
| ok_ratio | 0.984 | PASS |
| mean_gap_s | 59.9 | PASS |
| same_second_pairs | 0 | PASS |
| failed_without_category | 0 | PASS |
| n_rejected_sum | 0 | info |
| distinct_devices | 7 | PASS |
| implausible_ts | 0 | PASS |
| duplicate_device_ts | 0 | PASS |
| readings_vs_raw_status | 64/64 | PASS |
| ts_source_not_cloud | 0 | info |
| verdict | all checks pass | PASS |

Extra aggregates: gaps between attempts 54 s to 74 s (the 74 s follows the one failed attempt, a 15 s timeout); the only non-ok row is one `timeout` (no HTTP status); 62 ok cycles each saw all 7 configured devices; 57 readings were inserted in the window; `poll.log` holds no WARNING or ERROR line other than that expected `poll failed: timeout`. It contains log lines only (no stack dumps).

Wrong-key drill (dummy key, `SHELOG_DB_PATH=data/drill.db`, 420 s): 2 `auth` rows (HTTP 401) 300 s apart, `meta shelly_auth = rejected`, log line "rejected the auth key; retrying every 300 s" after the second. The real key was not sent.

Outage drill (dummy key, reserved unresolvable host, interval 30 s, 240 s): 4 `network` rows with NULL http_status, gaps 16 s, 56 s, 108 s (growing, capped exponential backoff; the cap of 300 s is not reached in 240 s). Nothing left the machine.

Simulated 429: covered offline by `test_leak_e2e` and `test_poller` (Retry-After 120 delays the next send by at least 120 s and logs `rate_limited`). No live throttle was provoked.

Leak scan with the real key: `shelog check` reported `config: ok | devices: 7 | db schema: v1 | leak scan: 38 files, 0 hits` over `shelog.db`, `-wal`/`-shm`, `poll.log`, `drill.db`, drill logs and `data/probe/**`. Drill query: `1|1`.

Request budget: the two drills and the hour run, all through the rate guard (at least 1.1 s apart); the hour run sent about 63 requests, one per interval. The drills sent 2 requests with a dummy key to the configured host and none elsewhere.

## Continuing local collection

From the main checkout root, `scripts/live.sh poll` (or `uv run shelog poll`) keeps collecting into `data/shelog.db`; stop it with SIGTERM/Ctrl-C. A local poller must be stopped once the Phase 2 production poller polls the same account (the 1 req/s budget is shared, and the instance lock does not span machines).

## Task Commits

1. `3f29902` test(01-08): end-to-end sentinel-key leak test across every poll outcome and the probe
2. `5ffe5cc` feat(01-08): aggregate-only collection acceptance report
3. `c2ad692` docs(01-08): CLAUDE.md describes the built collector

Task 2 (live drills and the hour run) changed no tracked file: no drill misbehaved, so no regression test was needed.

## Deviations from Plan

### Auto-fixed Issues

None - plan executed as written. The leak test found no leak, so no module was changed.

### Notes

- `collection_report.py` gained `--min-attempts` (default 2) and `--now`. The hour-run acceptance (attempts at least 50) was checked with `--min-attempts 50`; the plan's own verify command (which omits it) passes too.
- `data/hour-run.exit` was written next to `hour-run.start` to capture the exit status of the background run; both are gitignored.
- A first attempt at the "wait for the poller to exit" helper matched its own command line; harmless, no extra requests were made.

## TDD Gate Compliance

The leak test passed immediately because the earlier plans already close every leak path; there was no RED commit. Its sensitivity was verified by temporarily breaking redaction and logging exception text (test failed), then reverting. The report script was written alongside its tests; the tests failed once on a test-side SQL mistake and were fixed in the same task.

## Known Stubs

None.

## Threat Flags

None. No new network endpoint, auth path or schema change; the report opens the DB read-only.

## Self-Check: PASSED

- tests/test_leak_e2e.py, scripts/collection_report.py, tests/test_collection_report.py, CLAUDE.md: present
- commits 3f29902, 5ffe5cc, c2ad692: present on main
- full suite 374 passed with the real-id scan required; ruff check and format clean
