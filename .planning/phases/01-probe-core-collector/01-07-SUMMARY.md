---
phase: 01-probe-core-collector
plan: 07
subsystem: probe-evidence
tags: [shelly-cloud, fixtures, sanitizer, classifier, docs, live-probe]
requires:
  - phase: 01-06
    provides: "probe step matrix and --save-fixtures export"
  - phase: 01-04
    provides: "synthetic rate-limit fixture, AUTH_CONFIRMATIONS"
provides:
  - "Sanitized live fixtures: v1_all_status_info, v2_get_status, v2_get_settings, errors.json"
  - "tests/test_classify_fixtures.py: classify() pinned to every captured envelope and the synthetic rate-limit cases"
  - "docs/shelly-api.md: endpoint decision, timestamp semantics, error behaviour, reporting settings, tuning plan, operations"
  - "COVERAGE.md re-validated with probe evidence; max_silence_s set for all 7 devices in the owner's local devices.toml"
  - "sanitizer redaction of DeviceInfo.key, home coordinates, _gip_s and regional cloud host, with matching scan rules"
affects: [01-08]
tech-stack:
  added: []
  patterns:
    - "Sensitive fields the id mapping cannot pseudonymize are redacted in sanitize_docs and flagged by scan() when left"
    - "Offline replay of a stored raw capture through probe.main (fake send) to re-export fixtures without a second live run"
key-files:
  created:
    - tests/test_classify_fixtures.py
    - tests/fixtures/shelly/v1_all_status_info.json
    - tests/fixtures/shelly/v2_get_status.json
    - tests/fixtures/shelly/v2_get_settings.json
    - tests/fixtures/shelly/errors.json
    - docs/shelly-api.md
  modified:
    - src/shelog/probe.py
    - src/shelog/sanitize.py
    - tests/test_probe.py
    - tests/test_sanitize.py
    - .planning/phases/01-probe-core-collector/COVERAGE.md
    - .planning/phases/01-probe-core-collector/01-RESEARCH.md
key-decisions:
  - "AUTH_CONFIRMATIONS stays 2: no live throttle evidence, so a throttle arriving as 401 cannot be excluded; additionally a wrong regional host answers exactly like a wrong key (HTTP 401, same body)"
  - "v2 verdict: works, carries _updated for every model incl. Gen1 sleepers, key accepted in URL query and JSON body (not form, not Bearer header); unknown ids silently omitted. No v2 code path kept in the poller; v2 successes classify as bad_response by design"
  - "classify() needed no table change: every captured envelope already mapped to the evidence (401 -> auth for wrong key/host/header, 400 -> http_other, 200 non-all_status documents -> bad_response)"
  - "Gen1 reporting settings were returned by the cloud (source cloud_settings), so no default-sourced values; the Phase 2 tuning plan still applies"
requirements-completed: [PROBE-01, PROBE-02, PROBE-03, PROBE-04, PROBE-05]
duration: ~70 min
completed: 2026-11-01
status: complete
commits: 5
plan_head_before: 21f847e81f661488ea08f817940637c5c85f438c
plan_head_after: e1fa9d2332c3c2eccb41e3613ac5b4f5bd51f875
actuals:
  tokens: 30000
  tasks: 2
  commits: 5
---

# Phase 1 Plan 07: Live probe evidence, pinned classifier and findings doc Summary

**One rate-guarded live probe run was captured as sanitized fixtures, `classify()` is pinned to every real envelope plus the synthetic rate-limit cases, `docs/shelly-api.md` records the PROBE-03/04/05 findings, and all 7 devices have a `max_silence_s` in the owner's local devices.toml.**

## Accomplishments

- **Live run.** `probe --errors --wrong-host --save-fixtures` sent 12 requests, each at least 1.1 s apart; the real key went only to the configured host, a dummy key to the wrong-key and sibling-host steps. No rate-limit provocation. All 7 configured devices were present in v1, v1 info and v2; exit 0.
- **Findings.** `_updated` is the report time in UTC for every model (Gen1 `unixtime` and Gen3 `sys.unixtime` agree within 1 s; LR-3 on fw 1.11 has no device clock at all). All devices were `_sleeping` with `online` true. Wrong key v1/v2, wrong host and the Bearer header are all HTTP 401 `UNAUTHORIZED` with an identical body; the form-encoded v2 body is HTTP 400; the v1 `/device/settings` route exists (HTTP 200), contrary to the expected 404; unknown ids in a v2 batch are silently dropped.
- **Reporting settings by name.** Cloud-returned (source `cloud_settings`): LR-1, LR-2, LR-3, LR-Flood, Work, Bedroom, all with a 12 h `sleep_mode`, thresholds 0.5 and offsets 0 (the Flood has a temperature threshold only). LR-4 (Gen3): 7200 s from status. No device needed the `default` source.
- **Classifier pinned.** `tests/test_classify_fixtures.py` checks each errors.json entry against its recorded category, against categories written down in the test from the evidence, the v1 info capture (ok, 7 devices), the v2 lists (`bad_response`), the wrong-host-equals-wrong-key identity, and the three synthetic rate-limit cases. No change to `classify()` was required.

## Per-device silence thresholds (owner's local devices.toml, by name)

Before: no `max_silence_s` on any device. After (1.25 x wake period, whole minutes):

| device | max_silence_s | source |
|---|---|---|
| LR-1 | 54000 | cloud_settings (12 h) |
| LR-2 | 54000 | cloud_settings (12 h) |
| LR-3 | 54000 | cloud_settings (12 h) |
| LR-4 | 9000 | status (7200 s) |
| LR-Flood | 54000 | cloud_settings (12 h) |
| Work | 54000 | cloud_settings (12 h) |
| Bedroom | 54000 | cloud_settings (12 h) |

Only those seven lines were added to the gitignored file; nothing else in it and nothing in the env file was touched. `shelog check` re-validates the file (7 devices, 0 leak hits).

## Task Commits

1. `8fee7e8` fix(01-07): sibling_host accepts three-digit regional host numbers
2. `a2b1356` fix(01-07): sanitizer redacts Gen3 DeviceInfo.key instead of allow-listing it
3. `c7c1482` fix(01-07): sanitizer also redacts home coordinates, geo label and regional cloud host
4. `f96aeb8` test(01-07): sanitized live probe fixtures and classify() pinned to them
5. `e1fa9d2` docs(01-07): Shelly API findings, reporting settings and re-validated API coverage

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] `sibling_host` only matched two-digit regional host numbers**
- **Found during:** Task 1, first live attempt (`--wrong-host` exited 2 with "nothing was sent")
- **Issue:** The configured host has a three-digit number (`shelly-NNN-<region>`); the regex required two digits.
- **Fix:** Accept 2-3 digits, keep the neighbour's width; tests extended.
- **Files:** src/shelog/probe.py, tests/test_probe.py. **Commit:** 8fee7e8

**2. [Rule 2 - Security] Sanitizer let a Gen3 per-device token pass the structural scan only as an export refusal; allow-listing it was the plan's remedy**
- **Found during:** Task 1, first full live run (export refused: `long_token` at `DeviceInfo.key` in the v2 settings)
- **Issue:** The plan's remedy is a `KNOWN_TOKENS` entry for benign firmware strings, but `DeviceInfo.key` is a per-device dotted token, not a benign string; allow-listing it would commit it.
- **Fix:** `sanitize_docs` replaces `DeviceInfo.key` with a placeholder. **Commit:** a2b1356

**3. [Rule 2 - Security] Real home coordinates, geo-IP city and the account's regional cloud host passed both sanitizer and scanner**
- **Found during:** Task 1, reviewing the exported fixtures before committing (they were untracked and were deleted before any commit)
- **Issue:** Gen1 settings carry `lat`/`lng`, Gen3 `sys.location` carries `lat`/`lon`, both carry `_gip_s`, and the Gen3 cloud `server` is a regional host name. None was pseudonymized or flagged; CLAUDE.md forbids committing coordinates.
- **Fix:** `sanitize_docs` blanks coordinates (to 0), `_gip_s` and regional hosts; `scan()` gained `coordinate_not_redacted`, `region_label_not_redacted` and `cloud_host_not_redacted` rules; tests added. **Commit:** c7c1482

**4. [Process] Second live run and an offline re-export**
- The first command (before fix 1) sent nothing. The second run was the first with requests; its export was refused (fix 2). A third command (fixes 2 and 3 applied) was the permitted second live matrix, so the account saw two full 12-request matrices (about 24 requests, none closer than 1.1 s). Its export then showed the coordinates problem (fix 3); instead of a third live run the fixtures were regenerated by replaying that run's stored raw capture through `probe.main` with a fake transport (no network, same export code path, real-key check included).

**5. [Note] `expected_category` of non-error 200 responses**
- v2 key-in-body, unknown-id and v1 settings steps are successful 200 responses; errors.json records `bad_response` for them because `classify()` recognises only the v1 `all_status` envelope. Documented in docs/shelly-api.md; no new category was added.

**Total deviations:** 3 auto-fixed (1 Rule 1, 2 Rule 2) plus 2 notes. **Impact:** sanitizer/scanner got stricter; no interface changes.

## Known Stubs

None.

## Threat Flags

None. The real rate-limit envelope remains unverified (T-01-40 accepted: synthetic coverage, passive capture in `poll_log`, two auth confirmations).

## Self-Check: PASSED

- Files present: docs/shelly-api.md, the four new fixtures, tests/test_classify_fixtures.py, this SUMMARY; commits 8fee7e8, a2b1356, c7c1482, f96aeb8, e1fa9d2 are ancestors of HEAD; `git rev-list --count` from the recorded base gives 5.
- Full suite with `SHELOG_REQUIRE_REAL_ID_SCAN=1` and the real devices file passes with no skips; `ruff check` and `ruff format --check` clean; `shelog check` 0 hits; `AUTH_CONFIRMATIONS == 2`; the plan-01-03 v1 fixture is unmodified; `grep -c '(probe)'` on COVERAGE.md is 0; uv.lock unchanged.
