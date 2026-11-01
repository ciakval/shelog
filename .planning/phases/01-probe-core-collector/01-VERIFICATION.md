---
phase: 01-probe-core-collector
verified: 2026-11-01T15:20:00Z
status: passed
score: 4/4 roadmap success criteria verified (plus 13/13 requirements satisfied)
covered_files:
  - .env.example
  - .planning/phases/01-probe-core-collector/01-01-PLAN.md
  - .planning/phases/01-probe-core-collector/01-01-SUMMARY.md
  - .planning/phases/01-probe-core-collector/01-02-PLAN.md
  - .planning/phases/01-probe-core-collector/01-02-SUMMARY.md
  - .planning/phases/01-probe-core-collector/01-03-PLAN.md
  - .planning/phases/01-probe-core-collector/01-03-SUMMARY.md
  - .planning/phases/01-probe-core-collector/01-04-PLAN.md
  - .planning/phases/01-probe-core-collector/01-04-SUMMARY.md
  - .planning/phases/01-probe-core-collector/01-05-PLAN.md
  - .planning/phases/01-probe-core-collector/01-05-SUMMARY.md
  - .planning/phases/01-probe-core-collector/01-06-PLAN.md
  - .planning/phases/01-probe-core-collector/01-06-SUMMARY.md
  - .planning/phases/01-probe-core-collector/01-07-PLAN.md
  - .planning/phases/01-probe-core-collector/01-07-SUMMARY.md
  - .planning/phases/01-probe-core-collector/01-08-PLAN.md
  - .planning/phases/01-probe-core-collector/01-08-SUMMARY.md
  - CLAUDE.md
  - devices.example.toml
  - docs/shelly-api.md
  - scripts/collection_report.py
  - scripts/live.sh
  - src/shelog/check.py
  - src/shelog/cli.py
  - src/shelog/config.py
  - src/shelog/db.py
  - src/shelog/lock.py
  - src/shelog/logging_setup.py
  - src/shelog/migrations/0001_init.sql
  - src/shelog/poller.py
  - src/shelog/probe.py
  - src/shelog/sanitize.py
  - src/shelog/shelly/client.py
  - src/shelog/shelly/parse.py
  - src/shelog/shelly/transport.py
  - src/shelog/store.py
covered_digest: "v4:sha256:3aa967d4aa04de5b267f9594a6ff8c25e31026853b083781d39be0387dded413"
behavior_unverified: 0
overrides_applied: 0
---

# Phase 1: Probe & Core Collector Verification Report

**Phase Goal:** The owner can run the collector locally against the real Shelly account and see deduplicated, raw, correctly timestamped readings from all seven sensors land in SQLite, with every poll attempt explained and the auth key never exposed
**Verified:** 2026-11-01T15:20:00Z
**Status:** passed
**Re-verification:** No, initial verification (after code review fixes WR-01..WR-06, IN-01, IN-03 were applied; the code verified here includes them)

## Goal Achievement

### Observable Truths (ROADMAP success criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `shelog probe` returns raw status for all seven devices; sanitized fixtures for H&T Gen1, H&T Gen3, Flood Gen1 (incl. error, asleep, rate-limit) are committed; a test fails on any real id/MAC/IP/SSID/key | VERIFIED | `tests/fixtures/shelly/` holds `v1_all_status.json`, `v1_all_status_info.json` (asleep, `_sleeping`), `v2_get_status.json`, `v2_get_settings.json`, `errors.json` (wrong key, wrong host, unknown id, missing route), `synthetic_rate_limit.json`, `synthetic_cases.json`, `synthetic_envelope_all_models.json`. `tests/test_fixture_safety.py` passes with `SHELOG_REQUIRE_REAL_ID_SCAN=1` (real-id scan not skipped). The probe step matrix and `--save-fixtures` are implemented in `src/shelog/probe.py` (tested in `tests/test_probe*.py`). The 7-device live capture is documented in `docs/shelly-api.md` and `COVERAGE.md`. |
| 2 | A document records, per model, the report-time field (`ts_source`, timezone) and each device's reporting settings; each device in `devices.toml` has a silence threshold; only `devices.example.toml` is in the repo | VERIFIED | `docs/shelly-api.md` "Timestamp semantics": `_updated`, UTC, `ts_source='cloud'` for H&T Gen1 (fw 1.14 and 1.11), Flood Gen1 and H&T Gen3, each with clock cross-check evidence. "Reporting settings" table records wake period, thresholds, offsets and provenance for all 7 devices and a tuning plan. Owner `devices.toml` (gitignored, `git check-ignore` confirmed): 7 devices, 7 `max_silence_s` values (six at 54000 s, one at 9000 s, matching the 12 h and 2 h periods x 1.25). `git ls-files` shows only `devices.example.toml` and `.env.example`; `devices.toml` and `.env` are not tracked. |
| 3 | Local run against the real account for at least an hour: one batched request per interval, never more than 1 req/s; a row only when the report time advanced; raw uncalibrated T/RH/battery/flood, nulls kept NULL, raw JSON alongside; implausible timestamps rejected and counted | VERIFIED | Live DB (`data/shelog.db`), window since the hour-run start (`collection_report.py --expect-devices 7`): 435 attempts, mean gap 60.1 s, 0 same-second pairs, 7 distinct devices, 0 implausible ts, 0 duplicate (device, ts), readings vs raw_status 390/390, `ts_source` all `cloud` (390), 0 failed-without-category, verdict "all checks pass". Whole DB: min gap between consecutive attempts 46 s (>> 1.1 s guard); 0 readings with `t_c = 0` or `rh = 0`; 3 NULL RH rows correspond to the Flood sensor (no RH). Dedup: 390 inserted vs 2550 unchanged over the run, 0 rejected live. Implausible/regressed/NaN/bool/out-of-range handling is code-verified (`store.decide`, `parse._num`, `_Tally`) and pinned by tests (`tests/test_store.py` plausibility boundaries, regressed, poll-source; `tests/test_parse.py`). Live `n_rejected` was 0, so the reject-and-count path is test-evidenced, not live-evidenced (no real implausible report occurred). |
| 4 | Wrong key, simulated 429 + Retry-After, and network outage each cause capped backoff and a poll_log row with a category; wrong key is a separate "auth rejected" state; no trace of the key in logs, error text or DB | VERIFIED | Live dummy-key drill DB (`data/drill.db`): 2 `auth` rows (HTTP 401) 300 s apart, `meta.shelly_auth = rejected` with `shelly_auth_since`; 4 `network` rows with growing gaps (16, 56, 108 s) and a ceiling of 300 s (`CAP_S`). Drill logs show "poll failed: auth", "rejected the auth key; retrying every 300 s". Production DB: 16 `timeout` rows (15 s, category-only) among 436 attempts, ok ratio 0.963. 429/Retry-After: `next_delay` (honours Retry-After as a floor), `classify` (status-first, `parse_retry_after` clamp 1..900), covered by `tests/test_poller.py::test_next_delay_rate_limited_honours_retry_after`, `test_six_hour_simulation_respects_gap_logs_every_attempt_and_honours_retry_after`, `tests/test_client.py` and `synthetic_rate_limit.json`. Never provoked live by design. Key leak: `scripts/live.sh check` reports "leak scan: 39 files, 0 hits, 0 unreadable" over the DB (+WAL/SHM), logs and probe output; `tests/test_leak_e2e.py` drives every poll outcome and the probe with a sentinel key and finds no trace. |

**Score:** 4/4 truths verified (0 behavior-unverified)

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/shelog/poller.py` | Cycle, loop, backoff, auth state | VERIFIED | Substantive and wired (`step`, `cycle`, `run`, `next_delay`, `_observe`); invoked from `cli`; running live. |
| `src/shelog/store.py` | Dedup decision + one-transaction writer | VERIFIED | `decide`, `write_cycle` (BEGIN IMMEDIATE, ON CONFLICT DO NOTHING, rollback on BaseException). |
| `src/shelog/shelly/parse.py` | Per-model parsers | VERIFIED | `_updated` UTC only; None never coerced to 0; bounds counted. |
| `src/shelog/shelly/client.py` | RateGuard, classify, all_status | VERIFIED | Closed `ERROR_CATEGORIES`; no guard-skipping parameter; key in POST form body only. |
| `src/shelog/shelly/transport.py` | Only socket code | VERIFIED | Wall-clock deadline, no redirects, 1 MiB cap, `HTTPException` mapped to network. |
| `src/shelog/migrations/0001_init.sql` / `db.py` | Full schema, WAL, `user_version` | VERIFIED | Live DB reports schema v1; tables `reading`, `raw_status`, `poll_log` populated; STRICT tables with CHECKs. |
| `src/shelog/config.py`, `logging_setup.py`, `check.py` | Strict config, redaction, leak scan | VERIFIED | `check` ran live: config ok, 7 devices, 0 hits. |
| `src/shelog/probe.py`, `sanitize.py` | Probe matrix, sanitizer | VERIFIED | Tested offline; live capture evidenced in docs. |
| `src/shelog/lock.py` | Single-instance lock | VERIFIED | `data/poller.lock` in use by the running poller; `test_main_returns_3_when_another_poller_holds_the_lock`. |
| `scripts/live.sh`, `scripts/collection_report.py` | Live wrapper, aggregate report | VERIFIED | Report ran live (see above). |
| `docs/shelly-api.md`, `COVERAGE.md` | Findings | VERIFIED | Contain names and pseudonymised values only (covered by the real-id scan). |
| `devices.example.toml`, `.env.example` | Public examples | VERIFIED | Tracked; real files gitignored. |

### Key Link Verification

| From | To | Via | Status |
|------|----|-----|--------|
| `poller.step` | `ShellyClient.fetch_all_status` | one call per cycle | WIRED |
| `ShellyClient.request` | `RateGuard.acquire` | mandatory before every send | WIRED |
| `poller.step` | `parse.parse_device` then `store.decide` then `store.write_cycle` | per-device parse, dedup, one transaction | WIRED |
| `write_cycle` | `poll_log` + `reading` + `raw_status` | same transaction | WIRED (390/390 live) |
| `poller._observe` | `meta.shelly_auth` | persisted `rejected`/`ok` | WIRED (drill DB shows `rejected`, prod `ok`) |
| `logging_setup` formatter | all log lines | key scrub | WIRED (`test_secrets.py`, e2e leak test) |
| `cli` | poll / probe / check, shared lock | subcommands | WIRED |

### Data-Flow Trace (Level 4)

| Artifact | Data | Source | Real data | Status |
|----------|------|--------|-----------|--------|
| `reading` table | T, RH, battery, flood, ts | real `POST /device/all_status` via parse | Yes: 390 live rows, 7 devices, `ts_source='cloud'` | FLOWING |
| `raw_status` | per-device JSON | same response | Yes: 390 rows, one per reading | FLOWING |
| `poll_log` | outcome, status, counts, latency | per attempt | Yes: 436 rows with categories | FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full offline suite incl. real-id scan | `SHELOG_REQUIRE_REAL_ID_SCAN=1 uv run pytest -q` | all pass (no failures or skips reported) | PASS |
| Lint and format | `uv run ruff check .` / `ruff format --check .` | clean; 37 files formatted | PASS |
| Acceptance report on live DB | `collection_report.py --db data/shelog.db --since 1793520113 --expect-devices 7` | all checks PASS | PASS |
| Key leak scan (offline) | `scripts/live.sh check` | 39 files, 0 hits, 0 unreadable | PASS |
| Poller still running | `ps`; latest `poll_log` row 1 s old | running, `poll ok` lines current | PASS |

No Shelly request was made by the verifier.

### Probe Execution

Phase probe is the product CLI (`shelog probe`), not a `scripts/*/tests/probe-*.sh` script. No such scripts exist or are declared. Not executed (instruction: no Shelly requests); its behaviour is covered by offline tests and the recorded live capture. SKIPPED, not a gap.

### Requirements Coverage

All 13 IDs declared across PLAN frontmatter (01-01..01-08) appear in REQUIREMENTS.md mapped to Phase 1. No orphaned Phase 1 requirement.

| Requirement | Source Plans | Status | Evidence |
|-------------|--------------|--------|----------|
| PROBE-01 | 01-01, 01-06, 01-07 | SATISFIED | `probe.py`, live capture of all 7 devices (`docs/shelly-api.md`) |
| PROBE-02 | 01-03, 01-06, 01-07 | SATISFIED | sanitized fixtures; `test_fixture_safety.py` passes with real-id scan required |
| PROBE-03 | 01-07 | SATISFIED | "Timestamp semantics" per model, UTC `_updated` |
| PROBE-04 | 01-06, 01-07 | SATISFIED | Error behaviour table (wrong key, wrong host, unknown id, missing route, sleeping/offline; rate limit documented as unverified/synthetic) with fixtures and categories |
| PROBE-05 | 01-06, 01-07 | SATISFIED | Reporting settings table with provenance; 7 `max_silence_s` in owner inventory |
| COLL-01 | 01-01, 01-02, 01-04, 01-08 | SATISFIED | one batched call/cycle, mean gap 60.1 s, 0 same-second pairs, min gap 46 s |
| COLL-02 | 01-04, 01-08 | SATISFIED | capped backoff, Retry-After floor, persisted `rejected` state; drills + tests |
| COLL-03 | 01-03, 01-08 | SATISFIED | `store.decide`; 2550 unchanged vs 390 inserted live; implausible rejected and counted (test-evidenced) |
| COLL-04 | 01-02, 01-03, 01-08 | SATISFIED | raw values, `ts_source` column, no zero coercion (0 zero values; NULLs only for Flood RH) |
| COLL-05 | 01-02, 01-04, 01-08 | SATISFIED | every attempt logged; 0 failed rows without a category |
| COLL-06 | 01-02, 01-03, 01-08 | SATISFIED | raw_status 390/390 |
| COLL-07 | 01-05, 01-08 | SATISFIED | redacting formatter, `check` 0 hits live, e2e sentinel test |
| COLL-08 | 01-01, 01-05 | SATISFIED | strict `devices.toml` loader; only example files tracked |

### Anti-Patterns Found

None blocking. No TBD/FIXME/XXX/TODO/HACK markers in `src`, `scripts`, `tests` or `docs`. Code review (01-REVIEW.md: 0 critical, 6 warnings) was followed by fixes for all six warnings plus IN-01 and IN-03 (01-REVIEW-FIX.md, commits fc59a29..d456432); the fixes are present in the code read here (status-first `classify`, wall-clock transport deadline, `Poller` construction guard, `where()` locations only, `body_shape` instead of excerpt, fail-closed leak scan).

Not-fixed review items IN-02, IN-04..IN-08 were out of scope by instruction and are informational.

### Human Verification Required

None required for the phase goal. The owner already operates the live poller.

### Advisory Observations (non-blocking)

1. **Live timeouts.** 16 of 436 attempts in the live window ended as category `timeout` (15 s, 3.7 percent), and successful requests had a maximum latency of about 11.5 s. All are logged with a category and the loop recovered each time, so COLL-05 holds. For Phase 2: keep `stop_grace_period` about 20-30 s (noted in the review fix report) and consider whether alert thresholds should tolerate this baseline of isolated timeouts.
2. **Reject path not seen live.** Live `n_rejected` was 0 and the 429 envelope was never provoked live (by owner decision). Both paths are exercised by offline tests and the synthetic fixtures only.
3. **Silence thresholds are initial.** `max_silence_s` values come from cloud settings (12 h / 2 h x 1.25); `docs/shelly-api.md` schedules re-tuning from observed gaps in Phase 2.
4. **Admin.** ROADMAP.md still shows Phase 1 as unchecked "In Progress" (8/8 plans done) and `.planning/state.json` is untracked; the orchestrator should update the roadmap on completion.

### Gaps Summary

No gaps. All four roadmap success criteria and all 13 requirements are supported by code, passing offline tests, and live evidence from the real account.

---

_Verified: 2026-11-01T15:20:00Z_
_Verifier: Claude (gsd-verifier)_
