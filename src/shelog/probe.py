"""`shelog probe`: live API checks with a raw dump on disk and a name-only summary.

Raw response bodies contain MACs, SSIDs, IPs and real device ids, so they go only to the
gitignored data/probe/ directory. The index file and the console summary carry device
NAMES only: no ids, host, key or URL.

Every request goes through ShellyClient.request, which always waits on the shared RateGuard
(account limit 1 req/s). The probe has no unguarded, concurrent or rate-limit-testing mode.
The real key is sent only to the configured host: wrong-key and wrong-host steps carry
DUMMY_KEY and never read the real one.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote, quote_plus

from shelog.clock import Clock, SystemClock
from shelog.config import Device, Secret, Settings
from shelog.lock import AlreadyRunning, acquire_instance_lock
from shelog.sanitize import SanitizeError, sanitize_docs, scan
from shelog.shelly import transport
from shelog.shelly.client import Outcome, RateGuard, ShellyClient, classify
from shelog.shelly.transport import RawResponse, TransportError

ANCHOR_KEYS = {"ht_gen1": "tmp", "ht_gen3": "temperature:0", "flood_gen1": "flood"}

# The only key any wrong-key or wrong-host step sends. It is not a valid Shelly key.
DUMMY_KEY = "probe-invalid-key-000000000000000000"
# A well-formed id no real device can own: its first octet has the multicast bit set. It sorts
# after every real id and lies outside the sanitizer's pseudo space.
UNKNOWN_ID = "fffffffffffe"

GROUP_DEFAULT = "default"
GROUP_ERRORS = "errors"
GROUP_WRONG_HOST = "wrong_host"

V2_PATH = "/v2/devices/api/get"
V2_BATCH_MAX = 10  # documented limit of ids per v2 call

_SIBLING_RE = re.compile(r"shelly-(\d{2})-([a-z0-9]+)\.shelly\.cloud")

# Reporting settings the summary looks for in a v2 `settings` block (PROBE-05).
SETTINGS_PATHS: tuple[tuple[str, ...], ...] = (
    ("sleep_mode", "period"),
    ("sleep_mode", "unit"),
    ("sensors", "temperature_threshold"),
    ("sensors", "humidity_threshold"),
    ("temperature_offset",),
    ("humidity_offset",),
    ("external_power",),
)
WAKEUP_PATH = ("sys", "wakeup_period")


def sibling_host(host: str) -> str | None:
    """Neighbouring regional host (NN+1, or NN-1 at 99) for `shelly-NN-<region>.shelly.cloud`."""
    match = _SIBLING_RE.fullmatch(host.lower())
    if match is None:
        return None
    number = int(match.group(1))
    neighbour = number + 1 if number + 1 <= 99 else number - 1
    return f"shelly-{neighbour:02d}-{match.group(2)}.shelly.cloud"


@dataclass(frozen=True)
class StepResult:
    raw: RawResponse | None
    outcome: Outcome


@dataclass(frozen=True)
class StepContext:
    client: ShellyClient
    clock: Clock
    settings: Settings
    devices: tuple[Device, ...]


@dataclass(frozen=True)
class ProbeStep:
    name: str
    run: Callable[[StepContext], StepResult]
    group: str = GROUP_DEFAULT
    key_placement: str = "form"  # form/json/query/header/none/dummy


def _latency_ms(clock: Clock, started: float) -> int:
    return round((clock.monotonic() - started) * 1000)


def _call(
    ctx: StepContext,
    path: str,
    *,
    client: ShellyClient | None = None,
    form: Mapping[str, str] | None = None,
    json_body: Any = None,
    query: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> StepResult:
    """One guarded request, classified. Transport failures become a category, never text."""
    started = ctx.clock.monotonic()
    try:
        raw = (client if client is not None else ctx.client).request(
            path, form=form, json_body=json_body, query=query, headers=headers
        )
    except TransportError as err:
        return StepResult(
            None, Outcome(err.category, None, latency_ms=_latency_ms(ctx.clock, started))
        )
    outcome = classify(raw, ctx.clock.time())
    return StepResult(raw, replace(outcome, latency_ms=_latency_ms(ctx.clock, started)))


def _real_key(ctx: StepContext) -> str:
    return ctx.client.key.reveal()


def _ids(ctx: StepContext) -> list[str]:
    return [d.id for d in ctx.devices][:V2_BATCH_MAX]


def _v2_body(ctx: StepContext, select: Sequence[str], **extra: Any) -> dict[str, Any]:
    return {"ids": _ids(ctx), "select": list(select), **extra}


def _step_v1_all_status(ctx: StepContext) -> StepResult:
    return _call(ctx, "/device/all_status", form={"auth_key": _real_key(ctx)})


def _step_v1_all_status_info(ctx: StepContext) -> StepResult:
    return _call(
        ctx,
        "/device/all_status",
        form={"auth_key": _real_key(ctx)},
        query={"show_info": "true", "no_shared": "true"},
    )


def _step_v2_get_status(ctx: StepContext) -> StepResult:
    return _call(
        ctx, V2_PATH, query={"auth_key": _real_key(ctx)}, json_body=_v2_body(ctx, ["status"])
    )


def _step_v2_get_settings(ctx: StepContext) -> StepResult:
    return _call(
        ctx,
        V2_PATH,
        query={"auth_key": _real_key(ctx)},
        json_body=_v2_body(ctx, ["status", "settings"]),
    )


def _step_v2_key_in_body(ctx: StepContext) -> StepResult:
    return _call(ctx, V2_PATH, json_body=_v2_body(ctx, ["status"], auth_key=_real_key(ctx)))


def _step_v2_key_in_form(ctx: StepContext) -> StepResult:
    form = {"ids": ",".join(_ids(ctx)), "select": "status", "auth_key": _real_key(ctx)}
    return _call(ctx, V2_PATH, form=form)


def _step_v2_key_in_header(ctx: StepContext) -> StepResult:
    return _call(
        ctx,
        V2_PATH,
        json_body=_v2_body(ctx, ["status"]),
        headers={"Authorization": f"Bearer {_real_key(ctx)}"},
    )


def _step_v1_device_settings(ctx: StepContext) -> StepResult:
    return _call(
        ctx, "/device/settings", form={"id": ctx.devices[0].id, "auth_key": _real_key(ctx)}
    )


def _step_wrong_key_v1(ctx: StepContext) -> StepResult:
    return _call(ctx, "/device/all_status", form={"auth_key": DUMMY_KEY})


def _step_wrong_key_v2(ctx: StepContext) -> StepResult:
    return _call(ctx, V2_PATH, query={"auth_key": DUMMY_KEY}, json_body=_v2_body(ctx, ["status"]))


def _step_unknown_id_v2(ctx: StepContext) -> StepResult:
    body = {"ids": [ctx.devices[0].id, UNKNOWN_ID], "select": ["status"]}
    return _call(ctx, V2_PATH, query={"auth_key": _real_key(ctx)}, json_body=body)


def _step_wrong_host(ctx: StepContext) -> StepResult:
    neighbour = sibling_host(ctx.settings.host)
    if neighbour is None:  # run_probe refuses earlier; stay safe if called directly
        return StepResult(None, Outcome("internal", None))
    other = ctx.client.for_host(neighbour, Secret(DUMMY_KEY))
    return _call(ctx, "/device/all_status", client=other, form={"auth_key": DUMMY_KEY})


STEPS: tuple[ProbeStep, ...] = (
    ProbeStep("v1_all_status", _step_v1_all_status, GROUP_DEFAULT, "form"),
    ProbeStep("v1_all_status_info", _step_v1_all_status_info, GROUP_DEFAULT, "form"),
    ProbeStep("v2_get_status", _step_v2_get_status, GROUP_DEFAULT, "query"),
    ProbeStep("v2_get_settings", _step_v2_get_settings, GROUP_DEFAULT, "query"),
    ProbeStep("v2_key_in_body", _step_v2_key_in_body, GROUP_ERRORS, "json"),
    ProbeStep("v2_key_in_form", _step_v2_key_in_form, GROUP_ERRORS, "form"),
    ProbeStep("v2_key_in_header", _step_v2_key_in_header, GROUP_ERRORS, "header"),
    ProbeStep("v1_device_settings", _step_v1_device_settings, GROUP_ERRORS, "form"),
    ProbeStep("wrong_key_v1", _step_wrong_key_v1, GROUP_ERRORS, "dummy"),
    ProbeStep("wrong_key_v2", _step_wrong_key_v2, GROUP_ERRORS, "dummy"),
    ProbeStep("unknown_id_v2", _step_unknown_id_v2, GROUP_ERRORS, "query"),
    ProbeStep("wrong_host", _step_wrong_host, GROUP_WRONG_HOST, "dummy"),
)


def select_steps(*, errors: bool, wrong_host: bool) -> tuple[ProbeStep, ...]:
    wanted = {GROUP_DEFAULT}
    if errors:
        wanted.add(GROUP_ERRORS)
    if wrong_host:
        wanted.add(GROUP_WRONG_HOST)
    return tuple(step for step in STEPS if step.group in wanted)


# ---------------------------------------------------------------------------
# Summary (device NAMES only)
# ---------------------------------------------------------------------------


def _age_minutes(updated: object, now: float) -> str:
    """Age of an `_updated` UTC string in whole minutes, or 'unparseable'."""
    if not isinstance(updated, str):
        return "unparseable"
    try:
        when = datetime.fromisoformat(updated + "+00:00")
    except ValueError:
        return "unparseable"
    return str(round((now - when.timestamp()) / 60))


def _summarize(outcome: Outcome, devices: tuple[Device, ...], now: float, stdout: TextIO) -> bool:
    """Print the name-only summary; return True when every configured device is present."""
    print(f"outcome: {outcome.category}", file=stdout)
    status = outcome.devices if outcome.devices is not None else {}
    known = {k.lower(): v for k, v in status.items()}
    all_present = True
    for dev in devices:
        entry = known.get(dev.id)
        if not isinstance(entry, dict):
            all_present = False
            print(f"{dev.name}: model {dev.model}, present: no", file=stdout)
            continue
        updated = entry.get("_updated")
        age = _age_minutes(updated, now)
        anchor = "yes" if ANCHOR_KEYS[dev.model] in entry else "no"
        age_text = age if age == "unparseable" else f"{age} min"
        print(
            f"{dev.name}: model {dev.model}, present: yes, last report: {updated} UTC, "
            f"age: {age_text}, anchor key: {anchor}",
            file=stdout,
        )
    configured = {d.id for d in devices}
    extra = sum(1 for key in known if key not in configured)
    print(f"unconfigured devices in response: {extra}", file=stdout)
    return all_present


def _load_json(result: StepResult | None) -> Any:
    if result is None or result.raw is None:
        return None
    try:
        return json.loads(result.raw.body)
    except (ValueError, RecursionError):
        return None


def _v2_entries(result: StepResult | None) -> dict[str, dict[str, Any]] | None:
    """v2 entries by lower-case id, or None when the response is not a list of entries."""
    doc = _load_json(result)
    if isinstance(doc, dict) and isinstance(doc.get("data"), list):
        doc = doc["data"]
    if not isinstance(doc, list):
        return None
    return {e["id"].lower(): e for e in doc if isinstance(e, dict) and isinstance(e.get("id"), str)}


def _dig(doc: Any, path: Sequence[str]) -> Any:
    node = doc
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str) and len(value) <= 16:
        return value
    return "present"


def _v1_devices(result: StepResult | None) -> dict[str, Any]:
    devices = result.outcome.devices if result is not None else None
    return {k.lower(): v for k, v in devices.items()} if devices else {}


def _summarize_extras(
    captures: Mapping[str, StepResult], devices: tuple[Device, ...], stdout: TextIO
) -> None:
    """v2 presence, `_dev_info`, reporting settings and one line per error step."""
    v1 = _v1_devices(captures.get("v1_all_status"))
    info = _v1_devices(captures.get("v1_all_status_info"))
    v2_status = captures.get("v2_get_status")
    v2_settings = captures.get("v2_get_settings")
    status_entries = _v2_entries(v2_status)
    settings_entries = _v2_entries(v2_settings)

    if v2_status is not None and status_entries is None:
        code = v2_status.outcome.http_status
        print(
            f"v2 get status: not a list of entries (category {v2_status.outcome.category}, "
            f"HTTP {code if code is not None else 'none'})",
            file=stdout,
        )
    for dev in devices:
        if status_entries is not None:
            entry = status_entries.get(dev.id)
            if entry is None:
                print(f"{dev.name} [v2]: present: no", file=stdout)
            else:
                has_updated = (
                    isinstance(entry.get("status"), dict) and "_updated" in entry["status"]
                )
                flag = "yes" if has_updated else "no"
                print(f"{dev.name} [v2]: present: yes, _updated in status: {flag}", file=stdout)
        if "v1_all_status_info" in captures:
            dev_info = _dig(info.get(dev.id), ("_dev_info",))
            if isinstance(dev_info, dict):
                print(
                    f"{dev.name} [info]: gen {_fmt(dev_info.get('gen'))}, "
                    f"code {_fmt(dev_info.get('code'))}, online {_fmt(dev_info.get('online'))}",
                    file=stdout,
                )
            else:
                print(f"{dev.name} [info]: no _dev_info", file=stdout)
        found: list[str] = []
        wakeup = _dig(v1.get(dev.id), WAKEUP_PATH)
        if wakeup is None and status_entries is not None:
            wakeup = _dig((status_entries.get(dev.id) or {}).get("status"), WAKEUP_PATH)
        if wakeup is not None:
            found.append(f"{'.'.join(WAKEUP_PATH)}={_fmt(wakeup)}")
        if settings_entries is not None:
            block = (settings_entries.get(dev.id) or {}).get("settings")
            for path in SETTINGS_PATHS:
                value = _dig(block, path)
                if value is not None:
                    found.append(f"{'.'.join(path)}={_fmt(value)}")
        if v2_settings is not None or found:
            print(
                f"{dev.name} [reporting]: {', '.join(found) if found else 'none found'}",
                file=stdout,
            )

    for name, result in captures.items():
        step = next((s for s in STEPS if s.name == name), None)
        if step is None or step.group == GROUP_DEFAULT:
            continue
        code = result.outcome.http_status
        http = code if code is not None else "none"
        print(f"{name}: category {result.outcome.category}, HTTP {http}", file=stdout)


# ---------------------------------------------------------------------------
# Fixture export (--save-fixtures)
# ---------------------------------------------------------------------------

NON_JSON_MAX_CHARS = 2000
V1_FIXTURE = "v1_all_status"  # committed by plan 01-03 under the same numbering; never rewritten


def _body_doc(raw: RawResponse) -> Any:
    """Body as a JSON document, the dummy key neutralised; non-JSON text is wrapped."""
    text = raw.body.decode("utf-8", errors="replace").replace(DUMMY_KEY, "dummy-key")
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return {"_non_json_body": text[:NON_JSON_MAX_CHARS]}


def _key_forms(key: str) -> list[str]:
    forms = {key, quote(key, safe=""), quote_plus(key)}
    return sorted(form for form in forms if form)


def _error_entry(step: ProbeStep, result: StepResult) -> dict[str, Any]:
    raw = result.raw
    return {
        "http_status": result.outcome.http_status,
        "has_retry_after": raw is not None and "retry-after" in raw.headers,
        "key_placement": step.key_placement,
        "expected_category": result.outcome.category,
        "body": _body_doc(raw) if raw is not None else None,
    }


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def export_fixtures(
    out_dir: Path,
    chosen: Sequence[ProbeStep],
    captures: Mapping[str, StepResult],
    devices: tuple[Device, ...],
    key: str,
    stdout: TextIO,
) -> int:
    """Sanitize every capture with ONE mapping and write fixtures, or write nothing.

    Messages name steps, rules and JSON paths only: never a body, id, key or URL.
    """
    v1 = captures.get(V1_FIXTURE)
    if v1 is None or v1.raw is None or not v1.outcome.ok:
        print(
            "export refused: the v1_all_status capture is missing, so device numbering "
            "could not match the committed fixture",
            file=stdout,
        )
        return 1

    forms = _key_forms(key)
    for name, result in captures.items():
        if result.raw is None:
            continue
        text = result.raw.body.decode("utf-8", errors="replace")
        if any(form in text for form in forms):
            print(f"export refused: the {name} body contains the auth key", file=stdout)
            return 1

    names: list[str] = []
    docs: list[Any] = []
    for step in chosen:
        raw = captures[step.name].raw
        if step.group == GROUP_DEFAULT and raw is not None:
            names.append(step.name)
            docs.append(_body_doc(raw))
    failing = [s for s in chosen if s.group != GROUP_DEFAULT]
    if failing:
        docs.append({s.name: _error_entry(s, captures[s.name]) for s in failing})
        names.append("errors")

    try:
        outputs = sanitize_docs(docs, [d.id for d in devices])
    except SanitizeError:
        print("export refused: the sanitizer rejected the captured identifiers", file=stdout)
        return 1

    files = {f"{name}.json": text for name, text in zip(names, outputs, strict=True)}
    findings = [(fname, f) for fname, text in files.items() for f in scan(text)]
    if findings:
        for fname, finding in findings:
            print(f"export refused: {fname}: {finding}", file=stdout)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for fname, text in files.items():
        if fname == f"{V1_FIXTURE}.json":
            continue
        _write_atomic(out_dir / fname, text)
        written += 1
    print(f"saved {written} sanitized fixture file(s)", file=stdout)
    return 0


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_probe(
    settings: Settings,
    devices: tuple[Device, ...],
    client: ShellyClient,
    clock: Clock,
    *,
    out_root: Path = Path("data/probe"),
    stdout: TextIO | None = None,
    errors: bool = False,
    wrong_host: bool = False,
    save_fixtures: Path | None = None,
    steps: Sequence[ProbeStep] | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    chosen = (
        tuple(steps) if steps is not None else select_steps(errors=errors, wrong_host=wrong_host)
    )
    if any(s.group == GROUP_WRONG_HOST for s in chosen) and sibling_host(settings.host) is None:
        print(
            "--wrong-host needs a regional Shelly Cloud host name to derive a sibling from; "
            "nothing was sent",
            file=sys.stderr,
        )
        return 2

    stamp = datetime.fromtimestamp(clock.time(), UTC).strftime("%Y%m%dT%H%M%SZ")
    out_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    out_dir = out_root / stamp
    out_dir.mkdir(mode=0o700, exist_ok=True)

    ctx = StepContext(client, clock, settings, devices)
    index: list[dict[str, object]] = []
    captures: dict[str, StepResult] = {}
    baseline: Outcome | None = None
    for step in chosen:
        result = step.run(ctx)
        captures[step.name] = result
        body = result.raw.body if result.raw is not None else b""
        if result.raw is not None:
            (out_dir / f"{step.name}.json").write_bytes(body)
        index.append(
            {
                "step": step.name,
                "http_status": result.outcome.http_status,
                "category": result.outcome.category,
                "retry_after_s": result.outcome.retry_after_s,
                "latency_ms": result.outcome.latency_ms,
                "bytes": len(body),
                "key_placement": step.key_placement,
            }
        )
        if baseline is None:
            baseline = result.outcome
    (out_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    if baseline is None:
        return 1
    all_present = _summarize(baseline, devices, clock.time(), out)
    _summarize_extras(captures, devices, out)
    code = 0 if baseline.ok and all_present else 1
    if save_fixtures is not None:
        exported = export_fixtures(
            save_fixtures, chosen, captures, devices, client.key.reveal(), out
        )
        if exported != 0:
            code = 1
    return code


def main(
    settings: Settings,
    devices: tuple[Device, ...],
    *,
    errors: bool = False,
    wrong_host: bool = False,
    save_fixtures: Path | None = None,
    clock: Clock | None = None,
    send: Callable[..., RawResponse] | None = None,
    out_root: Path = Path("data/probe"),
) -> int:
    # Same lock as `poll`: the two processes share one 1 req/s account budget.
    try:
        handle = acquire_instance_lock(settings.db_path.parent / "poller.lock")
    except AlreadyRunning:
        print("another shelog poll/probe process is running; probe not started", file=sys.stderr)
        return 3
    try:
        the_clock = clock if clock is not None else SystemClock()
        client = ShellyClient(
            settings.host,
            settings.auth_key,
            send=send if send is not None else transport.send,
            guard=RateGuard(the_clock),
            clock=the_clock,
        )
        return run_probe(
            settings,
            devices,
            client,
            the_clock,
            out_root=out_root,
            errors=errors,
            wrong_host=wrong_host,
            save_fixtures=save_fixtures,
        )
    finally:
        handle.close()
