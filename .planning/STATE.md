---
gsd_state_version: "1.0"
current_phase: 01
current_phase_name: Probe & Core Collector
status: executing
stopped_at: Completed 01-02-PLAN.md
last_updated: "2026-11-01T07:07:37.149Z"
last_activity: 2026-11-01
last_activity_desc: Phase 01 execution started
state_head: b188ba68c17697aab2318b8adfbc3603d75df207
progress:
  total_phases: 5
  completed_phases: 0
  total_plans: 8
  completed_plans: 2
  percent: 0
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-10-31)

**Core value:** Collect a complete, trustworthy, fine-grained (~5 min) climate history from every sensor, starting as soon as possible.
**Current focus:** Phase 01 — Probe & Core Collector

## Current Position

Phase: 01 (Probe & Core Collector) — EXECUTING
Plan: 3 of 8
Status: Ready to execute
Last activity: 2026-11-01 — Phase 01 execution started

Progress: [░░░░░░░░░░] 0%

## Performance Metrics

**Velocity:**
- Total plans completed: 0
- Average duration: -
- Total execution time: 0.0 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| - | - | - | - |

**Recent Trend:**
- Last 5 plans: -
- Trend: -

*Updated after each plan completion*
**Per-Plan Metrics:**

| Plan | Duration | Tasks | Files |
|------|----------|-------|-------|
| Phase 01 P01 | 25 min | 2 tasks | 14 files |
| Phase 01 P02 | 20min | 2 tasks | 9 files |

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- [Roadmap]: Collector goes to production (Phase 2) before outdoor weather, the read API and the UI, because missed data can't be recovered
- [Roadmap]: `compose.yaml` lists both `poller` and `web` at first onboarding, because changing it later means running `bombur-onboard` again
- [Roadmap]: `/api/health` (DATA-05) is in Phase 2, so the `web` service is a real health endpoint that shows early it can read the database while the poller writes
- [Roadmap]: Each device gets its own silence threshold from the probe's reporting settings (PROBE-05), instead of a flat 6 h
- [Phase 01]: Zero runtime dependencies: stdlib urllib transport instead of httpx2
- [Phase 01]: Live runs go through scripts/live.sh (main checkout root as CWD, 1.1 s start delay) instead of literal uv run shelog
- [Phase 01]: Plan 01-02: reload last_states from DB after a partial insert; no-configured-device responses become no_devices failures

### Pending Todos

None yet.

### Blockers/Concerns

- [Phase 1]: Credentials are in place (repo-root env file + gitignored devices.toml); the app loads the env file itself, so Claude can run `uv run shelog …` live
- [Phase 2]: Owner actions needed. No GitHub remote exists yet, `main` needs protecting, `bombur-onboard` must be run, and the server `.env` and `devices.toml` must be filled in. Don't use `CHANGE_ME` for the Shelly values.
- [Phase 2]: Decide whether to pull forward the external dead-man's switch (ROB-01), because alerts sent by the poller can't report that the poller or the VPS is dead
- [Phase 3]: Must ship within 92 days of Phase 2 going live, or the first days of the record can only be backfilled from the historical-forecast API
- [Stack]: `httpx2` is a young library. Keep it behind one client module, with plain `httpx` as the fallback.

## Deferred Items

Items acknowledged and deferred at milestone close, most recent first:

| Category | Item | Status | Deferred At | Milestone |
|----------|------|--------|-------------|-----------|
| *(none)* | | | | |

## Session Continuity

Last session: 2026-11-01T07:07:37.117Z
Stopped at: Completed 01-02-PLAN.md
Resume file: None
