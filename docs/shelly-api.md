# Shelly Cloud API: findings from the Phase 1 probe

Evidence date: 2026-11-01. One rate-guarded `shelog probe --errors --wrong-host --save-fixtures` run against the real account (12 requests, each at least 1.1 s after the previous one; the real key went only to the configured host, a dummy key to everything else). Sanitized captures are committed under `tests/fixtures/shelly/`; this file quotes key names, shapes and aggregate values only. Devices are referred to by name. No ids, MACs, IPs, SSIDs, serials, host names or keys appear here.

Fixture files used below:

| file | content |
|---|---|
| `tests/fixtures/shelly/v1_all_status.json` | v1 `all_status`, plain (plan 01-03) |
| `tests/fixtures/shelly/v1_all_status_info.json` | v1 `all_status?show_info=true&no_shared=true`, adds `_dev_info` |
| `tests/fixtures/shelly/v2_get_status.json` | v2 `devices/api/get`, `select:["status"]` |
| `tests/fixtures/shelly/v2_get_settings.json` | v2 `devices/api/get`, `select:["status","settings"]` |
| `tests/fixtures/shelly/errors.json` | every captured error or non-poll envelope: `http_status`, `has_retry_after`, `key_placement`, `expected_category`, `body` |
| `tests/fixtures/shelly/synthetic_rate_limit.json` | synthetic rate-limit envelopes (never captured live) |
| `tests/fixtures/shelly/synthetic_cases.json` | synthetic absent-device, empty-block and empty-list envelopes |

`tests/test_classify_fixtures.py` pins `classify()` to the files above.

## Endpoint decision

- **v1 `POST /device/all_status` is the poll path** (D-06). One request returns all seven devices, the key travels in the form body (never in a URL), and every device block carries `_updated`.
- **v2 `POST /v2/devices/api/get` works and carries `_updated` for every model**, Gen1 sleepers included (all seven devices had it in the v2 status). It accepted the key in the URL query and in the JSON body (`auth_key` field). It did not accept the key as a form-encoded body (HTTP 400, `VALIDATION_ERRORS`: the form is not read as `ids`/`select`, so the placement is inconclusive for form) nor as `Authorization: Bearer <key>` (HTTP 401). Unknown ids in a batch are omitted from the returned list without any error entry (HTTP 200).
- **Verdict: no v2 code path is kept in the poller.** v1 never returned `endpoint_gone`, v2 adds nothing the v1 block does not already have, and v2 needs an id list. v2 stays a probe-only step because its `settings` block is the only cloud route to the Gen1 sleep period, thresholds and offsets (see Reporting settings).
- **v1 `POST /device/settings` exists**, although undocumented: HTTP 200, `isok:true`, `data.device_settings` for the one device whose id is posted. The probe expected a 404. It is not used (one request per device, undocumented), but it is a second route to the Gen1 settings if v2 ever changes.
- A successful v2 list is classified `bad_response` by `classify()`. This is deliberate: `classify()` recognises the v1 `all_status` envelope only, the poller never calls v2, and no category is added for a path that is not polled.

## Timestamp semantics

PROBE-03. For every model the real report time is **`_updated`** (a `"YYYY-MM-DD HH:MM:SS"` string) in **UTC**, stored with `ts_source = 'cloud'` (D-07). Evidence: `_updated` equals the device's own Unix clock to within 1 s wherever the device has a clock.

| model | report-time field | timezone | evidence (this run) | device-clock notes | ts_source |
|---|---|---|---|---|---|
| H&T Gen1, fw 1.14 (LR-1, LR-2, Work, Bedroom) | `_updated` | UTC | `unixtime` minus `_updated` as epoch is -1, 0 or +1 s on all four units | `time` is local `HH:MM` (UTC+1 at capture time); ignore it | `cloud` |
| H&T Gen1, older fw 1.11 (LR-3) | `_updated` | UTC | `_updated` was minutes old and plausible against the capture time, while the device clock fields were empty | `unixtime` is `0` and `time` is `""`: this unit has no clock, so `_updated` is the only usable time. `getinfo` and `sensor_error` are absent | `cloud` |
| Flood Gen1, fw 1.14 (LR-Flood) | `_updated` | UTC | `unixtime` minus `_updated` as epoch is 0 s | no `hum` key, `flood` boolean and `tmp` only | `cloud` |
| H&T Gen3, fw 2.0.3 (LR-4) | `_updated` | UTC | `sys.unixtime` minus `_updated` as epoch is +1 s; `ts` (float) falls in the same second as `sys.unixtime` | `ts` is the cloud-side time of the update, `sys.time` is local | `cloud` |

Further evidence:

- The probe computes each device's age from `_updated` read as UTC. At capture time the ages were 0 to 9 minutes for the five Gen1 H&T units, 72 minutes for LR-4 (2 h wake period) and 322 minutes for LR-Flood, all consistent with the device clocks. Reading `_updated` as local time (UTC+1) would make every age 60 minutes larger and contradict the device clocks.
- Every device reported `_sleeping: true` while `_updated` stayed fresh and `_dev_info.online` was true: a sleeping device is not offline. The cloud keeps the last state, so `_updated` (not poll time) is what dedup keys on.
- The reported temperature and humidity fields are `tmp.tC` / `hum.value` (Gen1) and `temperature:0.tC` / `humidity:0.rh` (Gen3). The Gen1 Flood has `tmp` and `flood` only.

## Error behaviour

PROBE-04. HTTP status and body shape come from `errors.json` (keys only here). "Category" is what `classify()` returns, pinned by `tests/test_classify_fixtures.py`.

| case | HTTP | body shape (keys) | Retry-After | classify category | fixture |
|---|---|---|---|---|---|
| wrong key, v1 `all_status` | 401 | `error`=`UNAUTHORIZED`, `errors.invalid_token`, `isok:false` | no | `auth` | `errors.json` (`wrong_key_v1`) |
| wrong key, v2 `get` | 401 | same as above | no | `auth` | `errors.json` (`wrong_key_v2`) |
| wrong host (neighbouring regional host, dummy key) | 401 | identical to the wrong-key body | no | `auth` | `errors.json` (`wrong_host`) |
| key placement: URL query (v2) | 200 | list of device entries | no | `bad_response` (v2 list, not polled) | `v2_get_status.json` |
| key placement: JSON body `auth_key` (v2) | 200 | list of device entries | no | `bad_response` (v2 list, not polled) | `errors.json` (`v2_key_in_body`) |
| key placement: form-encoded body (v2) | 400 | `data.ids`, `data.select`, `error`=`VALIDATION_ERRORS`, `errors.validation_errors`, `isok:false` | no | `http_other` | `errors.json` (`v2_key_in_form`) |
| key placement: `Authorization: Bearer` header (v2) | 401 | same as wrong key | no | `auth` | `errors.json` (`v2_key_in_header`) |
| unknown device id inside a v2 batch | 200 | list containing only the known device; the unknown id is silently absent | no | `bad_response` (v2 list, not polled) | `errors.json` (`unknown_id_v2`) |
| missing route: v1 `/device/settings` | 200 (route exists) | `isok:true`, `data.device_settings` | no | `bad_response` (no `devices_status`) | `errors.json` (`v1_device_settings`) |
| rate limit | unverified, synthetic | three shapes: HTTP 429 with `Retry-After`, HTTP 429 without, HTTP 200 `isok:false` with `errors.max_req` | synthetic | `rate_limited` | `tests/fixtures/shelly/synthetic_rate_limit.json` |
| sleeping device | 200 | `_sleeping:true`, fresh `_updated`, `_dev_info.online:true` | no | `ok` (a normal block) | `v1_all_status_info.json` |
| offline device | 200 | `_dev_info.online:false` is the documented marker; the capture had none offline. The absent-device case (block missing from `devices_status`) is synthetic | no | `ok` (the device is skipped or flagged by the parser) | `tests/fixtures/shelly/synthetic_cases.json` (`absent_device`) |

Conclusions that follow from the evidence:

- **A relocated or wrong host looks exactly like a rejected key.** The sibling regional host answered the dummy key with the same 401 body a wrong key gets. The poller therefore cannot tell "key rotated" from "host moved" by response alone; the `auth` alert text must name both causes.
- **A 200 does not mean success.** The v1 settings route and every v2 call answer HTTP 200 with documents that are not `all_status` envelopes; `classify()` accepts a poll response only when `isok` is true and `data.devices_status` is a non-empty mapping.
- **Unverified: the real throttle envelope.** It was never provoked (CLAUDE.md: no deliberate bursts), so its shape is unverified and covered only by the synthetic fixture. The poller records every real throttle it meets in `poll_log` (category `rate_limited`, `retry_after_s`), so the first genuine envelope is noticed passively.

## Reporting settings

PROBE-05 (orchestrator decision 6, 2026-10-31: no owner checkpoint). Source `status` means read from the polled status, `cloud_settings` from the v2 `settings` block, `default` the documented model default. "not returned" means the cloud did not return it. `max_silence_s` is 1.25 times the wake period, rounded up to whole minutes, in seconds; it is recorded in the owner's local, gitignored `devices.toml`.

| device | model | wake period | temperature threshold | humidity threshold | on-device T/RH offsets | source | max_silence_s |
|---|---|---|---|---|---|---|---|
| LR-1 | H&T Gen1 fw 1.14 | 12 h | 0.5 C | 0.5 % | 0 / 0 | cloud_settings | 54000 |
| LR-2 | H&T Gen1 fw 1.14 | 12 h | 0.5 C | 0.5 % | 0 / 0 | cloud_settings | 54000 |
| LR-3 | H&T Gen1 fw 1.11 | 12 h | 0.5 C | 0.5 % | 0 / 0 | cloud_settings | 54000 |
| LR-4 | H&T Gen3 fw 2.0.3 | 2 h (7200 s) | not returned | not returned | not returned | status | 9000 |
| LR-Flood | Flood Gen1 fw 1.14 | 12 h | 0.5 C | not applicable | 0 / not applicable | cloud_settings | 54000 |
| Work | H&T Gen1 fw 1.14 | 12 h | 0.5 C | 0.5 % | 0 / 0 | cloud_settings | 54000 |
| Bedroom | H&T Gen1 fw 1.14 | 12 h | 0.5 C | 0.5 % | 0 / 0 | cloud_settings | 54000 |

- The v2 `settings` block did return `sleep_mode.period`/`unit`, both thresholds and both offsets for every Gen1 device, sleeping or not. No `default` source was needed. The Gen3 H&T settings returned a `sleep_mode` of 7200 s that agrees with `sys.wakeup_period` in the polled status (the recorded source), but no thresholds or offsets.
- All on-device offsets are 0, so shelog's `t_offset`/`rh_offset` stay the only calibration.
- `external_power` is 0 on every Gen1 H&T (battery powered).
- The 12 h Gen1 period is the longest a device may stay silent when nothing changes; a change beyond the 0.5 thresholds triggers an earlier report. The silence threshold is therefore a ceiling, and real inter-report gaps are expected to be much shorter.

### Tuning plan

The one-hour run in plan 01-08 cannot observe a 12 h period. Phase 2 (alerts) therefore recomputes each device's threshold from observed inter-report gaps: per device, the largest gap between consecutive stored `_updated` times over the first production days, times 1.25. It replaces each value before silence alerts are enabled and records the new source as `observed`. Until then the values above stand.

## Rate limit and operations

- The 1 request per second budget is **per account** and shared with the Shelly app, a local `shelog poll` and production (Phase 2). A local poll stops once production polls the same account; use `shelog poll --once` for checks.
- No deliberate bursts, ever. `RateGuard` keeps at least 1.1 s between any two requests across both hosts, and the probe has no rate-limit-testing mode. The real rate-limit envelope remains unverified.
- `poller.AUTH_CONFIRMATIONS` stays at **2**. A throttle that arrives as HTTP 401 cannot be excluded without live throttle evidence, and a wrong host looks like a wrong key (see Error behaviour), so a single 401 must never raise the "auth rejected" alert.
- The probe takes the same instance lock as `poll`; if one holds it the other exits with code 3 before sending anything.
