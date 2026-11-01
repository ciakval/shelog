---
phase: 01-probe-core-collector
plan: 06
subsystem: probe
tags: [python, probe, shelly-cloud, rate-limit, sanitizer, fixtures, pytest]
requires:
  - phase: 01-01
    provides: "ShellyClient.request (always rate-guarded), RateGuard, transport seam, probe baseline step"
  - phase: 01-03
    provides: "sanitize_docs/build_mapping/scan, committed v1 fixture numbering"
  - phase: 01-04
    provides: "acquire_instance_lock, synthetic rate-limit fixture"
  - phase: 01-05
    provides: "strict settings, redacting logging, shelog check"
provides:
  - "probe step matrix: v1 plain and show_info, v2 status and settings; --errors group (key placements, v1 settings route, wrong key v1/v2, unknown id); --wrong-host with DUMMY_KEY to a sibling host"
  - "probe.DUMMY_KEY, probe.UNKNOWN_ID, probe.sibling_host, probe.select_steps, probe.export_fixtures"
  - "--save-fixtures DIR: one shared sanitizer mapping, errors.json, scan and key check before any write"
  - "ShellyClient.for_host: second-host client sharing the same RateGuard and transport"
  - "probe takes the poll instance lock (exit 3 when held)"
affects: [01-07, 01-08]
tech-stack:
  added: []
  patterns:
    - "Steps are a table (name, run, group, key_placement); every request funnels through ShellyClient.request so the 1.1 s guard cannot be skipped"
    - "Wrong-key and wrong-host steps build their payload from DUMMY_KEY and never read client.key"
    - "Export is all-or-nothing: collect, sanitize once, scan, then temp file plus rename"
key-files:
  created:
    - tests/test_probe.py
  modified:
    - src/shelog/probe.py
    - src/shelog/cli.py
    - src/shelog/shelly/client.py
    - tests/test_probe_baseline.py
    - tests/test_secrets.py
key-decisions:
  - "Export refuses (exit 1, nothing written) when the v1_all_status capture is missing or not ok, because the committed fixture numbering is derived from that body; without it the mapping could not match"
  - "errors.json is written only when error or wrong-host steps ran; the v1 fixture is fed to the mapping but never rewritten"
  - "The v2 steps are classified with the existing v1-shaped classify(), so successful v2 list responses show category bad_response in index.json and in the summary; classification of v2 is left to the 01-07 table pinning"
  - "Bearer scheme is used for the Authorization-header key-placement probe (docs name no scheme); the capture will show whether the cloud cares"
requirements-completed: [PROBE-01, PROBE-02, PROBE-04, PROBE-05]
duration: ~30 min
completed: 2026-11-01
status: complete
commits: 4
plan_head_before: a82c60738864819dd134cded2bb8ad98d93a7788
plan_head_after: c9f329704b98932b7d453e2df4178e4fb7fcb91d
actuals:
  tokens: 12500
  tasks: 2
  commits: 4
coverage:
  - deliverable: "Probe step matrix (default, --errors, --wrong-host) in documented order with the documented key placements"
    human_judgment: false
    verification:
      - {kind: test, ref: "tests/test_probe.py#test_default_steps_and_request_shapes", status: pass}
      - {kind: test, ref: "tests/test_probe.py#test_error_group_order_and_key_placements", status: pass}
  - deliverable: "Every probe request at least 1.1 s apart across both hosts; no unguarded or rate-limit-testing mode"
    human_judgment: false
    verification:
      - {kind: test, ref: "tests/test_probe.py#test_full_matrix_spacing_is_at_least_1_1_s", status: pass}
      - {kind: test, ref: "tests/test_probe.py#test_cli_exposes_the_flags_and_no_rate_limit_testing_option", status: pass}
  - deliverable: "Real key only to SHELLY_HOST; DUMMY_KEY for wrong-key and wrong-host steps"
    human_judgment: false
    verification:
      - {kind: test, ref: "tests/test_probe.py#test_dummy_key_steps_never_carry_the_real_key", status: pass}
      - {kind: test, ref: "tests/test_probe.py#test_real_key_goes_only_to_the_configured_host", status: pass}
  - deliverable: "No URL, host, key or id in index.json, stdout, stderr (including the query-string key step)"
    human_judgment: false
    verification:
      - {kind: test, ref: "tests/test_probe.py#test_index_stdout_stderr_hold_no_url_host_key_or_id", status: pass}
      - {kind: test, ref: "tests/test_probe.py#test_sentinel_absent_even_with_a_url_encoded_key", status: pass}
  - deliverable: "--save-fixtures export with one pseudonym mapping matching the v1 numbering, single errors.json, refusal paths write nothing"
    human_judgment: false
    verification:
      - {kind: test, ref: "tests/test_probe.py#test_pseudo_ids_are_shared_across_files_and_match_the_v1_numbering", status: pass}
      - {kind: test, ref: "tests/test_probe.py#test_a_body_carrying_the_real_key_stops_the_export", status: pass}
      - {kind: test, ref: "tests/test_probe.py#test_a_scan_finding_stops_the_export_and_writes_nothing", status: pass}
  - deliverable: "probe and poll share the single-instance lock"
    human_judgment: false
    verification:
      - {kind: test, ref: "tests/test_probe.py#test_held_lock_makes_probe_return_3_without_sending", status: pass}
---

# Phase 1 Plan 06: Probe capture matrix and sanitized fixture export Summary

**`shelog probe [--errors] [--wrong-host] [--save-fixtures DIR]` now runs the full capture matrix (v1, v1 with show_info, v2 status and settings, error group, dummy-key wrong host) strictly through the shared 1.1 s rate guard, keeps the real key on SHELLY_HOST, and exports sanitized fixtures through one pseudonym mapping or not at all. Proven entirely offline; no live request was made.**

## Accomplishments

- Step table of 12 steps in 3 groups. Default: `v1_all_status`, `v1_all_status_info` (query `show_info=true&no_shared=true`, key in the form body), `v2_get_status` (key in the query as documented, JSON `ids` + `select`), `v2_get_settings` (`select` status + settings). `--errors` adds `v2_key_in_body`, `v2_key_in_form`, `v2_key_in_header`, `v1_device_settings`, `wrong_key_v1`, `wrong_key_v2`, `unknown_id_v2`. `--wrong-host` adds `wrong_host`.
- Rate safety: all requests go through `ShellyClient.request`; the sibling-host client is made by `for_host` and shares the same `RateGuard`, so spacing holds across hosts. A FakeClock test over the full 12-request matrix asserts every gap is at least 1.1 s. No `--provoke`, `--force` or burst option exists (asserted on the parser).
- Key confinement: `wrong_key_v1`, `wrong_key_v2` and `wrong_host` build their payload from `DUMMY_KEY` and never read `client.key`. Tests assert the sentinel is absent from those calls and from every call to a host other than the configured one, and present only on the configured host. `sibling_host` refuses (exit 2, message without the host, before any request) when no regional sibling can be derived.
- Output hygiene: `index.json` entries carry `step, http_status, category, retry_after_s, latency_ms, bytes, key_placement` only. Tests assert no sentinel (raw, quote or quote_plus form), host, sibling host, `https://`, id, MAC, IP or SSID in index.json, stdout or stderr, including the query-string key step. Raw bodies still go verbatim to `data/probe/<stamp>/`, and both `data/probe` and the stamped directory are created 0o700.
- Name-only summary: per device, v1 presence (as before), v2 presence and whether `_updated` is in the v2 status, `_dev_info` gen/code/online, a reporting line (Gen3 `sys.wakeup_period`; `sleep_mode.period/unit`, thresholds, offsets, `external_power` from v2 settings), plus one `category, HTTP` line per error step.
- Export: the v1 body, the three success captures and one `errors` document go through a single `sanitize_docs` call with the configured ids as `extra_ids`, so numbering equals `build_mapping([v1 body], extra_ids)`. `UNKNOWN_ID` sorts last and does not shift it. Output is `v1_all_status_info.json`, `v2_get_status.json`, `v2_get_settings.json`, `errors.json` (the v1 fixture is never rewritten; an existing one is byte-identical afterwards). Non-JSON bodies become `{"_non_json_body": text[:2000]}`; a body echoing `DUMMY_KEY` is exported with `dummy-key`. Refusals (scan finding, sanitizer refusal, missing v1 capture, any body containing the real key in raw/quote/quote_plus form) return 1, create nothing, and print step names, rule names and JSON paths only.
- Lock: `probe.main` takes the same `poller.lock` as `poll`; a held lock returns 3 before any request.
- Suite: 329 passed (298 before plus 31 new) with `SHELOG_REQUIRE_REAL_ID_SCAN=1` and the real devices file, zero skips; `ruff check` and `ruff format --check` clean; `uv.lock` untouched.

## Task Commits

1. Task 1 RED: `5c7f004` test(01-06): probe step matrix, dummy-key confinement, spacing and lock (RED)
2. Task 1 GREEN: `dbbcfef` feat(01-06): probe step matrix with dummy-key wrong host, spacing and instance lock
3. Task 2 RED: `a59ba54` test(01-06): --save-fixtures export contract (RED)
4. Task 2 GREEN: `c9f3297` feat(01-06): probe --save-fixtures with one shared sanitizer mapping

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `ShellyClient.for_host` added to client.py**
- **Found during:** Task 1 design
- **Issue:** The wrong-host step needs a second client for another host that shares the same RateGuard, transport and clock. The guard is private to `ShellyClient`, and client.py was not in the plan's file list.
- **Fix:** Added a 12-line `for_host(host, key)` method that builds a sibling client from the same `send`, `guard`, `clock` and `timeout`. No change to `request` or the guard.
- **Files modified:** src/shelog/shelly/client.py
- **Commit:** dbbcfef

**2. [Rule 3 - Blocking] tests/test_probe_baseline.py adapted to the new contract**
- **Found during:** Task 1 GREEN
- **Issue:** The plan says to keep the baseline file green, but it scripts one response and pins the index keys without `key_placement`. The default run now has four steps, so the extra requests hit "script exhausted", and the index contract gained a key.
- **Fix:** `run_probe` accepts an optional `steps=` argument (default: select by flags). The baseline file passes `steps=STEPS[:1]` so it keeps pinning the first step only, and its `INDEX_KEYS` gained `key_placement`. Assertions are otherwise untouched.
- **Files modified:** tests/test_probe_baseline.py
- **Commit:** dbbcfef

**3. [Rule 3 - Blocking] `test_cli_check_runs_from_a_valid_config` failed under the owner-scan run**
- **Found during:** Task 2 final verify (`SHELOG_DEVICES_FILE` is set by the plan's verify command)
- **Issue:** That plan 01-05 test builds its own tmp devices file but `SHELOG_DEVICES_FILE` from the environment overrode it (7 real devices instead of 1), so the plan's mandated verify command could not pass.
- **Fix:** The test now clears `SHELOG_DEVICES_FILE` with `monkeypatch.delenv`. One line.
- **Files modified:** tests/test_secrets.py
- **Commit:** c9f3297

**4. [Process] Plan commit ledger (protocol 0c) was not recorded before the first commit**
- `plan_head_before` is the commit that was HEAD when this plan started (the 01-05 docs commit, per the opening git log), so the measured count of 4 equals the 4 task commits.

**Total deviations:** 3 auto-fixed (all Rule 3) plus 1 process note. **Impact:** none on frozen interfaces; `run_probe` and `main` gained keyword arguments only, defaults preserve earlier behaviour.

## Notes for plan 01-07 (the live run)

- Expected run: `scripts/live.sh probe --errors --wrong-host --save-fixtures <DIR>`: about 12 requests, roughly 14 s. `--wrong-host` needs the configured host to look like `shelly-NN-<region>.shelly.cloud`; otherwise it exits 2 with nothing sent.
- v2 list responses are run through the v1-shaped `classify()`, so a successful v2 step reports `bad_response` in index.json and in the error-step expectations. When 01-07 pins the classifier table to the real envelopes, decide whether v2 needs its own classification; `errors.json` `expected_category` records whatever `classify` returns today.
- `latency_ms` includes the time spent waiting on the rate guard (the wait happens inside `request`), so for steps after the first it is at least about 1100 ms by construction.
- A repeat of the `v1_all_status` fixture is deliberately not exported; `v1_all_status_info.json` is a new fixture and its numbering matches the committed v1 file because both come from the same configured device set.
- The rate-limit envelope remains synthetic by decision.

## Known Stubs

None.

## Threat Flags

None. The only new outbound surface is the opt-in `--wrong-host` request, which carries `DUMMY_KEY` only and is covered by T-01-29 in the plan.

## Self-Check: PASSED

- Created/modified files present: tests/test_probe.py, src/shelog/probe.py, src/shelog/cli.py, src/shelog/shelly/client.py, tests/test_probe_baseline.py, tests/test_secrets.py.
- Commits 5c7f004, dbbcfef, a59ba54, c9f3297 are ancestors of HEAD; `git rev-list --count` from the recorded base gives 4.
- Acceptance criteria re-run: `shelog probe --help` lists `--errors`, `--wrong-host`, `--save-fixtures`; `grep -c -- '--provoke\|--force'` on the help prints 0; the `sibling_host` and constants one-liners exit 0; `grep -c acquire_instance_lock src/shelog/probe.py` = 2; `grep -c 'sanitize_docs(' src/shelog/probe.py` = 1.
- Plan verification: full suite with `SHELOG_REQUIRE_REAL_ID_SCAN=1` and the real devices file passes (329 passed, no skips); `ruff check .` and `ruff format --check .` clean.
