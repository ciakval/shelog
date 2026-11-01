---
phase: 01-probe-core-collector
plan: 01
subsystem: collector
tags: [python, uv, urllib, shelly-cloud, probe, config, rate-limit]
requires: []
provides:
  - "shelog probe: one live, rate-guarded POST /device/all_status with raw dump to gitignored data/probe/ and a name-only summary"
  - "config.py (Secret, Settings, Device, load_settings, load_devices), clock.py, shelly/transport.py, shelly/client.py (RateGuard, classify, ShellyClient), probe.py STEPS table"
  - "tests/conftest.py shared fakes (FakeClock, FakeSend, sentinel key, pseudo-id devices)"
  - "scripts/live.sh live-run wrapper"
affects: [01-02, 01-03, 01-04, 01-05, 01-06]
tech-stack:
  added: []
  patterns:
    - "urllib transport seam returning RawResponse or TransportError(category); classify() maps to a closed outcome enum"
    - "Every Shelly request goes through RateGuard; ShellyClient.request has no guard-skipping parameter"
    - "Secret wrapper renders as *** in str/repr/format"
key-files:
  created:
    - pyproject.toml
    - .python-version
    - uv.lock
    - scripts/live.sh
    - src/shelog/__init__.py
    - src/shelog/cli.py
    - src/shelog/config.py
    - src/shelog/clock.py
    - src/shelog/shelly/__init__.py
    - src/shelog/shelly/transport.py
    - src/shelog/shelly/client.py
    - src/shelog/probe.py
    - tests/conftest.py
    - tests/test_probe_baseline.py
  modified: []
key-decisions:
  - "Zero runtime dependencies: stdlib urllib instead of httpx2 (D-14)"
  - "Live commands run through scripts/live.sh (main checkout root as CWD, 1.1 s start delay) instead of literal uv run shelog"
  - "ruff excludes .planning because ruff 0.17 reformats markdown code blocks"
requirements-completed: [PROBE-01, COLL-01, COLL-08]
duration: ~25 min
completed: 2026-11-01
status: complete
commits: 2
plan_head_before: 3fdcb475f31ef469ee0a9a8c9ef4bbaa8b6d401b
plan_head_after: 2b31965e783090bfd9e247c0fc653069b1678da1
actuals:
  tokens: 30000
  tasks: 2
  commits: 2
---

# Phase 1 Plan 01: Probe tracer Summary

**Zero-dependency `shelog probe` that loads its own config, sends one rate-guarded all_status request with the key in the POST body, saves the raw JSON under gitignored data/probe/ and prints a name-only device summary.**

## Accomplishments

- Tracer verified end-to-end against the real account: `scripts/live.sh probe` exited 0 after exactly one request. Raw capture landed in the main checkout's gitignored `data/probe/<UTC stamp>/` (dir mode 0700) with `isok` true and 7 devices; `index.json` has exactly the 6 agreed keys.
- Probe summary (names only, present yes/no): LR-1 yes, LR-2 yes, LR-3 yes, LR-4 yes, LR-Flood yes, Work yes, Bedroom yes. All anchor keys present, 0 unconfigured devices in the response. Report ages ranged from 0 to 36 minutes, except LR-Flood at about 286 minutes, consistent with the long per-model reporting cadence (D-11).
- Shared seams match the plan's interfaces block exactly, so plan 01-02 can add parse/store/poll on top.
- 10 offline tests pin the probe contract: raw body saved verbatim, id-free index and summary, key only in the form body (URL and headers clean), RateGuard spacing at least 1.1 s, Interrupted on stop, Secret/Settings never render the key.

## Task Commits

1. Task 1 (tracer): `1baa386` feat(01-01): shelog probe tracer
2. Task 2: `2b31965` test(01-01): shared fakes and probe baseline contract

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] ruff reformatted planning markdown**
- **Found during:** Task 1
- **Issue:** `ruff format .` (0.17.3) rewrote markdown code blocks in `.planning/` files.
- **Fix:** Reverted the two touched planning files with `git checkout -- <file>` and added `extend-exclude = [".planning"]` to `[tool.ruff]`.
- **Files modified:** pyproject.toml

### Recorded plan-level deviations

- **D-03 live-run form:** live commands go through `scripts/live.sh` as the plan specifies; here it ran from the main checkout, so it was equivalent to `uv run shelog probe`.
- **`uv python install 3.13` was not needed:** 3.13.15 was already installed.
- **TDD ordering:** Task 2 is `tdd="true"` but the plan puts the implementation in Task 1, so the tests were written afterwards and passed on the first run (no RED commit). The plan type is `execute`, so no TDD gate applies.
- **Protected-branch guard:** the first commit attempt halted because HEAD was on `main`. The orchestrator set `git.allow_default_branch_commits=true` and commits resumed with no work lost.
- **Latency:** `latency_ms` includes any rate-guard wait (zero for the single probe request).

**Total deviations:** 1 auto-fixed (Rule 3), plus the recorded items above. **Impact:** none on scope.

## Authentication Gates

None.

## Known Stubs

None.

## Threat Flags

None. No new network surface beyond the planned HTTPS call to the configured Shelly host.

## Issues Encountered

None outstanding.

## Next Phase Readiness

Ready for plan 01-02 (parse, store, SQLite, `poll --once`). The live probe capture in the main checkout's `data/probe/` is available for deriving sanitized fixtures.

## Self-Check: PASSED

- Files exist: all 14 key files present.
- Commits `1baa386` and `2b31965` are ancestors of HEAD.
- `uv run ruff check .`, `uv run ruff format --check .` and `uv run pytest` (10 passed) were green before the commits; the live probe was not re-run, per the coordinator.
