---
phase: 01-probe-core-collector
plan: 03
subsystem: collector
tags: [python, sanitizer, fixtures, parser, dedup, privacy, pytest]
requires:
  - phase: 01-02
    provides: "parse_device/decide/write_cycle seams, schema 0001, tmp_db fixture"
provides:
  - "sanitize.py: deterministic device-centric pseudonymizer, structural scanner and dev CLI"
  - "Sanitized live fixture tests/fixtures/shelly/v1_all_status.json (7 devices, 3 models)"
  - "Three-layer leak guard: structural fixture scan, real-id scan and configured-key scan over every committable file"
  - "Complete parse rules (validity flags, sensor_error, range checks, poll-source fallback, device-clock cross-check) and decide() poll-source value-change rule"
  - "synthetic_cases.json: per-model edge-case blocks and whole-envelope edge cases"
affects: [01-04, 01-05, 01-06]
tech-stack:
  added: []
  patterns:
    - "Sequential-counter pseudonymization into reserved ranges (02:00:.. MACs, 192.0.2.0/24, 2001:db8::/32, fixture-ssid-N)"
    - "Scanner findings carry rule + JSON path or offset only; hex-shaped keys are rendered as [#index]"
    - "Key and ids stay inside helpers that return (file, form|device NAME) pairs"
key-files:
  created:
    - src/shelog/sanitize.py
    - tests/test_sanitize.py
    - tests/test_fixture_safety.py
    - tests/test_parse.py
    - tests/test_store.py
    - tests/fixtures/shelly/v1_all_status.json
    - tests/fixtures/shelly/synthetic_cases.json
  modified:
    - src/shelog/shelly/parse.py
    - src/shelog/store.py
key-decisions:
  - "Field garbage (bool, NaN/Inf, strings, non-bool flood) is counted as a field reject like out-of-range values; plain null and validity-flag false are routine NULLs and not counted"
  - "Present but unparseable _updated is ParseReject 'bad_ts' (replaces the tracer's 'no_ts'); absent _updated is a 'poll' report"
  - "sensor_error and the is_valid flag also null the Flood sensor's temperature"
requirements-completed: [PROBE-02, COLL-03, COLL-04, COLL-06]
duration: ~35 min
completed: 2026-11-01
status: complete
commits: 2
plan_head_before: 28592c7ac052545fed5eba4136c71a2059a0734b
plan_head_after: c16af3606d764b67e241ef7b7acdcf501b75726b
actuals:
  tokens: 23000
  tasks: 2
  commits: 2
---

# Phase 1 Plan 03: Sanitized fixtures and trustworthy parse/dedup rules Summary

**A deterministic pseudonymizer turned the live all_status response into a committed 7-device, 3-model fixture, three scan layers fail the suite if a device id or the auth key reaches any committable file, and the parser/dedup now store raw NULL-preserving values keyed on the cloud `_updated` time.**

## Accomplishments

- `shelog.sanitize`: discovers ids by key/field name and value shape (including the Gen3 BLE address and bssid as the second MAC space, and IPv6), numbers them with sequential counters, and replaces in one longest-first case-insensitive pass. Output passed its own post-scan; the CLI refuses to write if the scan finds anything (proven by a test where a long token survives).
- Fixture generated once from the existing gitignored probe capture, so no extra Shelly request was needed. It holds one Flood, one H&T Gen3 and five H&T Gen1 (one of them on older firmware without getinfo/sensor_error and with `unixtime` 0): 7 devices, all three models.
- `tests/test_fixture_safety.py`: structural layer over every fixture (with a planted-MAC self-test), real-id layer and key layer over `git ls-files --cached --others --exclude-standard` (so `.planning/` and this SUMMARY are scanned). Both owner layers ran with `SHELOG_REQUIRE_REAL_ID_SCAN=1` and zero skips against the real devices file and real key. Self-tests on a tmp git repo prove the listing, the digest-run exemption (64-hex run ignored, MAC-shaped run reported), the once-per-form key hits and that failure text never contains the key.
- Parser: Gen1 validity flags and `sensor_error`, Gen3 nulls, Flood bool-only flag, T in [-50, 100] and RH in [-5, 105] ranges (NULL, warning, counted, never clamped; RH 100.4 is stored as 100.4), device-clock cross-check warning above 120 s (never used as ts), absent `_updated` gives a `poll` report.
- `decide()`: poll-source branch (insert with `now` when last is None or any of t_c/rh/battery/flood differs, else UNCHANGED); cloud branch and plausibility constants unchanged, still no age-based rejection.
- Whole-envelope cases through the real `Poller.step`: device absent (asleep) is skipped quietly, empty block is rejected and counted, empty list is a `no_devices` failed poll.
- Suite: 137 passed with the owner switch on and zero skips; `ruff check` and `ruff format --check` clean; `uv.lock` untouched.

## KNOWN_TOKENS added

One pattern, observed in the live fixture (`getinfo.fw_info.device`, a Gen3-style full-MAC device string):

- `shelly[a-z0-9]*-(?:02[0-9a-f]{2}0000[0-9a-f]{4}|0000[0-9a-f]{2})` (case-insensitive): the model name followed by a pseudo MAC or pseudo tail. Firmware version strings (`YYYYMMDD-HHMMSS/vX.Y.Z-gHASH`) split below 20 characters at `/` and `.` and need no entry.

## Task Commits

1. Task 1: `1385e2f` feat(01-03): fixture sanitizer, structural scanner and sanitized live fixture
2. Task 2: `c16af36` feat(01-03): complete parse and dedup rules against live and synthetic fixtures

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Scanner path would have leaked device ids**
- **Found during:** Task 1
- **Issue:** JSON paths in findings contain dictionary keys, and `devices_status` keys are the device ids.
- **Fix:** hex-shaped or otherwise non-identifier keys are rendered as `[#index]`; a test asserts no id appears in any finding.
- **Files modified:** src/shelog/sanitize.py, tests/test_sanitize.py

**2. [Rule 2 - Missing critical] Count field garbage, not only out-of-range values**
- **Found during:** Task 2
- **Issue:** the behavior list counts only out-of-range values, but threat T-01-11 requires bool/NaN/non-numeric values to be NULL plus a counted reject, and the plan success criteria say every rejection is counted.
- **Fix:** present-but-unusable values add a warning and `field_rejects`; plain null and `is_valid: false` stay uncounted.
- **Files modified:** src/shelog/shelly/parse.py, tests/test_parse.py

### Recorded plan-level notes

- **TDD ordering:** tests were written before the implementation for both tasks and the red run was observed locally (23 failures in Task 2), but each task is a single commit rather than separate RED/GREEN commits. The plan type is `execute`, so no TDD gate applies.
- Reason code `no_ts` from the tracer parser became `bad_ts` (present but garbage) as the plan specifies; absent `_updated` no longer rejects.
- The poller was not modified (owned by 01-04); the new warnings, counts and poll-source timestamps flow through the tracer step unchanged.

**Total deviations:** 2 auto-fixed (1 Rule 1, 1 Rule 2). **Impact:** none on scope.

## Authentication Gates

None. No live Shelly request was made in this plan.

## Identifier hygiene

No real device id, MAC, IP, SSID or key was printed or written outside the gitignored capture. The raw capture was only read by the sanitizer CLI. For debugging the one scanner finding, only a shape-masked view (letters replaced by `a`) and the already-sanitized in-memory text were printed.

## Known Stubs

None.

## Threat Flags

None. No new network surface; T-01-07/08/09/39 are mitigated by the three scan layers and the finding format, T-01-10/11 by `decide()` and the guarded parser.

## Issues Encountered

None outstanding.

## Next Phase Readiness

Plan 01-06 can reuse `build_mapping`, `sanitize_docs` and `scan` for `probe --save-fixtures`. Plan 01-04 can rely on `Report.warnings`, `field_rejects` and `Decision.ts` as before. Plan 01-04 adds `synthetic_rate_limit.json`, which holds no identifiers, so the structural scan passes in either merge order.

## Self-Check: PASSED

- All 7 created and 2 modified files exist; commits `1385e2f` and `c16af36` are ancestors of HEAD.
- `SHELOG_REQUIRE_REAL_ID_SCAN=1` full suite (137 passed, no skips), `ruff check`, `ruff format --check` green; task acceptance criteria re-run and passing.
