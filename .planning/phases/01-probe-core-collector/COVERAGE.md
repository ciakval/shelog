# Phase 1: Shelly Cloud Control API coverage

**Written:** 2026-10-31 by the phase planner (API coverage checkpoint).
**Re-validated by:** plan 01-05 Task 2, which replaces each probe-pending tag in the reason column with live evidence.
**Sources:** shelly-api-docs.shelly.cloud `cloud-control-api/` (Getting Started), `communication` (v1, marked deprecated), `communication-v2` (v2.0-beta), `real-time-events`; 01-RESEARCH.md; live discovery run of 2026-10-31 (CONTEXT D-06..D-08).

shelog is a read-only logger (REQUIREMENTS "Out of Scope": device control / settings changes). Every control capability is therefore OPT-OUT. All requests share the account-wide 1 request/second limit.

| capability | decision | reason |
|---|---|---|
| v1 `POST /device/all_status` (all devices of the account in one call, `auth_key` in the form body) | INTEGRATE | Primary poll path (D-06): one request per cycle returns all 7 devices, each with `_updated` (the UTC report time for every model, D-07), and the key never enters a URL (D-05). |
| v1 `all_status` with `show_info=true&no_shared=true` (adds `_dev_info`: `id`, `gen`, `code`, `online`; documented on the Real Time Events page) | INTEGRATE | Probe-only step (probe): used once by `shelog probe` to cross-check each device's model/generation and the cloud `online` flag. The poller does not use it, because the model comes from `devices.toml`. |
| v1 `POST /device/status` (one device per call, `id` + `auth_key`) | OPT-OUT | Marked deprecated by Shelly. Seven devices would need seven requests per cycle under a 1 req/s limit, and `all_status` already returns the same per-device block. |
| v2 `POST /v2/devices/api/get` with `select:["status"]` (1-10 ids, key documented only as a query parameter) | OPT-OUT | Not a runtime path (probe): the key would sit in the URL, it needs an id list, and `_updated` for Gen1 sleepers is unproven. Probed once (PROBE-01/04) for the decision record. Revisit only if v1 starts returning the `endpoint_gone` category. |
| v2 `get` with `select:["settings"]` / `pick` | INTEGRATE | Probe-only step (probe): the only documented cloud route to Gen1 sleep period, thresholds and on-device offsets (PROBE-05). Read once by `shelog probe` and never polled. |
| v1 `POST /device/settings` (undocumented) | OPT-OUT | Probed once under `--errors` to rule it out as a settings source (expected 404) (probe). Not used. |
| v2 `POST /v2/devices/api/set/switch` | OPT-OUT | Device control is out of scope for a read-only logger, and the sensors have no switch. |
| v2 `POST /v2/devices/api/set/cover` | OPT-OUT | Device control is out of scope. |
| v2 `POST /v2/devices/api/set/light` | OPT-OUT | Device control is out of scope. |
| v2 `POST /v2/devices/api/set/groups` | OPT-OUT | Group device control is out of scope. |
| v1 `/device/relay/control`, `/device/relay/roller/control`, `/device/light/control` | OPT-OUT | Device control is out of scope, and these routes are deprecated. |
| v1 `/device/relay/bulk_control`, `/device/light/bulk_control`, `/device/roller/bulk_control` | OPT-OUT | Bulk device control is out of scope. |
| Real Time Events WebSocket (`wss://<host>:6113/shelly/wss/hk_sock`, `Shelly:StatusOnChange`, `Shelly:Online`) | OPT-OUT | Deferred to the v2 backlog (ROB-02). It needs an OAuth access token instead of the auth key, and the DIY client id may be rate-limited. Polling every 60 s with dedup already meets the ~5 min target. |
| Real Time Events `Shelly:CommandRequest` (relay/light/roller commands over the socket) | OPT-OUT | Device control is out of scope. |
| OAuth login + token exchange (`my.shelly.cloud/oauth_login.html`, `/oauth/auth`) | OPT-OUT | Only needed for Real Time Events. The auth key is enough for polling, and OAuth would add an interactive login and token refresh to an unattended service. |
| Statistics / history charts | OPT-OUT | The Cloud Control API documents no history endpoint. A cloud-stats backfill importer is v2 backlog item ROB-03. |
| Integrator API (cloud-to-cloud, many accounts) | OPT-OUT | Meant for third-party integrators. shelog reads one owner account. |
| Account rate limit (1 request/second) | INTEGRATE | `RateGuard` keeps at least 1.1 s between any two requests (poller and probe). HTTP 429 / `Retry-After` and `isok:false` + `max_req` map to `rate_limited` with capped backoff (COLL-01/02). |
| Server URI relocation and auth-key rotation | INTEGRATE | The host comes from `SHELLY_HOST`. A rejected key becomes a distinct `rejected` auth state, stored in `meta` and logged loudly. A vanished v1 route maps to `endpoint_gone` (COLL-02). |
