---
gsd_state_version: '1.0'
status: planning
progress:
  total_phases: 5
  completed_phases: 0
  total_plans: 0
  completed_plans: 0
  percent: 0
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-10-31)

**Core value:** Collect a complete, trustworthy, fine-grained (~5 min) climate history from every sensor, starting as soon as possible.
**Current focus:** Phase 1 - Probe & Core Collector

## Current Position

Phase: 1 of 5 (Probe & Core Collector)
Plan: 0 of TBD in current phase
Status: Ready to plan
Last activity: 2026-10-31 — Roadmap created (5 phases, 36/36 v1 requirements mapped)

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

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- [Roadmap]: Collector goes to production (Phase 2) before outdoor weather, the read API and the UI, because missed data can't be recovered
- [Roadmap]: `compose.yaml` lists both `poller` and `web` at first onboarding, because changing it later means running `bombur-onboard` again
- [Roadmap]: `/api/health` (DATA-05) is in Phase 2, so the `web` service is a real health endpoint that shows early it can read the database while the poller writes
- [Roadmap]: Each device gets its own silence threshold from the probe's reporting settings (PROBE-05), instead of a flat 6 h

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

Last session: 2026-10-31
Stopped at: Roadmap created; ready to plan Phase 1
Resume file: None
