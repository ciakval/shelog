# Phase 1: Probe & Core Collector - Research

**Researched:** 2026-10-31
**Domain:** Shelly Cloud polling (Gen1 + Gen3 battery sensors), SQLite WAL time-series collector, secret-safe Python CLI
**Confidence:** MEDIUM-HIGH (HIGH on stack, schema, parsing, timestamps and testing, all verified by running code or reading the live snapshot; MEDIUM-LOW on Shelly error envelopes, v2 behaviour for Gen1 and cloud-side settings, which can only be settled by the live `probe`)

<user_constraints>
## User Constraints (from CONTEXT.md)

> Copied verbatim except that real device ids (D-07, D-08 example, D-10) are replaced by neutral wording, per the research brief's rule that no real identifiers may enter this file.

### Locked Decisions

**Owner stance**
- **D-01:** Owner cares about the result; technical choices in SPEC.md and research are recommendations, not mandates. Prefer minimal dependencies and clean, well-automated code.
- **D-02:** Repo is public. `devices.toml` (real device IDs) and `.env` (secrets) are gitignored and live on the server in production; only `devices.example.toml` and `.env.example` are committed.

**Secrets & live runs**
- **D-03:** The app loads `./.env` from its working directory by itself (no `.env` path on the command line). A Claude Code hook blocks any command mentioning `.env` — never `cat`/`grep`/source `.env`, never pass it as an argument. Live runs are `uv run shelog probe`, `uv run shelog poll --once`, etc. Real env vars override `.env` values.
- **D-04:** Env var names: `SHELLY_AUTH_KEY`, `SHELLY_HOST` (host may be given with or without `https://`; normalize). Further vars (DB path, poll interval, log level) are Claude's discretion with safe dev defaults.
- **D-05:** The auth key must never be printed: send it in the POST body (form field `auth_key`) where the endpoint accepts it; redact URLs/keys in logs and exception text; silence httpx/httpcore INFO logging; `poll_log.error` stores only a coarse category enum, never raw exception text.

**API endpoint & real response shape (verified live 2026-10-31)**
- **D-06:** `POST https://<host>/device/all_status` with form body `auth_key=<key>` WORKS: returns `{"isok": true, "data": {"devices_status": {<id>: <status>, ...}}}` with all 7 devices in one call. This is a valid primary (one request per cycle, no id list needed). The v2 `POST /v2/devices/api/get` is still to be probed (PROBE-01/04) and compared; choose whichever is reliable, keep the other as fallback only if cheap. Rate limit 1 req/s account-wide applies to everything.
- **D-07:** Report timestamp: `_updated` is a string `"YYYY-MM-DD HH:MM:SS"` in **UTC** (verified: equals device `unixtime` within 1 s where present). Gen1 `unixtime` is **0** on some devices (one older-firmware H&T and the Flood). Gen3 `sys.unixtime` equals `_updated`; `sys.time` is local CET (ignore). → Use `_updated` (ts_source `cloud`) for all models; device clock is only a cross-check. Validate plausibility (reject 0/negative/future > small skew/ancient; count rejections).
- **D-08:** Field paths (verified):
  - Gen1 H&T: `tmp.tC` (NOT `tmp.value`, which follows configured units), `tmp.is_valid`, `hum.value`, `hum.is_valid`, `bat.value` (%), `bat.voltage`, `sensor_error`, `is_valid`, `_sleeping`, `act_reasons` (e.g. `["sensor"]`), `getinfo.fw_info.device` (e.g. `shellyht-<MAC tail>`).
  - Gen1 Flood: `tmp.tC`, `flood` (bool), `bat.value`, `rain_sensor`, no humidity.
  - Gen3 H&T: `temperature:0.tC`, `humidity:0.rh`, `devicepower:0.battery.percent`, `devicepower:0.battery.V`, `devicepower:0.external.present`, `sys.unixtime`, top-level `ts`, `code`.
  - No `_dev_info`/model code in the v1 envelope → model comes from `devices.toml` (`model = ht_gen1 | ht_gen3 | flood_gen1`), optionally cross-checked against keys.
- **D-09:** A raw snapshot of the live response exists at the scratchpad path `/tmp/claude-1000/-home-ciakval-GIT-shelog/835d21f7-af00-4c70-9137-da6bea6a204e/scratchpad/all_status.json` (contains MACs, SSIDs, IPs, real IDs — must be sanitized before anything from it is committed). It may not survive; the `probe` command must be able to regenerate it.

**Devices (owner-confirmed mapping; lives in gitignored `devices.toml`, already written at repo root)**
- **D-10:** [Real device ids redacted in this file on purpose: see CONTEXT.md D-10; the ids are not to be copied into new documents.] Mapping by name: LR-1 (outer corner, assumed worst), LR-2 (near corner), LR-3, LR-4 (Gen3), LR-Flood (centre) — group `subfloor`; Work, Bedroom — group `reference`. All Gen1 except LR-4 (Gen3) and Flood. Fields: `id, name, note, group, model, t_offset, rh_offset` (+ optional per-device silence threshold).
- **D-11:** Reporting cadence is long and per model (Gen1 battery ~12 h max wake period unless a 0.5 °C / 0.5 %RH threshold is crossed; Gen3 ~2 h). Silence thresholds are per device/model, never a flat 6 h. Observed first look: one sensor last reported 4 h earlier.

**Storage**
- **D-12:** SQLite WAL, single writer (poller). Full schema in migration `0001` from day one (readings, raw_status, outdoor long-format, poll_log, alert_state, meta) so later phases add no breaking migrations. Store raw, uncalibrated values; NULL stays NULL (never coerced to 0). Raw status JSON stored when a reading row is inserted (on change), enough to reparse later.
- **D-13:** Dedup: insert only when `_updated` advanced for that device (unchanged re-reports are dropped — acceptable); count ignored/rejected rows.

**Process/tooling (recommended, from research)**
- **D-14:** Python 3.13, uv, package `shelog`, CLI `shelog` with subcommands (`probe`, `poll [--once]`, later `serve`, `export`, `calibrate`). Runtime deps minimal: `httpx2` (fallback `httpx`) — or stdlib urllib if simpler; stdlib sqlite3/tomllib/argparse/logging. Dev: pytest, ruff (incl. DTZ). Single-threaded sync poll loop on monotonic deadlines, jitter, capped exponential backoff (~2–5 min cap, since the cloud keeps only last state), honour 429/Retry-After, distinct auth-rejected state, SIGTERM-clean, single-instance flock.
- **D-15:** Host Python is 3.14 (WSL); target 3.13 for the image — use `uv` with a pinned `requires-python`/`.python-version` so dev matches production.

### Claude's Discretion
- Exact module layout, CLI flag names beyond those above, env var names other than D-04, error category enum names, fixture file naming, whether to keep a v2 code path at all after probing.

### Deferred Ideas (OUT OF SCOPE)
CONTEXT.md has no `## Deferred Ideas` section. Per ROADMAP/REQUIREMENTS, the following are out of scope for Phase 1: Docker/compose/CI/Bombur deployment (Phase 2), ntfy alerts and `/api/health` (Phase 2), Open-Meteo weather (Phase 3), derived metrics / calibration application / read API (Phase 4), web UI (Phase 5), `reparse`, CSV export, calibration CLI, Real Time Events, cloud-stats backfill (v2 backlog), tracking the dehumidifier confounder.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| PROBE-01 | `shelog probe` prints/saves raw status JSON for every configured device | Probe design (Architecture Pattern 6): v1 `all_status` baseline + optional variants, raw output under gitignored `data/probe/`, summary prints device **names** only |
| PROBE-02 | Sanitized fixtures per model + test that fails on real identifiers | Device-centric deterministic pseudonymizer prototyped and validated on the live snapshot (Pattern 5); structural allowlist test works in CI with no secrets |
| PROBE-03 | Report-time field per model + timezone semantics documented | Verified from snapshot: `_updated` UTC for all models (table in "Timestamp semantics"); `unixtime` 0 on 2 Gen1 units; Gen3 `sys.unixtime` may be null if no NTP sync [CITED] |
| PROBE-04 | Error behaviour captured: wrong key, wrong host, unknown id, rate limit, sleeping/offline | Probe step matrix that stays below 1 req/s; closed outcome enum with data-driven classifier; honest status of what is unknown (Open Questions) |
| PROBE-05 | Reporting settings per device recorded | Three-tier source: Gen3 status carries `sys.wakeup_period` [VERIFIED]; v2 `select:["settings"]` to try for Gen1 [CITED docs, unverified for sleepers]; fallback = documented defaults + observed-interval statistics |
| COLL-01 | One batched request per interval (60 s jittered), never > 1 req/s | v1 `all_status` = 1 request for all devices; `RateGuard` min gap 1.1 s on monotonic clock |
| COLL-02 | Capped backoff, 429/Retry-After, distinct auth-rejected state | Pure `next_delay()` + outcome enum + injected clock; tests with fake clock |
| COLL-03 | Insert only when report ts advanced; implausible ts rejected and counted | `plausible_ts()` + `decide()` pure functions; `poll_log.n_unchanged` / `n_rejected` columns |
| COLL-04 | Raw uncalibrated T/RH/battery/flood with ts_source; NULL never 0 | Per-model parsers (field paths in Standard Stack); bool/NaN/None guards; `STRICT` tables verified to reject text in REAL |
| COLL-05 | Every poll attempt logged with outcome, HTTP status, device count | `poll_log` DDL (validated); row written for failures in same transaction path |
| COLL-06 | Raw status JSON retained per stored reading | `raw_status` row written in the same transaction as each inserted `reading` |
| COLL-07 | Auth key never in logs, errors, DB, public output | Key in POST body (urllib never formats body), redaction filter, enum-only errors, leak-scan tests + `shelog check` command that scans without printing |
| COLL-08 | Device inventory from server-side `devices.toml` | `tomllib` loader with strict validation; `devices.example.toml` only in repo |
</phase_requirements>

## Summary

The collector can be built with **zero third-party runtime dependencies**: stdlib `urllib.request` is sufficient for a single form-encoded POST with a timeout, TLS verification, response headers (for `Retry-After`) and redirect blocking, and the owner's own discovery run already proved that exact transport works against the real account. This follows D-01/D-14 (minimal dependencies, "or stdlib urllib if simpler"), removes the young `httpx2` package from Phase 1 entirely, and makes key leakage structurally harder because the key is only ever in the request body and `urllib` exceptions never format the body. HTTP is hidden behind one small `send()` seam so tests inject canned responses with no sockets, and so `httpx2` could replace it later without touching callers.

**Primary endpoint recommendation: v1 `POST /device/all_status` with `auth_key` in the form body.** It is the only call verified live: one request for the whole account, `_updated` present on all seven devices (the field that is the report time for every model), key outside the URL. The v1 `device/status` page is marked deprecated by Shelly, and `all_status` itself is undocumented there (community-used), so the poller must treat "endpoint disappears / non-JSON" as a distinct loud category, and the probe should record v2 `/v2/devices/api/get` for the decision record. v2 should become a code path only if the probe shows `status._updated` is present for Gen1 devices; without it v2 would force the unreliable `unixtime` (0 on 2 of 6 Gen1 units) and is unusable for timestamps. The v2 docs show `auth_key` only in the query string and say nothing about body/header, about errors, or about 429 behaviour.

Everything that depends on how Shelly *fails* (wrong key, wrong host, 429, unknown id) is not documented anywhere I could reach; sources conflict (HTTP 429 vs HTTP 401 vs HTTP 200 with `{"isok":false,"errors":{"max_req":...}}`). The plan must therefore build the classifier as a small table-driven function with an `unexpected` fallback, capture the real envelopes in the probe (opt-in `--errors` step, spaced at >= 1.1 s except one deliberate, flagged burst), commit them as sanitized fixtures, and then pin the table to those fixtures. Per-device reporting settings are only partly obtainable: Gen3 exposes `sys.wakeup_period` (7200 s) directly in the status; for Gen1 the documented path is v2 `select:["settings"]` (unverified for sleeping devices), with a fallback of model defaults plus thresholds tuned from observed report intervals.

**Primary recommendation:** Build `shelly/client.py` on stdlib `urllib` against v1 `all_status` (key in body), parse with three pure per-model functions keyed on `_updated` (UTC, `ts_source='cloud'`), store with the validated `0001_init.sql` below, drive a single-threaded loop with an injected clock, and gate everything on sanitized fixtures generated from the live snapshot by the device-centric pseudonymizer (a prototype of which already removes all 21 identifiers from the live snapshot).

## Architectural Responsibility Map

This phase is a backend/CLI-only system; the "tiers" are the process roles from ARCHITECTURE.md.

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Shelly HTTP I/O, rate guard, error classification | Poller (`shelly.client`) | — | Only the poller holds the key and talks to Shelly (OPS-03 later: web never gets the key) |
| Parse device status to a `Report` | Pure function layer (`shelly.parse`) | — | No I/O; tested against fixtures |
| Dedup / plausibility decision | Pure function layer (`store.decide`) | Poller | Pure so it is table-testable; DB only supplies the "last row" |
| Persistence (schema, readings, raw, poll_log) | Database (SQLite WAL) | Poller (sole writer) | Single-writer invariant |
| Scheduling, backoff, SIGTERM, flock | Poller (`poller.py`) | — | Single-threaded loop on monotonic deadlines |
| Config/secrets loading, redaction | CLI/config layer | Logging filter | Key must be loaded by the app itself (D-03) |
| Probe + fixture sanitization | CLI (`probe`) + dev tool | tests | One-off live discovery; writes raw output only under gitignored `data/` |
| Leak verification | CLI (`check`) + tests | — | Lets the owner/Claude verify "no key anywhere" without ever reading `.env` |

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python | 3.13 (`.python-version` = `3.13`; uv managed 3.13.15 installed during this research; image `python:3.13-slim`) | Runtime | Matches target image; D-15 [VERIFIED: `uv python install 3.13` → "Installed Python 3.13.15"] |
| stdlib `urllib.request` / `http` | 3.13 | Shelly HTTP client (POST form, timeout, no redirects) | Zero deps; same transport as the verified discovery run; behaviour prototyped (below) [VERIFIED: local prototype run] |
| stdlib `sqlite3` | bundled (host 3.51.2; uv-managed 3.13.15 ships 3.53.1; `python:3.13-slim` ships 3.46.1 per STACK.md) | Storage | STRICT (>= 3.37), WITHOUT ROWID, UPSERT all available; **write SQL valid for 3.46** [VERIFIED: executed DDL on 3.51.2; version numbers from `python3 -c` and uv sync output] |
| stdlib `tomllib`, `argparse`, `logging`, `signal`, `fcntl`, `threading.Event` | 3.13 | Config, CLI, logs, SIGTERM, single-instance lock, interruptible sleep | No `click`/`dotenv`/`pydantic` needed [CITED: STACK.md] |
| uv | system 0.12.19 on host; Docker pin 0.13.6 (PyPI latest 0.13.6) | Env/lock/build | Bombur standard [VERIFIED: `uv --version`, PyPI JSON] |
| `uv_build` backend | `>=0.13.6,<0.14` | Build backend | Works under host uv 0.12.19 (tested `uv sync`) [VERIFIED: ran `uv sync` in a scratch project] |

### Supporting (dev only)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pytest | 9.2.0 | Tests | All tests; sync only [VERIFIED: PyPI + `uv run pytest --version`] |
| ruff | 0.17.3 | Lint + format | Select `E,F,I,UP,B,SIM,RUF,DTZ,S` [VERIFIED: PyPI + `uv run ruff --version`] |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `urllib.request` | `httpx2` 2.16.0 (released 2026-10-21, 10 days old; deps anyio, httpcore2, idna, truststore) | `MockTransport` convenience and nicer API, but a brand-new package, 4 transitive deps, and URL/exception text that embed whatever the URL holds. Starlette's `TestClient` will pull httpx2 into the **dev** tree in Phase 4 anyway, so keeping it out of **runtime** now costs nothing. Revisit only if urllib proves inadequate. |
| `urllib` | plain `httpx` 0.28.1 | Stagnant (last release 2024-12-06); no advantage over urllib here |
| v1 `all_status` | v2 `/v2/devices/api/get` | Needs id list, key (documented) in query string, Gen1 `_updated` presence unproven. Probe it; adopt only if it adds something v1 lacks |

**Installation:**
```bash
uv python install 3.13            # one-time on this host (uv here is set to python-downloads=manual)
uv init --package --python 3.13 --name shelog --no-workspace .   # NOTE: default would pin the host 3.14
# then edit pyproject.toml: uv_build>=0.13.6,<0.14 ; add [dependency-groups] dev = ["pytest>=9.2", "ruff>=0.17"]
uv sync
```

**Version verification (run this session):** `pytest 9.2.0` (2026-10-27), `ruff 0.17.3` (2026-10-27), `uv_build 0.13.6` (2026-10-27), `uv 0.13.6`, `httpx2 2.16.0` (2026-10-21), `httpx 0.28.1` (2024-12-06) — all from `https://pypi.org/pypi/<pkg>/json` [VERIFIED: PyPI JSON API]. `python:3.13-slim` last updated 2026-10-28 [VERIFIED: Docker Hub API].

## Package Legitimacy Audit

Runtime packages for Phase 1: **none**. Only the dev group and the build backend are installed.

| Package | Registry | Age | Downloads | Source Repo | Verdict | Disposition |
|---------|----------|-----|-----------|-------------|---------|-------------|
| pytest | PyPI | long-established (pytest-dev) | seam returned `null` (lookup limitation, not a signal) | github.com/pytest-dev/pytest | SUS (reasons: `too-new` = latest release 4 days old, `unknown-downloads`) | Flagged — see note |
| ruff | PyPI | established (Astral) | `null` | docs.astral.sh/ruff | SUS (`too-new`, `unknown-downloads`) | Flagged — see note |
| uv_build | PyPI | established (Astral, ships with uv) | `null` | pypi.org/project/uv | SUS (`too-new`, `unknown-downloads`) | Flagged — see note |
| httpx2 | PyPI | 10 days | `null` | github.com/pydantic/httpx2 | SUS (`too-new`, `unknown-downloads`) | **Not installed in Phase 1** (urllib recommended) |

**Packages removed due to [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** `pytest`, `ruff`, `uv_build` (and `httpx2`, not installed). All SUS reasons are mechanical: the seam's download-count lookup returned `null` for **every** package including pytest, and `too-new` fires on a recent release date of a long-established project. Registry metadata cross-checked: each links to its known maintainer repository. Because the protocol requires it, the planner adds **one bundled** `checkpoint:human-verify` task (not one per package) in the scaffold plan: "confirm dev-group installs (`pytest`, `ruff`, `uv_build`) resolve to the expected publishers in `uv.lock`".

`pytest`, `ruff`, `uv_build` names came from STACK.md (project research) and were confirmed by PyPI JSON metadata, not by Context7/official-docs lookup this session; treat as `[ASSUMED]`-tier for the purposes of the install gate.

## Architecture Patterns

### System Architecture Diagram

```
                       +---------------------------+
   .env (cwd, loaded   |  config.load()            |   devices.toml (server-side, gitignored)
   by the app itself)->|  env > .env ; host norm.  |<--- strict validation, unique ids, enums
                       +------------+--------------+
                                    |  Settings(key redacted in repr), Devices
        shelog probe                |                       shelog poll [--once]
   +------------------+             |             +--------------------------------------+
   | probe steps      |             |             | poller loop (single thread)          |
   | (>=1.1 s apart)  |             |             |  flock(data/poller.lock)             |
   +--------+---------+             |             |  SIGTERM -> stop Event               |
            |                       v             |  deadline = monotonic + interval+jit |
            |             +---------+---------+   +----------------+---------------------+
            +------------>| RateGuard (1.1 s) |<-----------------+
                          +---------+---------+
                                    |  POST https://HOST/device/all_status   body: auth_key=...
                                    v
                          +-------------------+   transport seam: send(url, body, timeout)
                          | shelly.client     |   -> RawResponse(status, headers, bytes) | TransportError(category)
                          | classify()        |   outcome in {ok, auth, rate_limited, http_5xx, http_other,
                          +---------+---------+                timeout, network, bad_response, no_devices}
                       ok |         | failure
                          v         v
              +-----------+---+   +-+---------------------------+
              | parse (pure)  |   | next_delay(): capped        |
              | per model:    |   | exp. backoff + jitter,      |
              | ht_gen1/gen3/ |   | max(Retry-After), auth=300s |
              | flood_gen1    |   +-----------------------------+
              +-----------+---+
                          v   Report(device_id, ts, ts_source, t_c, rh, battery, flood) | ParseReject(reason)
              +-----------+----------------+
              | decide(last, report, now)  |  pure: insert | unchanged | regressed | implausible
              +-----------+----------------+
                          v
        BEGIN IMMEDIATE
          INSERT poll_log (ok, http_status, error, n_devices, n_new, n_unchanged, n_rejected, ...)
          for each insert:  INSERT reading ... ON CONFLICT DO NOTHING ; INSERT raw_status ...
        COMMIT   -----------> data/shelog.db (WAL)      <- read later by web (Phase 2+)
```

### Recommended Project Structure
```
pyproject.toml  uv.lock  .python-version(3.13)  ruff config in pyproject
devices.example.toml  .env.example
docs/shelly-api.md            # PROBE-03/04/05 findings (sanitized), committed
src/shelog/
├── cli.py                    # argparse: probe | poll [--once] | check   (serve/export later)
├── config.py                 # env + minimal .env parser + devices.toml loader; Settings.__repr__ redacts
├── logging_setup.py          # UTC formatter, RedactingFilter, httpx/httpcore -> WARNING (harmless w/ urllib)
├── clock.py                  # Clock protocol + SystemClock (+ FakeClock lives in tests)
├── db.py                     # connect_rw(), migrate(), schema guard (refuse newer DB)
├── migrations/0001_init.sql  # complete schema (below)
├── shelly/
│   ├── transport.py          # urllib adapter: send(); NoRedirect; size cap; closes HTTPError
│   ├── client.py             # RateGuard, fetch_all_status() -> Outcome ; classify()
│   └── parse.py              # PURE: parse_ht_gen1 / parse_ht_gen3 / parse_flood_gen1 ; plausible_ts
├── store.py                  # decide() PURE ; write_cycle() SQL
├── poller.py                 # next_delay() PURE ; run() loop ; step() one cycle
├── probe.py                  # probe steps + summary printer + --save-fixtures
└── sanitize.py               # deterministic pseudonymizer (dev tool, importable by tests)
tests/
├── fixtures/shelly/          # sanitized live captures + clearly-named synthetic_*.json
├── fixtures/devices.test.toml
└── test_*.py
```

### Pattern 1: Transport seam + closed outcome enum
**What:** `client.py` never touches sockets; it calls `send(url, body, timeout) -> RawResponse` (or raises `TransportError(category)`), then `classify(raw)` returns an `Outcome`. The default `send` is the urllib adapter; tests pass a list-backed fake. One extra real-socket test (stdlib `http.server` on 127.0.0.1) exercises the adapter itself.
**When to use:** Always; it is also what makes `httpx2` a drop-in later.
**Key facts verified by prototype (Python 3.14 host, same stdlib API in 3.13):**
- `urllib.request.Request(url, data=urlencode({"auth_key": k}).encode(), method="POST")` sends `Content-Type: application/x-www-form-urlencoded` and the key only in the body.
- A custom `HTTPRedirectHandler.redirect_request` returning `None` turns any 3xx into `HTTPError` (never follow a redirect with a key-bearing body).
- `HTTPError.headers.get("Retry-After")` is available; `str(HTTPError)` is `'HTTP Error 429: Too Many Requests'` and `str(URLError)` is `'<urlopen error [Errno 111] Connection refused>'`; neither contained the sentinel key.
- HTTP 200 with `{"isok": false, "errors": {"max_req": ...}}` parses normally, so `isok` must be checked.
**Example:**
```python
# Source: prototype run in this session (scratchpad urltest.py), stdlib docs for urllib.request
import json, urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):          # returning None => HTTPError for 3xx
        return None

_OPENER = urllib.request.build_opener(_NoRedirect)
MAX_BODY = 1_048_576

@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: dict[str, str]        # lower-cased names
    body: bytes

class TransportError(Exception):
    def __init__(self, category: str):      # 'timeout' | 'network' ; never carries the original text
        super().__init__(category)
        self.category = category

def send(url: str, form: dict[str, str], timeout: float) -> RawResponse:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as r:                       # noqa: S310  (https enforced in config)
            return RawResponse(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(MAX_BODY + 1))
    except urllib.error.HTTPError as e:
        try:
            return RawResponse(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read(MAX_BODY + 1))
        finally:
            e.close()                                                        # else ResourceWarning
    except TimeoutError as e:
        raise TransportError("timeout") from None
    except (urllib.error.URLError, OSError):                                 # DNS, refused, reset, TLS
        raise TransportError("network") from None
```
(`socket.timeout` is `TimeoutError` in 3.10+; a `URLError` wrapping a timeout also needs mapping: check `isinstance(e.reason, TimeoutError)` before falling to `network`.)

### Pattern 2: Pure per-model parsers (field paths verified against the live snapshot)
**What:** `parse_<model>(device_id, block, now) -> Report | ParseReject`. Selected by the `model` from `devices.toml`; cross-checked structurally (mismatch -> `ParseReject("shape")`, counted, never raises).
**Verified shapes** (`[VERIFIED: scratchpad all_status.json:1]`, quoted verbatim):
- Gen1 H&T: `"tmp":{"value":26.25,"units":"C","tC":26.25,"tF":79.25,"is_valid":true}`, `"hum":{"value":66.5,"is_valid":true}`, `"bat":{"value":100,"voltage":3.04}`, `"sensor_error":0`, `"_updated":"2026-10-31 16:03:58"`, `"unixtime":1793462638`, `"act_reasons":["sensor"]`, `"_sleeping":true`. `hum.value` is sometimes an int (`"hum":{"value":100,"is_valid":true}`) and sometimes a float (`66.5`): accept both.
- Gen1 H&T **older firmware** (v1.11.0, one unit): has `"unixtime":0`, `"time":""`, **no** `getinfo`, **no** `sensor_error`, and `"cloud":{"enabled":true,"connected":false}` while still delivering fresh `_updated`. Parsers must treat `sensor_error` and `getinfo` as optional.
- Gen1 Flood: `"flood":false`, `"rain_sensor":false`, `"tmp":{...tC...}`, `"bat":{"value":100,"voltage":3.04}`, **no `hum` key** at all, `"unixtime":0`, `"time":""`.
- Gen3 H&T: `"temperature:0":{"id":0,"tC":25.8,"tF":78.4}`, `"humidity:0":{"id":0,"rh":46.1}`, `"devicepower:0":{"id":0,"battery":{"V":6.06,"percent":100},"external":{"present":false}}`, `"code":"S3SN-0U12A"`, `"_updated":"2026-10-31 18:21:58"`, `"ts":1793470918.51`, `"sys":{... "unixtime":1793470918, "last_sync_ts":1793470917, ... "wakeup_reason":{"boot":"deepsleep_wake","cause":"periodic"},"wakeup_period":7200 ...}`.
- Envelope: `{"isok":true,"data":{"devices_status":{...},"pending_notifications":{}}}`. Ids are lowercase hex keys; Gen1 ids appear as either the 6-hex MAC tail or the full 12-hex MAC; Gen3 id is the 12-hex MAC and is also repeated inside the block as `"id"`.
**Rules (all unit-tested):**
- `ts` = `_updated` parsed as UTC; `ts_source='cloud'`. Parse with `datetime.fromisoformat(f"{s}+00:00")` (verified: `"2026-10-31 18:21:58"` -> 1793470918 == the Gen3 `sys.unixtime`, i.e. exactly equal); this is DTZ-clean for ruff.
- Numeric guards: exclude `bool` (`isinstance(True, int)`), reject non-finite (`json` accepts `NaN`), reject `None`; a field failing the guard becomes `NULL`, never `0`.
- Validity: Gen1 `t_c = tmp.tC` only if `tmp.is_valid is True`; `rh = hum.value` only if `hum.is_valid is True`; if `sensor_error` is present and non-zero, both are `NULL`. Gen3: `tC`/`rh` are `null` when no valid reading [CITED: gen2 Temperature docs: "`tC` ... is `null` when a valid reading can't be obtained"]. A row with NULL T/RH is still inserted (liveness + battery); it is the *report* that advanced.
- Absurd values (T outside [-50, 100] C, RH outside [-5, 105] %) -> that field `NULL` + WARNING + counted in `n_rejected`. Do not clamp RH 100.x to 100: store raw.
- `flood`: `bool` required -> `int`; anything else -> NULL.
- Do **not** use the device clock for `ts`. Optional cross-check: if `unixtime > 0` and `abs(unixtime - ts) > 120`, log one WARNING per device per day (never stored).

### Pattern 3: Dedup and plausibility as pure functions
```python
# decide() returns one of: INSERT | UNCHANGED | REGRESSED | IMPLAUSIBLE
MIN_PLAUSIBLE_TS = 1_577_836_800     # 2020-01-01: before this the value is garbage (e.g. 0) -- [ASSUMED] choice
MAX_FUTURE_SKEW_S = 120              # [ASSUMED] choice

def plausible_ts(ts: int, now: int) -> bool:
    return MIN_PLAUSIBLE_TS <= ts <= now + MAX_FUTURE_SKEW_S

def decide(last_ts: int | None, ts: int, now: int) -> str:
    if not plausible_ts(ts, now): return "implausible"
    if last_ts is None or ts > last_ts: return "insert"
    return "unchanged" if ts == last_ts else "regressed"
```
- "Ancient" means only before 2020. A battery sensor that died three weeks ago legitimately keeps reporting its old `_updated` (-> `unchanged`, then nothing); do **not** reject by age relative to now.
- `last_ts` per device is loaded once at start (`SELECT ts FROM reading WHERE device_id=? ORDER BY ts DESC LIMIT 1`; query plan verified as `SEARCH reading USING PRIMARY KEY (device_id=?)`), then updated in memory after each insert.
- Second line of defense: `INSERT ... ON CONFLICT DO NOTHING`; a `rowcount == 0` is counted as `unchanged`, never silent (verified: duplicate insert -> `rowcount 0`). Add a periodic WARNING: "device polled OK N times but no row ever inserted" (catches a wrong `ts` source).
- Missing/unparseable `_updated`: fall back to `ts_source='poll'` with a value-change rule *only when the key is absent entirely*; a present-but-implausible `_updated` is rejected and counted. (All seven devices carry `_updated` today, so this branch exists for COLL-03's "no reliable timestamp" clause and costs ~15 lines.) On a poll-time fallback, do not stamp a stale cloud state with `now` after an outage: mark `ts_source='poll'` so later phases can exclude it from time-weighted edges.

### Pattern 4: Single-threaded loop with an injected clock
```python
# Source: design; patterns verified in prototypes: Event.wait for interruptible sleep, fcntl.flock LOCK_NB refusal (verified)
class Clock(Protocol):
    def monotonic(self) -> float: ...
    def time(self) -> float: ...                       # wall clock, for polled_at / plausibility only
    def wait(self, stop: threading.Event, timeout: float) -> bool: ...   # True if stop was set

def next_delay(interval: float, consecutive_failures: int, outcome: str,
               retry_after: float | None, rng: random.Random) -> float:
    if outcome == "ok":
        return interval * rng.uniform(0.9, 1.1)                      # +-10% jitter, deadline-based so no drift
    base = min(CAP_S, interval * 2 ** (consecutive_failures - 1))     # 60,120,240,300 ... CAP_S = 300
    delay = base / 2 + rng.uniform(0, base / 2)                       # "equal jitter"
    if outcome == "rate_limited" and retry_after: delay = max(delay, retry_after)
    if outcome == "auth": delay = max(delay, 300.0)                   # do not hammer a rejected key
    return max(delay, MIN_GAP_S)
```
- Deadlines advance by the interval (not "now + interval") so a slow request does not accumulate drift; on failure the next deadline is `now + next_delay`.
- `RateGuard.acquire()` blocks (via `clock.wait`) until `last_sent + 1.1 s`; **every** Shelly request in the process goes through it (probe included). One test asserts that over a long fake-clock run with injected failures the minimum gap between `send` calls is >= 1.1 s.
- Take `flock(LOCK_EX|LOCK_NB)` on `data/poller.lock` in `poll` **and** `probe` (so they cannot overlap on one machine); a second instance exits non-zero with a clear message. (A different machine sharing the account cannot be prevented; document it.)
- SIGTERM/SIGINT handler only sets the `Event`; the loop finishes the current transaction and exits 0.
- `poll --once` = one `step()` then exit; exit code non-zero when the outcome is not `ok` (useful for the owner and for `/loop`-style checks).
- A crash-safe failure to open the DB or invalid config must exit non-zero fast (Phase 2: `restart: unless-stopped` + a bad config would become a hot loop; sleep 30 s before exit in `poll` mode is cheap insurance).

### Pattern 5: Fixture sanitizer (deterministic, structural, no secrets needed to test)
**What was prototyped and validated on the live snapshot:** a device-centric pseudonymizer. Result: 25 original literals mapped, **0 originals surviving**, a structural scanner found **21 identifiers in the raw snapshot and 0 in the sanitized output**, including the IPv6 link-local address that embeds the Gen3 MAC (EUI-64) and the BLE address (which is *not* the Wi-Fi MAC: it is a different MAC) — both would be missed by a naive "replace the device id" approach.
**Design (follow this, do not hash):**
1. Hashing a 24-bit (6-hex) id with any public salt is brute-forceable: use **sequential counters**, not hashes.
2. Index devices `n = 1..N` by sorted original id. Assign pseudo-MAC `02:00:00:00:00:<n>` (locally administered prefix, 12 hex bare form `0200000000nn`).
3. For each device derive **all forms** from one mapping: original id (6- or 12-hex, whichever form it uses) -> pseudo id of the same length (12-hex = pseudo MAC, 6-hex = its tail); original bare MAC, colon MAC, and the **6-hex MAC tail in upper case** (appears in `getinfo.fw_info.device`, e.g. `shellyht-<TAIL>`) -> the pseudo equivalents, preserving letter case.
4. Other MAC-shaped values (`wifi.bssid`, `ble.addr`, with a `,p` suffix) get a second pseudo space `02:01:00:00:00:<k>`.
5. Map SSID -> `fixture-ssid-<k>`; IPv4 -> `192.0.2.<k>` (RFC 5737 TEST-NET-1); IPv6 -> `2001:db8::<k>` (RFC 3849). Keys are discovered **by field name** (`mac`, `ssid`, `ip`, `sta_ip`, `sta_ip6`, `bssid`, `addr`, `id`) plus the keys of `devices_status`, not by a list of known values.
6. Apply as a **single pass, longest-first, case-insensitive regex alternation** over the serialized JSON (avoids partial/overlapping replacements: my first prototype failed exactly this way, colliding id and MAC-tail numbering). Serialize with `sort_keys=True` for stable diffs.
7. Output is deterministic for a given input; do not rely on idempotence (re-sanitizing already-sanitized output renumbers). The sanitizer refuses input whose ids already match the pseudo pattern.
8. Post-condition inside the tool: scan the output with the structural scanner and exit non-zero if anything is left.
9. Fixtures are generated **once** and committed; do not regenerate casually (numbering depends on the device set).

**The "fixture contains a real identifier" test (PROBE-02), two layers:**
- *Structural (always runs, works in CI, needs no secrets):* walk every string in every `tests/fixtures/**/*.json`; every MAC-shaped token must match `02[0-9a-f]{2}0000[0-9a-f]{4}`; every IPv4 must be in `192.0.2.0/24`; every IPv6 in `2001:db8::/32`; every `ssid` must start with `fixture-ssid-`; every key of `devices_status` and every `id` field must be in the pseudo-id allowlist; no token matching `auth_key`/long base64-ish run (>= 20 chars of `[A-Za-z0-9_-]` that is not a known fw string or label). Also assert the scanner **fails on a planted real-looking value** (a self-test, so the check cannot silently become a no-op).
- *Real-id (runs where `devices.toml` exists, i.e. on the owner's machine):* load ids from the gitignored `devices.toml`, assert none (and none of their 6-hex tails, case-insensitive) occur anywhere in `tests/fixtures/` text. Skipped in CI by `pytest.skip("devices.toml not present")`, with the structural layer carrying CI. **Superseded 2026-10-31 (orchestrator decision):** the real-id layer scans every tracked or committable file (`.planning/`, `docs/`, `src/`, `scripts/`, `tests/`, README, CLAUDE.md included), not only `tests/fixtures/` (plan 01-03).
- Never read the real key in a test (the key must not exist in any test). The app's own `shelog check` command (below) is how the real key is scanned for. **Superseded 2026-10-31 (orchestrator decision):** one owner-machine test (plan 01-03) loads the configured key through the app's own config path, skips when no key is configured (CI), and fails if the key appears in any tracked file, printing only file names and counts, never the key.

### Pattern 6: Probe design (PROBE-01, -03, -04, -05) within 1 req/s
`shelog probe` loads config like `poll`, takes the lock, and runs **steps** through `RateGuard` (>= 1.1 s apart). Raw responses go to `data/probe/<UTC stamp>/<step>.json` (gitignored `data/`); the console prints a summary using **device names from devices.toml, never ids, hosts or keys**; `--save-fixtures DIR` runs the sanitizer and writes the committed fixtures.

| Step | Request | Answers | Always? |
|------|---------|---------|---------|
| 1 `v1_all_status` | `POST /device/all_status` body `auth_key` | PROBE-01 baseline; per-model `_updated` vs `unixtime` table (PROBE-03); asleep state (PROBE-04) | yes |
| 2 `v1_all_status_info` | same + `show_info=true` [CITED: community, forum 16502] | whether `_dev_info` (`code`, `gen`, `online`) appears -> would allow a real model cross-check | yes (1 req) |
| 3 `v2_get_status` | `POST /v2/devices/api/get?auth_key=...`, JSON `{"ids":[...],"select":["status"]}` | v2 envelope for Gen1 sleepers; is `_updated` in `status`? | yes |
| 4 `v2_get_settings` | same with `"select":["status","settings"]` | PROBE-05: do Gen1/Gen3 sleepers return `settings` (sleep period, thresholds, offsets)? | yes |
| 5 `v2_key_in_body` / `v2_key_in_header` | v2 with `auth_key` in JSON body / form body / `Authorization` header | "can v2 take the key outside the URL?" | `--errors` group (3 reqs) |
| 6 `v1_device_settings` | `POST /device/settings` (undocumented), `id`+`auth_key` body | whether any v1 cloud route returns settings | `--errors` group (1 req, 404 expected) |
| 7 `wrong_key_v1`, `wrong_key_v2` | dummy key (not the real one) on the real host | PROBE-04 wrong key envelopes | `--errors` |
| 8 `unknown_id_v2` | v2 with one real id + one syntactically valid non-existent 12-hex id | partial-batch behaviour | `--errors` |
| 9 `wrong_host` | **dummy key only** sent to a sibling `shelly-*.shelly.cloud` host (superseded 2026-10-31: the real key is never sent to any host other than `SHELLY_HOST`) | PROBE-04 wrong-host envelope (the real misconfiguration case) | opt-in `--wrong-host` |
| 10 `burst_429` | **Dropped 2026-10-31: no live burst.** 429 / `max_req` / `Retry-After` handling is covered by synthetic offline fixtures; the real envelope stays unverified | (not captured live) | never |

Total without the opt-in steps: about 14 requests, ~16 s. **Superseded 2026-10-31 (orchestrator decision):** there is no deliberate burst and no exception to 1 req/s; the rate-limit envelope is covered by synthetic fixtures and documented as unverified. The poller must also record every real 429 it ever meets (status, `Retry-After`, coarse category) so the envelope is captured passively too. Each captured error body is sanitized and committed under `tests/fixtures/shelly/errors/`; error bodies do not contain the key. "Unknown device id" is not applicable to v1 `all_status` (no id list); "offline device" cannot be forced, so it is covered by a hand-written `synthetic_*` fixture (device absent from `devices_status`, `{}` block, `devices_status: []`) and clearly named as synthetic.

**Probe result handling:** after the live run, one task writes `docs/shelly-api.md` (PROBE-03/04/05 findings, sanitized) and pins the `classify()` table to the committed error fixtures. This is the checkpoint where the plan has a hard gate (live key). Parsers and sanitizer do **not** have to wait for it: the existing live snapshot (if it still exists) already contains every model, so initial fixtures can be generated before the `probe` command exists; the `probe` run then refreshes/extends them (errors, v2).

### Pattern 7: Config loading and secret hygiene
- `.env` parser (about 20 lines): `KEY=VALUE`, optional `export `, `#` comments, strip matching single/double quotes, no interpolation; file path is `Path(".env")` relative to the CWD, **overridable only through a function parameter used by tests** (D-03: no CLI path). Real environment variables win (`os.environ` checked first).
- Variables (names are discretion; recommend): `SHELLY_AUTH_KEY`, `SHELLY_HOST` (D-04), `SHELOG_DB_PATH` (default `data/shelog.db`), `SHELOG_DEVICES_FILE` (default `devices.toml`), `SHELOG_POLL_INTERVAL_S` (default 60, minimum 30), `SHELOG_LOG_LEVEL` (default INFO). DB filename `shelog.db` matches ARCHITECTURE.md's compose sample and is also covered by `.gitignore` entries `*.db`, `*.db-wal`, `*.db-shm` in addition to `data/` (STACK.md says `shelog.sqlite3`; pick `.db`, the repo's gitignore already anticipates it) [VERIFIED: .gitignore read this session].
- Host normalization: strip `https://`, trailing `/` and any path; **reject an explicit `http://`** (would send the key in clear text); reject characters other than `[a-z0-9.-]` and any `@`, `:`, `/`, `?`, `#` (credential-in-URL / host-confusion); warn (not fail) if it does not end in `.shelly.cloud` (Shelly can relocate hosts).
- Refuse empty key, `CHANGE_ME`, or a key shorter than ~16 chars (placeholder guard; do not print the length).
- `Settings` is a frozen dataclass whose `__repr__`/`__str__` mask the key; the key lives in a `SecretStr`-style wrapper (`__repr__` -> `'***'`) so an accidental `log.info(settings)` or traceback locals cannot leak it.
- `devices.toml` (`[[device]]` tables, as in SPEC §2): required `id, name, group, model`; optional `note, t_offset=0.0, rh_offset=0.0, max_silence_s`. Validate: `id` normalized to lowercase and matches `^[0-9a-f]{6}$|^[0-9a-f]{12}$` (the live snapshot shows both lengths); unique `id` and `name`; `group in {"subfloor","reference"}`; `model in {"ht_gen1","ht_gen3","flood_gen1"}` [VERIFIED: SPEC.md:50-51]; offsets finite floats; **unknown keys are an error** (typo protection); `rh_offset` forbidden for `flood_gen1`. The loader must not crash on, and must not echo, ids in error text beyond the device `name`.
- Redaction: a `logging.Filter` that replaces the key (and its URL-encoded form) in `record.getMessage()`, `exc_text` and args; applied on the root handler. Never log `str(exc)` for transport errors; log `type(exc).__name__` plus the category. `logging.getLogger("httpx"/"httpcore").setLevel(WARNING)` is cheap insurance if `httpx2` is ever introduced.
- Unexpected response bodies (unknown `isok:false` shapes) may be logged at WARNING **truncated to 200 chars and passed through the redactor**, never stored in `poll_log`.

### Anti-Patterns to Avoid
- **Putting the key in a URL** (v2 documents it that way) or formatting `req.full_url`/`e.url` into messages. If v2 is ever used, build the URL inside `transport.send` only and never pass it to a logger.
- **`INSERT OR IGNORE` without counting** (hides wrong `ts`): use `ON CONFLICT DO NOTHING` + `rowcount`.
- **Treating HTTP 200 as success.** `ok=1` only when status 200, JSON parses, `isok is True`, `data.devices_status` is a dict (or empty list) and at least one configured device is present.
- **Treating `_sleeping`/`online`/`cloud.connected` as liveness.** All seven devices show `"_sleeping":true` and one shows `"cloud":{...,"connected":false}` yet delivers fresh data. Liveness = report timestamp only.
- **Using `tmp.value`** (follows the device's configured unit); use `tmp.tC`.
- **Hash-based pseudonyms** of device ids (brute-forceable) and **sorted-id numbering assumptions** across regenerations.
- **Fetching dev-time fixtures through tests that open `.env`**: tests must `monkeypatch.chdir(tmp_path)`.
- **Using `executescript` for migrations without an explicit transaction** (see Pitfall 5).
- **Rejecting "old" `_updated`** relative to now (a dead sensor would be mis-counted as implausible forever).

## Standard Schema: `src/shelog/migrations/0001_init.sql` (executed and validated)

Validated this session on SQLite 3.51.2: applied inside `BEGIN IMMEDIATE ... PRAGMA user_version = 1; COMMIT`, STRICT rejects a TEXT value in a REAL column, `CHECK (ts > 0)` rejects 0, duplicate `ON CONFLICT DO NOTHING` returns `rowcount 0`, NULL `rh` is preserved, and the last-row-per-device query plan is `SEARCH reading USING PRIMARY KEY (device_id=?)`. Uses only features present in SQLite 3.46 (the image). Differences from ARCHITECTURE.md are limited to: `STRICT`, `poll_log` gains `n_unchanged`, `n_rejected`, `retry_after_s`; `CHECK (ts > 0)` and `ok IN (0,1)`, `flood IN (0,1)`; no enum CHECKs (adding a value later would need a table rebuild); `error` is documented as a closed enum with **no `parse:<device>` form** (device ids must not leak through `/api/health` later; per-device parse failures are counted in `n_rejected` and logged by device *name*). Table name stays `reading` (ARCHITECTURE.md/SPEC) even though the roadmap prose says "readings".

```sql
-- 0001_init.sql: complete v1 schema. Applied by db.migrate() inside one transaction;
-- user_version is bumped by the runner, not by this file.

CREATE TABLE poll_log (
  id            INTEGER PRIMARY KEY,
  ts            INTEGER NOT NULL CHECK (ts > 0),      -- attempt start, unix s UTC
  kind          TEXT    NOT NULL DEFAULT 'shelly',    -- 'shelly' | 'weather' (no CHECK: new kinds must not need a table rebuild)
  ok            INTEGER NOT NULL CHECK (ok IN (0, 1)),
  http_status   INTEGER,                              -- NULL when no HTTP response (timeout, DNS, TLS)
  error         TEXT,                                 -- closed category enum from code, NEVER exception text / URL / device id
  n_devices     INTEGER,                              -- configured devices present in the response
  n_new         INTEGER,                              -- reading rows inserted this cycle
  n_unchanged   INTEGER,                              -- report ts == last stored (dedup drop)
  n_rejected    INTEGER,                              -- implausible/regressed ts or unparsable device block
  retry_after_s INTEGER,                              -- from Retry-After on 429, else NULL
  latency_ms    INTEGER
) STRICT;
CREATE INDEX poll_log_kind_ts ON poll_log (kind, ts);

CREATE TABLE reading (
  device_id TEXT    NOT NULL,                         -- normalized lowercase hex id from devices.toml
  ts        INTEGER NOT NULL CHECK (ts > 0),          -- report time, unix s UTC (see ts_source)
  ts_source TEXT    NOT NULL,                         -- 'cloud' (_updated) | 'device' | 'poll'
  t_c       REAL,                                     -- raw, uncalibrated; NULL = no valid reading
  rh        REAL,                                     -- NULL for Flood or invalid
  battery   REAL,                                     -- percent
  flood     INTEGER CHECK (flood IN (0, 1)),          -- Flood only
  polled_at INTEGER NOT NULL,                         -- when WE first saw this report
  poll_id   INTEGER REFERENCES poll_log (id),
  source    TEXT    NOT NULL DEFAULT 'poll',          -- 'poll' | 'cloud_stats' (future backfill)
  PRIMARY KEY (device_id, ts, source)
) WITHOUT ROWID, STRICT;

CREATE TABLE raw_status (
  device_id TEXT    NOT NULL,
  ts        INTEGER NOT NULL,
  source    TEXT    NOT NULL DEFAULT 'poll',
  body      TEXT    NOT NULL,                         -- per-device status JSON exactly as received
  PRIMARY KEY (device_id, ts, source)
) WITHOUT ROWID, STRICT;

CREATE TABLE outdoor (
  ts         INTEGER NOT NULL,
  variable   TEXT    NOT NULL,
  value      REAL,
  interval_s INTEGER NOT NULL DEFAULT 3600,
  fetched_at INTEGER NOT NULL,
  PRIMARY KEY (ts, variable)
) WITHOUT ROWID, STRICT;

CREATE TABLE alert_state (
  key         TEXT    PRIMARY KEY,
  since       INTEGER NOT NULL,
  notified_at INTEGER
) STRICT;

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;
```

**Closed `error` enum (code constant, recommended names):** `auth`, `rate_limited`, `http_5xx`, `http_other`, `timeout`, `network`, `bad_response`, `no_devices`, `endpoint_gone` (404/410 or non-JSON HTML on the v1 route: the deprecation tripwire), `internal` (our own bug, caught at the loop boundary). `NULL` when `ok=1`. A unit test asserts every value matches `^[a-z_0-9]+$` and that `poll_log.error` is never populated from `str(exc)`.

**Connection setup** (STACK.md, validated): `sqlite3.connect(path, isolation_level=None, timeout=5)`, then `PRAGMA journal_mode=WAL` (persistent), `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`. Create the `data/` directory if missing. **Migration runner:** `con.executescript("BEGIN IMMEDIATE;\n" + sql + "\nPRAGMA user_version = N;\nCOMMIT;")` works when `isolation_level=None` (verified: user_version became 1, `in_transaction` False); **on failure the transaction stays open** (verified: `in_transaction` was True after an error), so wrap in `try/except` and issue `ROLLBACK`. Refuse to start if `user_version` is greater than the highest known migration. `raw_status.body` is the per-device block `json.dumps(block, separators=(",", ":"))`.

**Privacy note for later phases:** `raw_status.body` contains SSID, LAN IP and MAC of the owner's network. It must never be exposed by the web service or committed (the DB and backups are gitignored; Phase 2 backups should stay server-side).

### Timestamp semantics (PROBE-03 content; goes into `docs/shelly-api.md`)

| Model | Report-time field | Zone | Evidence | Notes |
|-------|------------------|------|----------|-------|
| H&T Gen1 (fw 1.14) | `_updated` (`"YYYY-MM-DD HH:MM:SS"`) | UTC | `unixtime` 1793462638 == `_updated` `2026-10-31 16:03:58`; difference -1..0 s on the three units with `unixtime > 0` | `time` is local `HH:MM` (CET, UTC+1); ignore |
| H&T Gen1 (fw 1.11, older) | `_updated` | UTC | `unixtime` is 0, `time` is `""` | `getinfo`, `sensor_error` absent |
| Flood Gen1 | `_updated` | UTC | `unixtime` 0, `time` `""` | no `hum` key |
| H&T Gen3 | `_updated` | UTC | `_updated` `2026-10-31 18:21:58` == `sys.unixtime` 1793470918 exactly | `ts` (float, +0.51 s) is cloud receive time; `sys.unixtime` is `null` without NTP sync [CITED: gen2 Sys docs]; `sys.time` local |

All rows `[VERIFIED: scratchpad all_status.json:1]` except the `null`-without-NTP note `[CITED: shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Sys]`. Device wake reasons: Gen1 `act_reasons` documents `battery, button, periodic, poweron, sensor, alarm` [CITED: gen1 docs]; Gen3 `sys.wakeup_reason.cause` documents `button, usb, periodic, status_update, alarm, alarm_test, undefined` [CITED: gen2 Sys docs]. All six Gen1 units showed `["sensor"]` and the Gen3 showed `periodic`: the raw JSON retained per row is therefore enough to later separate threshold-triggered from periodic reports when tuning silence thresholds.

### Reporting settings (PROBE-05): what is obtainable
1. **Gen3 H&T: directly from status.** `sys.wakeup_period` = `7200` (seconds) and `wakeup_reason.cause` are in the polled status, so the cadence for LR-4 is known without any settings call [VERIFIED: all_status.json:1; "Period (in seconds) at which device wakes up" CITED: gen2 Sys docs].
2. **Gen1: not in `status`.** The device-local `/settings` documents `sleep_mode.period` + `unit`, `sensors.temperature_threshold` ("Temperature delta (in °C) which triggers an update"), `sensors.humidity_threshold`, `external_power`, `temperature_offset`, `humidity_offset`, `sensors.temperature_unit` [CITED: shelly-api-docs.shelly.cloud/gen1 H&T /settings]. Flood: `sleep_mode.period` is documented as *not configurable* since v1.10.0 [CITED: gen1 changelog]. The devices sleep, so the local HTTP `/settings` is unreachable; the only cloud route documented is v2 `select:["settings"]` / `pick` [CITED: communication-v2], which the probe tries for all seven (steps 4). Whether it returns anything for **sleeping Gen1** units is unknown (Open Question 3).
3. **Fallback if the cloud returns nothing for Gen1:** model defaults as the documented/known cadence (Gen1 H&T and Flood periodic ~12 h on battery, Gen3 2 h; carried from project research, see Assumption A9) and **observed** cadence: after the hour-long run and the first days of data, compute per-device report-interval statistics (max observed gap) and record them. The owner can also read thresholds in the Shelly app (ROADMAP "Owner action required"); the doc has a table the owner fills in if the API returns nothing.
4. **Where it lands:** `docs/shelly-api.md` (table per device by *name*, with source: `status | cloud_settings | default | observed | app`), plus optional `max_silence_s` per device in `devices.toml` (loaded and validated in Phase 1, used by alerts in Phase 2). Recommended defaults when absent: `ht_gen1` and `flood_gen1` 12 h x 1.25, `ht_gen3` 2 h x 1.25 (the multiplier and use belong to Phase 2). Success Criterion 2 ("each device in devices.toml has a silence threshold") is satisfied by setting `max_silence_s` explicitly in the real `devices.toml`.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| TOML parsing | custom config format | `tomllib` | stdlib, strict, typed |
| `.env` parsing | none needed beyond ~20 lines | tiny parser (no `python-dotenv`) | Behaviour is simple and must be fully controlled (no interpolation, no export to `os.environ` side effects) |
| URL-encoding the form | string concatenation | `urllib.parse.urlencode` | Keys may contain `+`, `/`, `=` |
| Interruptible sleep | `time.sleep` + flags | `threading.Event.wait(timeout)` | SIGTERM-clean; trivially fakeable |
| Single-instance lock | PID files | `fcntl.flock(LOCK_EX|LOCK_NB)` | Auto-released on crash; verified `BlockingIOError` on second holder |
| Schema versioning | migration framework | `PRAGMA user_version` + ordered SQL files | Per STACK.md; verified runner |
| Time parsing | `strptime` + naive datetimes | `datetime.fromisoformat(f"{s}+00:00")` | DTZ-clean, explicit UTC |
| Dedup uniqueness | app-side seen-sets alone | PK `(device_id, ts, source)` + `ON CONFLICT DO NOTHING` + counted | DB as the last line of defense |
| Pseudonym generation | hashing ids | sequential counters (device-centric) | 24-bit ids are brute-forceable |
| Retry/backoff | external library | ~15-line pure `next_delay()` | Needs bespoke Shelly rules (Retry-After, auth); trivially table-tested |

**Key insight:** every piece of logic that decides something (parse, plausibility, dedup, classify, backoff, sanitize) is a pure function over plain data. Only `transport.send`, the SQL wrappers, and the clock are impure, so the whole phase is testable without network, Docker, or the real key.

## Common Pitfalls

### Pitfall 1: Wrong or missing timestamp makes every insert collide or vanish
**What goes wrong:** `unixtime` is 0 on two Gen1 units; using it as `ts` collides on the PK and `INSERT OR IGNORE` silently discards everything after the first row.
**Why it happens:** Copying SPEC §3's `unixtime`/`sys.unixtime` guess instead of the verified `_updated`.
**How to avoid:** `_updated` for all models; `CHECK (ts > 0)`; `plausible_ts`; count `unchanged`; the "polled OK N times, never inserted" WARNING.
**Warning signs:** `n_new` stays 0 for a device while `n_unchanged` grows and `_updated` visibly changes in the raw probe output.

### Pitfall 2: Auth key leakage via exception text, repr, URL, or DB
**What goes wrong:** the key shows up in a traceback (`settings` object in locals/repr), an `httpx`-style error with the URL, a `poll_log.error` string, or the stored raw body.
**How to avoid:** key only in the form body; `SecretStr`-style wrapper; redaction filter; transport raises `TransportError(category)` with `from None`; `poll_log.error` is an enum; the raw envelope for failures is never stored. **Tests:** (a) a sentinel key (e.g. `"SENTINEL-KEY-0123456789"`) runs through every outcome with a fake transport that *echoes the body into its exceptions*, then asserts the sentinel is absent from `caplog`, captured stdout/stderr, the raw bytes of the DB **and `-wal`/`-shm` files**; (b) real-socket adapter test (stdlib `http.server`) asserting the key is only in the request body and never in `str(exception)`; (c) `shelog check` for the live key (below).
**Warning signs:** any `%r` of `Settings`, any `str(exc)` in a log call (ruff can't catch this; add a grep-style test over `src/` forbidding `str(e)`/`{e}` in log calls in the `shelly` package).

### Pitfall 3: Treating `isok:false` / 4xx bodies as success, and 401 as always "auth"
**What goes wrong:** a rate-limited request can come back as HTTP 200 with `{"isok":false,"errors":{"max_req":...}}` [CITED: forum 22549 reports a `max_req` / "Request limit reached!" error], HTTP 429 [CITED: third-party client docs "will 429 bursts"], or HTTP 401 [CITED: forum 12387, anecdotal: "second request in a quick sequence gets a 401"]. An immediate "auth rejected" alert on a 401 would then false-positive.
**How to avoid:** classifier is a table pinned to probe fixtures; `auth` is declared only after **two consecutive** auth-shaped failures >= one interval apart (cheap and makes a rate-limit-as-401 harmless); unknown shapes -> `bad_response` with a truncated, redacted WARNING log. Never assume a body shape not in a fixture.

### Pitfall 4: Cadence assumptions leak into Phase 1 behaviour
**What goes wrong:** treating an unchanged device as "dead" or logging a gap after minutes. Gen1 H&T/Flood report about every 12 h on battery, Gen3 every 2 h, otherwise only when a 0.5 °C / 0.5 %RH threshold is crossed [CITED: PITFALLS.md citing Gen1 docs; D-11].
**How to avoid:** Phase 1 only *records*; no silence logic. The unchanged count per poll is normal (most polls insert 0-2 rows). Expect the first hour to insert 7 rows (initial states) plus a few more.

### Pitfall 5: Migration and transaction handling in sqlite3
**What goes wrong:** (a) `executescript` implicitly COMMITs any pending transaction (legacy mode) and leaves an open transaction on error; (b) forgetting `foreign_keys=ON` per connection; (c) `PRAGMA journal_mode=WAL` inside a transaction is a no-op; (d) one connection per thread assumption.
**How to avoid:** `isolation_level=None`, explicit `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK`, run the pragmas outside transactions, one connection used by the single thread. The runner and the per-cycle writer are both verified patterns above.

### Pitfall 6: Python-version drift between host (3.14), uv-managed (3.13.15) and image (3.13.18)
**What goes wrong:** `uv init` on this host writes `requires-python = ">=3.14"` and `.python-version` `3.14` (verified), and this host's uv is set to `python-downloads = manual`, so `uv sync` fails with "No interpreter found for Python 3.13" until `uv python install 3.13` is run (verified). The bundled SQLite also differs: host system 3.51.2, uv 3.13.15 managed build 3.53.1, image 3.46.1.
**How to avoid:** commit `.python-version` = `3.13` and `requires-python = ">=3.13"` (no upper cap; uv discourages caps and the pin file already selects 3.13), document `uv python install 3.13` once, write SQL that is valid on 3.46, and let CI use the same pin. Do not use SQLite features newer than 3.46 (e.g. avoid anything added in 3.47+).

### Pitfall 7: ruff `S310`, `ResourceWarning`, and test hygiene with urllib
**What goes wrong:** `S310` flags `urlopen` of non-literal URLs; unclosed `HTTPError` raises `ResourceWarning` (pytest can turn it into a failure with `filterwarnings = error`).
**How to avoid:** `# noqa: S310` with a comment pointing to the https-only validation in config; `e.close()` in `finally` (shown above); `filterwarnings = ["error"]` in pytest config to keep the suite honest.

### Pitfall 8: Hook-blocked commands
**What goes wrong:** the secret-read guard blocks **any Bash command containing the text `.env`** (observed in this research: a `grep` over settings that merely mentioned the pattern was denied). `.env.example` is explicitly excepted by the hook message.
**How to avoid:** plan verification commands must not contain `.env`; use `.env.example` freely; verify `.gitignore` coverage by reading `.gitignore` with the Read tool, not `git check-ignore .env`. Live commands are only `uv run shelog probe|poll|check` (plans run the same CLI through `scripts/live.sh`, which uses the main checkout root as working directory; see plan 01-01).

### Pitfall 9: Real identifiers already exist in committed planning docs
`.planning/phases/01-probe-core-collector/01-CONTEXT.md` (D-07, D-10) is committed and contains the real device ids, and the repo is meant to become public in Phase 2. Device ids are not auth secrets, but D-02 treats them as non-public. Decide before the first push: scrub the ids in those docs (replace by names) and rewrite history for that file, or keep `.planning/` out of the pushed branch. The structural fixture test would not catch this (it scans only `tests/fixtures`); add the real-id scan to also cover `README`/`docs`/`tests` (not `.planning`), and flag this to the owner. **Resolved 2026-10-31:** the owner had history rewritten before any push; no commit contains a real device id (verified by scan); the real-id scan now covers every tracked file, `.planning/` included (plan 01-03).

### Pitfall 10: Uncapped response reads and hostile values
`resp.read()` without a cap lets a misbehaving endpoint exhaust memory; JSON parsed by `json.loads` accepts `NaN`/`Infinity`. Cap at 1 MiB, parse with `parse_constant` rejecting NaN/Infinity (or check `math.isfinite` after), and validate every field's type (including `bool` vs `int`).

## Code Examples

### Rate guard (clock-injected)
```python
# Source: design; behaviour covered by a fake-clock test asserting min gap >= 1.1 s
class RateGuard:
    def __init__(self, clock: Clock, min_gap: float = 1.1):
        self._clock, self._min_gap, self._last = clock, min_gap, None
    def acquire(self, stop: threading.Event) -> bool:
        if self._last is not None:
            remaining = self._last + self._min_gap - self._clock.monotonic()
            if remaining > 0 and self._clock.wait(stop, remaining):
                return False                       # stopping
        self._last = self._clock.monotonic()
        return True
```

### Cycle outcome classification (shape of the table; pin to fixtures after the probe)
```python
# Source: design; the specific 401/429/isok shapes are [ASSUMED] until pinned to probe fixtures
def classify(raw: RawResponse) -> Outcome:
    if raw.status in (404, 410) or looks_like_html(raw.body):   return Outcome("endpoint_gone", raw.status)
    if raw.status == 429:                                        return Outcome("rate_limited", 429, retry_after=parse_retry_after(raw.headers))
    if raw.status in (401, 403):                                 return Outcome("auth", raw.status)      # needs 2-in-a-row confirmation
    if 500 <= raw.status < 600:                                  return Outcome("http_5xx", raw.status)
    if raw.status != 200:                                        return Outcome("http_other", raw.status)
    doc = safe_json(raw.body)                                    # None on invalid / non-dict
    if doc is None:                                              return Outcome("bad_response", 200)
    if doc.get("isok") is not True:                              # inspect errors keys only, never store text
        return Outcome("rate_limited" if "max_req" in (doc.get("errors") or {}) else "bad_response", 200)
    ds = (doc.get("data") or {}).get("devices_status")
    if ds == [] or ds == {}:                                     return Outcome("no_devices", 200)
    if not isinstance(ds, dict):                                 return Outcome("bad_response", 200)
    return Outcome("ok", 200, devices=ds)
```
`Retry-After` may be delta-seconds or an HTTP-date [CITED: RFC 9110]; `parse_retry_after` handles both and clamps to [1, 900].

### `shelog check` (leak scan without reading `.env`)
```python
# Source: design. Loads the key through the normal config path and prints ONLY booleans/counts.
def check(settings, db_path, extra_dirs=("data/probe",)):
    needles = {settings.key.reveal(), urllib.parse.quote(settings.key.reveal(), safe=""),
               urllib.parse.quote_plus(settings.key.reveal())}
    targets = [db_path, f"{db_path}-wal", f"{db_path}-shm", *iter_files(extra_dirs), *log_files()]
    hits = sum(1 for p in targets if any(n.encode() in pathlib.Path(p).read_bytes() for n in needles))
    print(f"config: ok   db schema: {user_version}   leak scan: {len(targets)} files, {hits} hits")
    return 0 if hits == 0 else 1
```
Note: raw probe output under `data/probe/` is *expected* to be clean of the key (Shelly does not echo it); a hit there is a real finding. `shelog check` is what lets the owner (or Claude) verify Success Criterion 4's "a search ... finds no trace of the auth key" against the **real** key without ever reading `.env`. Run poll output captured to a file (`uv run shelog poll 2>&1 | tee data/poll.log`) so the scan covers logs too.

### Per-cycle write (one transaction)
```python
# Source: design; SQL verified against 0001_init.sql
def write_cycle(con, poll: PollRecord, inserts: list[Insert]) -> None:
    con.execute("BEGIN IMMEDIATE")
    try:
        poll_id = con.execute(
            "INSERT INTO poll_log(ts,kind,ok,http_status,error,n_devices,n_new,n_unchanged,n_rejected,retry_after_s,latency_ms)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)", poll.row()).lastrowid
        for i in inserts:
            cur = con.execute(
                "INSERT INTO reading(device_id,ts,ts_source,t_c,rh,battery,flood,polled_at,poll_id) VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT DO NOTHING", (*i.reading_row(), poll.ts, poll_id))
            if cur.rowcount == 1:
                con.execute("INSERT INTO raw_status(device_id,ts,body) VALUES(?,?,?) ON CONFLICT DO NOTHING", i.raw_row())
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK"); raise
```
(`n_new` must be computed from actual `rowcount`s, so the poll_log row is updated at the end of the same transaction, or the inserts are done first and `poll_log` last with `poll_id` back-filled; keep it in one transaction either way.)

## Testing Strategy (no network)

`workflow.nyquist_validation` is `false` in `.planning/config.json` [VERIFIED: file read this session], so the formal Validation Architecture section is omitted; the testing approach the planner needs is below.

| Concern | Technique |
|---------|-----------|
| Parsers | Table tests: each sanitized fixture block -> expected `Report`; synthetic fixtures: `sensor_error=1`, `tmp.is_valid=false`, `tmp: null`, missing `hum` on Flood, `bat: null`, bool where number expected, `NaN`, ts zero/negative/future/pre-2020, missing `_updated` |
| `decide()` / `plausible_ts` | Pure boundary tests (equal, +1, -1, `now+120`, `now+121`, 2020 boundary) |
| Client + classifier | Fake `send` returning scripted `RawResponse`/raising `TransportError`; one fixture per observed envelope; assert categories and `retry_after` |
| urllib adapter | One real-socket test using stdlib `http.server` on `127.0.0.1:0` in a thread: body-only key, 429 + `Retry-After`, 307 not followed, connection refused -> `network`, slow server -> `timeout`, oversize body truncated |
| Loop / backoff | `FakeClock` whose `wait()` advances `monotonic()`/`time()` and returns `stop.is_set()`; scripted outcome sequences; assert delay sequence (60/120/240/300 cap with fixed-seed `random.Random`), Retry-After honoured, auth floor 300 s, **min gap between sends >= 1.1 s over the whole run**, clean stop on `stop.set()` mid-wait |
| DB | `tmp_path` SQLite via real `connect_rw` + `migrate`; assert `user_version`, STRICT type rejection, dedup with replayed envelopes (same `_updated` -> 0 new), advance -> 1 new, regression -> `n_rejected`, NULL preserved, `raw_status` row count == reading count, `poll_log` row for every failure category |
| Secret leakage | Sentinel-key test (Pitfall 2) over every outcome incl. exception-echoing fake transport; scan DB + WAL bytes |
| Sanitizer | Round-trip: raw synthetic "real-looking" doc -> sanitize -> scanner reports zero; scanner flags a planted real-looking MAC/IP/SSID (self-test); determinism (two runs equal) |
| Fixture safety | Structural layer always; real-id layer when `devices.toml` exists |
| Config | `tmp_path` + `monkeypatch.chdir`; host normalization table (`https://h/`, `h`, `HTTP://h` -> error, `h@evil`, `h/path`), placeholder refusal, env-over-file precedence, unknown keys, duplicate ids, `Settings` repr masks key |
| Time | Tests never call real `time`; `Clock` injected; ruff `DTZ` rules on |
| Commands | `uv run pytest -x -q` (< 10 s expected); ruff `uv run ruff check . && uv run ruff format --check .` |

**Wave 0 gaps (all new, repo is greenfield):** `pyproject.toml`, `.python-version`, pytest + ruff config, `tests/conftest.py` (FakeClock, tmp DB fixture, fake transport), fixture directories, `devices.example.toml`, `.env.example` (writable through normal tools; the hook excepts it).

## Security Domain

`security_enforcement` is enabled (absent/true), ASVS level 1.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | partial | The only credential is the Shelly cloud auth key; held in env, wrapped, never logged; rejected-key state is explicit |
| V3 Session Management | no | No sessions (Phase 4/5 web is read-only, public) |
| V4 Access Control | partial | Key never reaches the future `web` service; DB file created with default (non-world-writable) permissions; document `0600` for `.env` on the server |
| V5 Input Validation | yes | Strict validation of env, host, `devices.toml`; type/finite checks on all Shelly JSON; response size cap; parameterized SQL only |
| V6 Cryptography | yes | HTTPS with default certificate verification only; **never** pass an unverified `ssl` context; reject `http://` hosts |
| V7 Error Handling & Logging | yes | Enum-only errors in DB, redaction filter, no `str(exc)` of transport errors, UTC logs |
| V8 Data Protection | yes | `raw_status` holds SSID/IP/MAC: never exposed; gitignored DB; sanitized fixtures only |
| V14 Configuration | yes | Placeholder guard, unknown-key rejection, pinned deps via `uv.lock` |

### Known Threat Patterns for this stack
| Pattern | STRICT category | Standard Mitigation |
|---------|-----------------|---------------------|
| Auth key leaked via logs/tracebacks/URLs | Information disclosure | Body-only transport, redaction filter, secret wrapper, leak-scan tests + `shelog check` |
| Key sent to an attacker-controlled host via a crafted `SHELLY_HOST` | Information disclosure | Host normalization/validation (charset, no `@:/?#`), https only, no redirects |
| Redirect follows with key-bearing POST | Information disclosure | `NoRedirect` handler |
| Malicious/oversized or malformed JSON from the endpoint | DoS / Tampering | 1 MiB cap, type checks, reject NaN/Inf, per-device try/except so one bad block never aborts the cycle |
| SQL injection via device ids/names | Tampering | Parameterized queries only; ids validated by regex at load |
| Real identifiers committed in fixtures/docs | Information disclosure | Deterministic sanitizer + structural fixture test; Pitfall 9 for planning docs |
| Hot restart loop burns the account rate limit | DoS (self) | flock, startup failure sleep, backoff caps |
| Poller concurrency (probe + poll, two instances) | DoS (self) | flock shared by `probe` and `poll` |

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| v1 Cloud Control API `/device/status` | v2 `/v2/devices/api/get` (batched, up to 10 ids) | v1 page: "This API is deprecated and will be removed in the near future" | Plan for v1 disappearing: `endpoint_gone` category, v2 behind the same interface if the probe proves it has `_updated` for Gen1 [CITED: shelly-api-docs.shelly.cloud/cloud-control-api/communication] |
| `httpx` | `httpx2` (Pydantic-maintained fork) | first release 2026-05-11 | Not needed in Phase 1; stdlib suffices |
| `datetime.utcfromtimestamp` | timezone-aware (`fromtimestamp(ts, UTC)`) | Deprecated in 3.12 (I observed the DeprecationWarning on the host) | Use aware datetimes; ruff DTZ enforces |

**Deprecated/outdated:** SPEC.md's `unixtime`/`sys.unixtime` as the primary timestamp (superseded by verified `_updated`); SPEC.md's `shelly-climate-log` CLI name (superseded by `shelog`, D-14); CLAUDE.md "Python >= 3.11" and `httpx` (superseded by D-14/D-15).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | A wrong auth key returns a recognizable auth failure (HTTP 401/403 or `isok:false` with an auth-ish error); exact envelope unknown | Pattern 3/Pitfall 3, `classify()` | Wrong key mis-labelled as `bad_response`; mitigated: probe captures it and the table is pinned to the fixture |
| A2 | Rate-limit response is one of HTTP 429 (+ optional `Retry-After`), HTTP 401, or HTTP 200 `isok:false` with `errors.max_req`; sources conflict | Pitfall 3 | Poller could misclassify a throttle as an auth failure; mitigated by 2-in-a-row confirmation |
| A3 | v2 `status` for Gen1 sleeping devices includes `_updated` | Summary / Probe step 3 | If absent, v2 is unusable for Gen1 timestamps (no impact on the v1 primary) |
| A4 | v2 may accept `auth_key` in body/header | Probe step 5 | None for v1 primary; if not, a v2 fallback would carry the key in the URL (redaction is the only control) |
| A5 | v2 `select:["settings"]` returns settings for sleeping Gen1/Gen3 devices | PROBE-05 | Fallback (defaults + observed cadence + Shelly app) already planned |
| A6 | `show_info=true` on v1 `all_status` adds `_dev_info` (`code`, `gen`, `online`) | Probe step 2 | Only a nice-to-have model cross-check; omit if absent |
| A7 | Empty `devices_status` may arrive as `[]` (PHP-style) rather than `{}` | `classify()` | Handled both ways; if never occurs, harmless |
| A8 | Default urllib `User-Agent` stays accepted (verified once in the owner's discovery run) | Transport | If blocked later, set a UA; low risk |
| A9 | Gen1 H&T / Flood periodic wake default 12 h on battery (10 min on external power), Gen3 2 h; carried from project research (PITFALLS.md citing Gen1 docs/Allterco KB); Gen3 `wakeup_period` 7200 itself is VERIFIED | Reporting settings | Silence thresholds in Phase 2 off; mitigated by observed-interval tuning |
| A10 | `python:3.13-slim` provides system CA certificates for urllib TLS | Phase 2 deploy | Poller cannot reach Shelly in the container; trivial to detect in Phase 2 smoke test (STACK.md reports TLS smoke-tested with httpx2, which uses truststore) |
| A11 | Plausibility constants: floor 2020-01-01, future skew 120 s, absurd T/RH ranges | Pattern 3/2 | Too tight rejects real data (counted, visible); too loose admits junk |
| A12 | `sensor_error` non-zero means the reading is invalid | Pattern 2 | Rare; raw JSON retained so reparse can fix |
| A13 | The three dev packages are the intended publishers (seam flagged SUS due to missing download data) | Package audit | Supply-chain risk; one bundled human-verify checkpoint |
| A14 | Gen1 6-hex and 12-hex id forms (both seen in the live snapshot) are the only forms; the cloud returns lowercase hex | Config/sanitizer | Config regex rejects a valid device; trivial to widen |
| A15 | Sending the real key to another Shelly regional host (probe step 9) is acceptable | Probe | **Superseded 2026-10-31:** not acceptable; the wrong-host step sends a dummy key only |

## Open Questions (RESOLVED)

1. **What do wrong key, wrong host, unknown id, and rate limit actually return?**
   - What we know: nothing authoritative. v2 docs list no error codes and no 429 semantics; v1 docs list none; community: `max_req`/"Request limit reached!", 429 on bursts, 401 on rapid second request.
   - What's unclear: HTTP status vs body for each.
   - Recommendation: run the probe `--errors` (+ opt-in `--wrong-host`, `--provoke-429`); commit sanitized envelopes; pin `classify()` to them. Until then ship the conservative table above. **RESOLVED:** conservative default in force: v1 all_status primary, classifier with synthetic fixtures, Gen1 thresholds from documented defaults; superseded by 01-07 live evidence recorded in docs/shelly-api.md. The live burst (`--provoke-429`) is dropped: the real rate-limit envelope stays "unverified, synthetic" (orchestrator decision 2026-10-31).
2. **Does v2 return `_updated` (or any reliable report time) for Gen1 sleepers, and can it take the key outside the URL?**
   - What we know: docs show only a Gen2 example (`sys.unixtime`), key in query.
   - Recommendation: probe steps 3 and 5; if `_updated` is absent, record "v2 unsuitable as fallback" in `docs/shelly-api.md` and build no v2 path. **RESOLVED:** conservative default in force: v1 all_status primary, classifier with synthetic fixtures, Gen1 thresholds from documented defaults; superseded by 01-07 live evidence recorded in docs/shelly-api.md (no v2 code path unless that evidence requires one).
3. **Can settings for sleeping Gen1 units be fetched through the cloud at all?**
   - Recommendation: probe step 4/6; otherwise defaults + observed intervals + owner reads thresholds in the app. **RESOLVED:** conservative default in force: v1 all_status primary, classifier with synthetic fixtures, Gen1 thresholds from documented defaults (12 h x 1.25, source `default`, tuned from observed inter-report gaps in Phase 2; no owner checkpoint); superseded by 01-07 live evidence recorded in docs/shelly-api.md.
4. **Real identifiers already committed in `.planning/` (Pitfall 9).** Owner decision: scrub/rewrite or keep `.planning/` out of the public push. Not blocking Phase 1; blocking before Phase 2's first push. **RESOLVED 2026-10-31:** owner had history rewritten before any push; no commit contains real device ids (verified by scan); CLAUDE.md forbids real identifiers in committed files.
5. **How should `shelog poll` behave on the owner's machine while Bombur polls in production (Phase 2+)?**
   - Same account => shared 1 req/s budget; cannot be enforced across hosts. Document `poll --once` for dev; the probe takes the local lock only. **RESOLVED (decision):** recommendation adopted: a local `shelog poll` stops once production polls the same account, `shelog poll --once` is used for dev checks, and the probe takes the local lock only (documented in docs/shelly-api.md by plan 01-07).
6. **Does `shelog probe` replace or complement a manual scratch `all_status.json`?** The snapshot may vanish; recommendation: the first plan creates fixtures from the snapshot **if present** (cheap, unblocks parser work), and the probe plan regenerates/extends them regardless. **RESOLVED (decision):** recommendation adopted in its robust form: `shelog probe` (plan 01-01) regenerates the capture on demand, plan 01-03 builds the first fixture from a probe capture, and plans 01-06/01-07 refresh and extend the fixtures; the scratch snapshot is not needed.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| uv | env/lock/build | yes | 0.12.19 (`/usr/sbin/uv`, python-downloads=manual) | — |
| Python 3.13 | D-15 pin | **was missing; installed during this research via `uv python install 3.13`** | 3.13.15 (uv-managed, user-level) | `UV_PYTHON_DOWNLOADS=automatic` |
| Python 3.14 (host) | not for the project | yes | 3.14.8 | — |
| SQLite | storage | yes (bundled) | 3.51.2 host / 3.53.1 uv-3.13 / 3.46.1 image | — |
| Network to PyPI / Shelly | install, live probe | PyPI yes; Shelly cloud: not called by research | — | Fixtures for all tests |
| git | commits | yes | — | — |
| Docker | Phase 2 only | present (`/usr/sbin/docker`) | not needed in Phase 1 | — |
| Shelly credentials (`.env`, real `devices.toml`) | probe, hour run | present on disk (not read by research) | — | Hard gate for live steps only |

**Missing dependencies with no fallback:** none for planning/coding. The live probe and the one-hour run require the owner's real key/host and a network path to Shelly Cloud.
**Missing dependencies with fallback:** Python 3.13 (now installed here; on another machine `uv python install 3.13` or allow automatic downloads).

## Project Constraints (from CLAUDE.md)

From `./CLAUDE.md` and `./.claude/CLAUDE.md` (the latter holds the generated GSD blocks):
- **Never read `.env`** in any form; a hook blocks it. Code loads secrets at runtime; for live commands the owner runs them (or the allowed `uv run shelog ...` commands run), and the key must never print.
- `.env` and `devices.toml` are gitignored; only examples are committed.
- Python >= 3.11 (superseded to 3.13 by D-14/D-15), `uv`, stdlib `sqlite3` + `tomllib`, `ruff`, `pytest`; keep dependencies minimal; no heavyweight stacks.
- Two processes sharing a WAL SQLite DB: poller (sole writer) and read-only web; one batched request per interval; honour 429; never exceed 1 req/s; log every attempt to `poll_log`.
- Store **raw, uncalibrated** values; calibration and derived metrics at read time; dedup on insert with `ts_source` recording provenance (`device|cloud|poll`).
- Sample-and-hold data semantics (relevant to later phases; Phase 1 must not interpolate or coalesce).
- Surface auth failures loudly.
- Development order: probe first, commit **sanitized** real responses as fixtures, derive parsers from them, then poller + DB, deploy early.
- Project instruction: make file changes through a GSD workflow (`/gsd-execute-phase` etc.).
- The `.planning/config.json` `claude_md_path` is `./.claude/CLAUDE.md`.

## Suggested Plan Decomposition (non-binding input for the planner)

1. **Scaffold + config + DB:** `uv init` with the 3.13 pin, ruff/pytest config, `config.py` (.env parser, host normalization, devices.toml validation, secret wrapper), `db.py` + `0001_init.sql`, `devices.example.toml`, `.env.example`. Includes the bundled dev-package `checkpoint:human-verify`.
2. **Sanitizer + fixtures + fixture-safety tests:** `sanitize.py`, scanner, fixtures from the snapshot if available (else wait for step 4), structural and real-id tests.
3. **Client + parsers + decide + store (pure and SQL, fully offline-tested):** transport, `RateGuard`, `classify`, `parse_*`, `decide`, `write_cycle`, leak tests.
4. **Poller loop + `poll`/`check` commands:** `next_delay`, `step`, `run`, flock, SIGTERM, FakeClock tests.
5. **Probe command + live run (human gate) + findings doc:** `probe`, the `--errors` group, `docs/shelly-api.md`, pin `classify()` to the captured fixtures, add `max_silence_s` to the real `devices.toml`.
6. **One-hour live run (human gate, end-of-phase):** `uv run shelog poll 2>&1 | tee data/poll.log` for >= 60 min, then SQL checks (rows per device, no duplicate `(device_id, ts)`, max one request/second from `poll_log` timestamps, `ts` plausible, NULLs intact) and `uv run shelog check`.

## Sources

### Primary (HIGH confidence)
- Live snapshot `scratchpad/all_status.json` (read with the Read tool this session): envelope, all field paths, `_updated` vs `unixtime`, Gen3 `sys.wakeup_period`
- Executed prototypes this session (scratchpad `proto/`): urllib behaviour against a local server, DDL and migration runner on SQLite 3.51.2, `fromisoformat` UTC parse, `flock`, sanitizer + structural scanner on the live snapshot
- `uv` experiments: `uv init`, `uv python install 3.13`, `uv sync` with `uv_build>=0.13.6,<0.14`, pytest/ruff
- PyPI JSON API (httpx2, httpx, pytest, ruff, uv_build, uv versions and dates); Docker Hub tag API (`python:3.13-slim`)
- `/home/ciakval/GIT/shelog/{SPEC.md, CLAUDE.md, .claude/CLAUDE.md, .gitignore, .planning/**}` read this session
- https://shelly-api-docs.shelly.cloud/cloud-control-api/communication-v2 (v2 get: query-string key, ids 1-10, `select`/`pick`, 1 req/s, no error codes)
- https://shelly-api-docs.shelly.cloud/cloud-control-api/communication (v1: key in POST body, deprecation text; `all_status` and `/device/settings` not documented)
- https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Sys and .../Temperature (wakeup fields, `unixtime` null without NTP, `tC` null on invalid)
- https://shelly-api-docs.shelly.cloud/gen1/ (H&T `/settings` fields, `act_reasons` values, Flood changelog on `sleep_mode.period`)

### Secondary (MEDIUM confidence)
- https://shelly-forum.com/thread/16502 (`device/all_status` with `show_info=true`, shared-device behaviour)
- https://shelly-forum.com/thread/22549 (`max_req` "Request limit reached!" report) and https://shelly-forum.com/thread/12387 (401 under rapid requests, anecdotal)
- https://hex.pm/packages/shelly/0.3.0 (third-party client: "will 429 concurrent bursts", ~1 req/s, v2 accepts the same auth key, ids normalisation, `isok:false` with HTTP 200)

### Tertiary (LOW confidence)
- Project-level research files (STACK/ARCHITECTURE/PITFALLS/SUMMARY) for cadence defaults and conventions; their Shelly shape statements are superseded by the live snapshot where they differ

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH, versions from registries, `uv`/Python behaviour exercised on this host; the urllib-instead-of-httpx2 call is a recommendation consistent with D-14.
- Architecture: HIGH for parse/dedup/schema/loop (executed prototypes and verified fixtures); MEDIUM for the classifier table until the probe pins it.
- Pitfalls: HIGH for items reproduced locally (WAL/transaction/urllib/uv), MEDIUM for Shelly-side error behaviour.

**Research date:** 2026-10-31
**Valid until:** 2026-11-30 for stack/versions (httpx2, ruff and pytest release often); Shelly API notes valid until the probe replaces them.
