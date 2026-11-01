---
phase: "01"
slug: "probe-core-collector"
status: verified
# threats_open = count of OPEN threats at or above workflow.security_block_on severity (the blocking gate)
threats_open: 0
asvs_level: 1
created: "2026-11-01"
---

# Phase 01 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| App → Shelly Cloud | HTTPS POST to the configured `SHELLY_HOST` | Auth key (secret, form body only), device status JSON (LAN identifiers) |
| Secrets file / env → process | Settings loaded at start | Auth key, host |
| Process → logs / stdout / poll_log | Everything the owner or a future public surface can read | Must carry names, categories and counts only |
| Live data → committed files | Fixtures, docs, planning artifacts in a public repo | Must contain no real ids, MACs, IPs, SSIDs, coordinates or key forms |
| Local data/ directory | SQLite DB, raw_status, probe dumps | LAN identifiers (gitignored) |

---

## Threat Register

42 threats (T-01-01..T-01-42 without T-01-28, which was never assigned, plus T-01-SC), from the eight 01-0N-PLAN.md `<threat_model>` blocks. All CLOSED; evidence (file:line and test names) is in the 2026-11-01 auditor verdict summarised below.

| Threat ID | Category | Component | Severity | Disposition | Mitigation | Status |
|-----------|----------|-----------|----------|-------------|------------|--------|
| T-01-01 | I | transport, client | high | mitigate | Key only in the form body; errors category-only, raised `from None` | closed |
| T-01-02 | I | config.normalize_host, opener | high | mitigate | https only, http:// rejected, redirects refused | closed |
| T-01-03 | D/T | transport.send, classify | medium | mitigate | 1 MiB body cap, NaN/Inf rejected, non-dict → bad_response | closed |
| T-01-04 | T | store, db | medium | mitigate | Parameterised DML, STRICT tables, CHECK constraints | closed |
| T-01-05 | R | store.write_cycle | high | mitigate | One BEGIN IMMEDIATE transaction, ON CONFLICT DO NOTHING, rollback | closed |
| T-01-06 | I | probe | medium | mitigate | Raw dumps in 0700 per-run dirs, index without identifiers | closed |
| T-01-07 | I | fixtures, sanitize | high | mitigate | Sanitizer-only fixtures; structural, real-id and key scans | closed |
| T-01-08 | I | sanitize.build_mapping | medium | mitigate | Sequential pseudonyms, no hashing | closed |
| T-01-09 | I | scan findings | medium | mitigate | Findings name rule/path, never the value | closed |
| T-01-10 | T | store.decide | medium | mitigate | Plausibility window, regression rejected and counted | closed |
| T-01-11 | T | parse numeric guard | medium | mitigate | Bad values → NULL + counted reject, never 0 | closed |
| T-01-12 | I | raw_status | low | accept | See accepted risks | closed |
| T-01-13 | D | RateGuard, backoff | high | mitigate | ≥1.1 s spacing, capped backoff, Retry-After clamp | closed |
| T-01-14 | D | startup, lock | medium | mitigate | flock single instance, 30 s startup-failure pause | closed |
| T-01-15 | I | loop boundary | high | mitigate | poll_log.error closed enum; exceptions logged as type + where() | closed |
| T-01-16 | I/E | redirects | high | mitigate | No redirect handler; real-socket test | closed |
| T-01-17 | R | poller.run | medium | mitigate | Every attempt writes a poll_log row, incl. internal | closed |
| T-01-18 | S | auth state | medium | mitigate | Two confirmations before `rejected` | closed |
| T-01-19 | D | bad_response logging | low | accept | See accepted risks (superseded by WR-05 fix) | closed |
| T-01-20 | I | RedactingFormatter | high | mitigate | Raw/quote/quote_plus key forms scrubbed from every record | closed |
| T-01-21 | I | host validation | high | mitigate | `[a-z0-9.-]+` fullmatch | closed |
| T-01-22 | I | ConfigError messages | medium | mitigate | Names variables / device names only | closed |
| T-01-23 | T | load_devices | medium | mitigate | Strict schema validation | closed |
| T-01-24 | S | placeholder guard | low | mitigate | Empty/CHANGE_ME/short keys refused | closed |
| T-01-25 | I | example files | medium | mitigate | Pseudo ids, empty secrets, real files gitignored | closed |
| T-01-26 | I | check | medium | mitigate | Counts/paths only, read-only DB open | closed |
| T-01-27 | I | probe v2 query steps | high | mitigate | Query URL confined to client.request | closed |
| T-01-29 | I | wrong-host/key steps | high | mitigate | Dummy key only | closed |
| T-01-30 | I | --save-fixtures | high | mitigate | Key check + scan abort before any write | closed |
| T-01-31 | S | classify | medium | mitigate | Pinned to captured envelopes | closed |
| T-01-32 | T | devices.toml edits | low | mitigate | `shelog check` validates strictly | closed |
| T-01-33 | I | data/ after live run | high | mitigate | Leak scan over DB, WAL, SHM, logs | closed |
| T-01-34 | I | drills | high | mitigate | Env dummy key overrides the secrets file | closed |
| T-01-35 | D | hour run | medium | mitigate | Same-second and mean-gap checks | closed |
| T-01-36 | I | collection_report | medium | mitigate | Aggregates only | closed |
| T-01-37 | D/T | Poller.step | medium | mitigate | Per-device isolation | closed |
| T-01-38 | D | client.request, live.sh | high | mitigate | Single guarded send path | closed |
| T-01-39 | I | key scan layer | high | mitigate | Key loaded via load_settings, never echoed | closed |
| T-01-40 | D | rate-limit classification | medium | mitigate | Synthetic 429/max_req fixtures | closed |
| T-01-41 | D | probe rate | high | mitigate | Shared guard and lock | closed |
| T-01-42 | I | docs, coverage, summaries | high | mitigate | Covered by real-id and key scans | closed |
| T-01-SC | T | package installs | high | mitigate | Zero runtime deps, lockfile from pypi.org only | closed |

*Status: open · closed · open — below high threshold (non-blocking)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-01-01 | T-01-12 | raw_status keeps SSID/IP/MAC in the local, gitignored DB; the web service and any export must never read raw_status (explicit column lists) | plan 01-03 (owner-approved project constraints) | 2026-11-01 |
| AR-01-02 | T-01-19 | bad_response body excerpt in logs; since the WR-05 fix only size and kind are logged, so the residual risk is nil | plan 01-04 | 2026-11-01 |

*Accepted risks do not resurface in future audit runs.*

### Advisories carried into Phase 2 (non-blocking)

- A1: make the data dir 0700 and DB/backup files 0600 in production.
- A2: add a top-level exception handler in `cli.main` so uncaught exceptions also go through redaction.
- A3: sanitizer recognises SSIDs only under `ssid` and coordinates only when numeric (review IN-06).
- A4: exceptions while reading an HTTPError body are recorded as `internal` rather than `timeout`/`network` (diagnosis accuracy only).
- A5: add an in-app startup delay ≥1.1 s before the first Shelly request (container restarts bypass `scripts/live.sh`).

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-11-01 | 42 | 42 | 0 | gsd-security-auditor (ASVS L1, block_on high) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-11-01
