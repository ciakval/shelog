"""Deterministic fixture pseudonymizer and structural identifier scanner.

The sanitizer is the only path from a real Shelly response to a committed file. It is
device-centric: every identifier is found by field name or by value shape, mapped to a
fixed pseudo space with sequential counters (never hashes, 24-bit ids are brute-forceable),
and replaced in one longest-first pass over the serialized JSON.

Pseudo spaces: device MACs 02:00:00:00:00:NN, other MACs 02:01:00:00:00:KK, IPv4
192.0.2.K (RFC 5737), IPv6 2001:db8::K (RFC 3849), SSID fixture-ssid-K.

The scanner reports a rule name and a JSON path or character offset only. It never echoes
the offending value, because CI logs of a public repo are public.

Dev CLI: python -m shelog.sanitize --devices-file PATH OUTDIR INPUT [INPUT ...]
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import tomllib
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

PSEUDO_ID_RE = re.compile(r"02[0-9a-f]{2}0000[0-9a-f]{4}|0000[0-9a-f]{2}")
_PSEUDO_MAC_RE = re.compile(r"02[0-9a-f]{2}0000[0-9a-f]{4}")
_PSEUDO_TAIL_RE = re.compile(r"0000[0-9a-f]{2}")
_PSEUDO_NET_V4 = ipaddress.ip_network("192.0.2.0/24")
_PSEUDO_NET_V6 = ipaddress.ip_network("2001:db8::/32")

# Firmware/build/label strings of 20+ characters that are legitimately present in fixtures.
# Each entry is a regex that must fully match the token. Entries are added only after the
# live capture is inspected; see the plan summary for the list.
KNOWN_TOKENS: tuple[re.Pattern[str], ...] = (
    # getinfo.fw_info.device: "shelly<model>-<MAC>" with the (pseudo) device MAC or its tail.
    re.compile(r"shelly[a-z0-9]*-(?:02[0-9a-f]{2}0000[0-9a-f]{4}|0000[0-9a-f]{2})", re.IGNORECASE),
)

_HEX6 = re.compile(r"[0-9A-Fa-f]{6}")
_HEX12 = re.compile(r"[0-9A-Fa-f]{12}")
_COLON_MAC = re.compile(r"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Za-z:])")
_BARE_MAC = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{12}(?![0-9A-Za-z])")
_IPV4 = re.compile(r"(?<![0-9A-Za-z.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9A-Za-z]|\.\d)")
_IPV6 = re.compile(r"(?<![0-9A-Za-z:])[0-9A-Fa-f:]{2,}(?:%[0-9A-Za-z]+)?(?![0-9A-Za-z:])")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,}")
_SHELLY_TAIL = re.compile(r"(?i)shelly[a-z0-9]*-([0-9a-f]{6})(?![0-9A-Za-z])")
_TEXT_SSID = re.compile(r"(?i)\bssid\"?\s*[=:]\s*\"?([^\"\n,}]*)")
_SAFE_KEY = re.compile(r"[A-Za-z_]+(?::\d+)?")


class SanitizeError(Exception):
    """Sanitizing was refused. The message never contains an identifier value."""


# ---------------------------------------------------------------------------
# JSON walking
# ---------------------------------------------------------------------------

Visit = Callable[[str, str | None, str], None]


def _walk(node: Any, parent_key: str | None, visit: Visit) -> None:
    """Call visit(kind, parent_key, string) for every key and every string value."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                visit("key", parent_key, key)
            _walk(value, key if isinstance(key, str) else None, visit)
    elif isinstance(node, list):
        for item in node:
            _walk(item, parent_key, visit)
    elif isinstance(node, str):
        visit("value", parent_key, node)


def _norm_mac(token: str) -> str:
    return token.replace(":", "").lower()


def _colon(bare12: str) -> str:
    return ":".join(bare12[i : i + 2] for i in range(0, 12, 2))


def _strip_zone(token: str) -> str:
    return token.split("%", 1)[0]


def _ip_of(token: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(_strip_zone(token))
    except ValueError:
        return None


def _ip_candidates(text: str) -> Iterable[str]:
    """Tokens inside text that are accepted as an IP address by the stdlib."""
    for regex in (_IPV4, _IPV6):
        for match in regex.finditer(text):
            token = match.group(0)
            if _ip_of(token) is not None:
                yield token


def _is_exempt_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return addr.is_unspecified or addr.is_loopback


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def build_mapping(docs: Sequence[Any], extra_ids: Iterable[str] = ()) -> dict[str, str]:
    """Map every discovered identifier (lowercase original) to its pseudo value."""
    device_ids: set[str] = set()
    macs: set[str] = set()  # normalized 12-hex
    ipv4: set[str] = set()
    ipv6: set[str] = set()
    ssids: set[str] = set()

    def visit(kind: str, parent_key: str | None, text: str) -> None:
        hex_shaped = bool(_HEX6.fullmatch(text) or _HEX12.fullmatch(text))
        is_device_key = kind == "key" and parent_key == "devices_status"
        if is_device_key and not hex_shaped:
            raise SanitizeError("a devices_status key is not a 6 or 12 hex device id")
        if is_device_key or (kind == "value" and parent_key == "id" and hex_shaped):
            device_ids.add(text.lower())
        if kind == "value" and parent_key == "ssid" and text:
            ssids.add(text)
        for match in _COLON_MAC.finditer(text):
            macs.add(_norm_mac(match.group(0)))
        for match in _BARE_MAC.finditer(text):
            macs.add(match.group(0).lower())
        for token in _ip_candidates(text):
            addr = _ip_of(token)
            if addr is None or _is_exempt_ip(addr):
                continue
            (ipv4 if addr.version == 4 else ipv6).add(_strip_zone(token).lower())

    for doc in docs:
        _walk(doc, None, visit)
    for extra in extra_ids:
        value = extra.strip().lower()
        if not (_HEX6.fullmatch(value) or _HEX12.fullmatch(value)):
            raise SanitizeError("a configured device id is not a 6 or 12 hex id")
        device_ids.add(value)

    if any(PSEUDO_ID_RE.fullmatch(device_id) for device_id in device_ids):
        raise SanitizeError("input device ids already match the pseudo id space")

    mapping: dict[str, str] = {}
    tail_to_pseudo12: dict[str, str] = {}
    for number, device_id in enumerate(sorted(device_ids), start=1):
        if number > 255:
            raise SanitizeError("more than 255 devices")
        pseudo12 = f"0200000000{number:02x}"
        tail = device_id[-6:]
        if tail in tail_to_pseudo12 and tail_to_pseudo12[tail] != pseudo12:
            raise SanitizeError("two device ids share the same 6-hex tail")
        tail_to_pseudo12[tail] = pseudo12
        if len(device_id) == 12:
            mapping[device_id] = pseudo12
            mapping[_colon(device_id)] = _colon(pseudo12)
            mapping.setdefault(tail, pseudo12[-6:])
        else:
            mapping[device_id] = pseudo12[-6:]

    others: list[str] = []
    for mac in sorted(macs):
        if mac in mapping:
            continue
        pseudo12 = tail_to_pseudo12.get(mac[-6:])
        if pseudo12 is not None:
            mapping[mac] = pseudo12
            mapping[_colon(mac)] = _colon(pseudo12)
        else:
            others.append(mac)
    for index, mac in enumerate(others, start=1):
        if index > 255:
            raise SanitizeError("more than 255 other MAC addresses")
        pseudo12 = f"0201000000{index:02x}"
        mapping[mac] = pseudo12
        mapping[_colon(mac)] = _colon(pseudo12)

    for index, address in enumerate(sorted(ipv4, key=lambda a: int(ipaddress.IPv4Address(a))), 1):
        if index > 254:
            raise SanitizeError("more than 254 IPv4 addresses")
        mapping[address] = f"192.0.2.{index}"
    for index, address in enumerate(sorted(ipv6), start=1):
        mapping[address] = f"2001:db8::{index:x}"
    for index, ssid in enumerate(sorted(ssids), start=1):
        pseudo = f"fixture-ssid-{index}"
        mapping[ssid.lower()] = pseudo
        escaped = json.dumps(ssid, ensure_ascii=False)[1:-1].lower()
        mapping.setdefault(escaped, pseudo)
    return mapping


def apply(text: str, mapping: Mapping[str, str]) -> str:
    """Replace every mapped original in one longest-first, case-insensitive pass."""
    if not mapping:
        return text
    lowered = {key.lower(): value for key, value in mapping.items()}
    keys = sorted(lowered, key=lambda k: (-len(k), k))
    pattern = re.compile(
        r"(?<![0-9A-Za-z])(?:" + "|".join(re.escape(k) for k in keys) + r")(?![0-9A-Za-z])",
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        original = match.group(0)
        pseudo = lowered[original.lower()]
        # Only hex-like pseudo values (ids, MACs, IPv6) follow the original's letter case.
        if original.isupper() and re.fullmatch(r"[0-9a-f:]+", pseudo):
            return pseudo.upper()
        return pseudo

    return pattern.sub(replace, text)


DEVICE_KEY_PLACEHOLDER = "fixture-device-key"


def _redact_device_keys(node: Any, parent_key: str | None = None) -> Any:
    """Replace every `DeviceInfo.key` value (a Gen3 per-device signed token) with a placeholder.

    The v2 settings of a Gen3 device carry `DeviceInfo.key`, a long dotted token tied to the
    device. It is not an id the mapping can pseudonymize, and it must not be allow-listed in
    KNOWN_TOKENS (that would commit it), so the sanitizer drops the value itself.
    """
    if isinstance(node, dict):
        return {
            key: (
                DEVICE_KEY_PLACEHOLDER
                if parent_key == "DeviceInfo" and key == "key" and isinstance(value, str)
                else _redact_device_keys(value, key if isinstance(key, str) else None)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_redact_device_keys(item, parent_key) for item in node]
    return node


def sanitize_docs(docs: Sequence[Any], extra_ids: Iterable[str] = ()) -> list[str]:
    """Serialize and sanitize all docs with ONE shared mapping. Deterministic."""
    mapping = build_mapping(docs, extra_ids)
    return [
        apply(
            json.dumps(_redact_device_keys(doc), sort_keys=True, indent=2, ensure_ascii=False)
            + "\n",
            mapping,
        )
        for doc in docs
    ]


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def _safe_segment(key: str, index: int) -> str:
    if _SAFE_KEY.fullmatch(key) and not (_HEX6.fullmatch(key) or _HEX12.fullmatch(key)):
        return key
    return f"[#{index}]"


def _value_findings(text: str, *, check_long: bool) -> list[tuple[str, int]]:
    """(rule, offset) pairs found inside one string."""
    found: list[tuple[str, int]] = []
    for match in _COLON_MAC.finditer(text):
        if not _PSEUDO_MAC_RE.fullmatch(_norm_mac(match.group(0))):
            found.append(("mac_outside_pseudo_space", match.start()))
    for match in _BARE_MAC.finditer(text):
        if not _PSEUDO_MAC_RE.fullmatch(match.group(0).lower()):
            found.append(("mac_outside_pseudo_space", match.start()))
    for regex in (_IPV4, _IPV6):
        for match in regex.finditer(text):
            addr = _ip_of(match.group(0))
            if addr is None or _is_exempt_ip(addr):
                continue
            net = _PSEUDO_NET_V4 if addr.version == 4 else _PSEUDO_NET_V6
            if addr not in net:
                rule = "ipv4_outside_pseudo_net" if addr.version == 4 else "ipv6_outside_pseudo_net"
                found.append((rule, match.start()))
    for match in _SHELLY_TAIL.finditer(text):
        if not _PSEUDO_TAIL_RE.fullmatch(match.group(1).lower()):
            found.append(("device_tail_outside_pseudo_space", match.start(1)))
    if check_long:
        for match in _LONG_TOKEN.finditer(text):
            if not any(known.fullmatch(match.group(0)) for known in KNOWN_TOKENS):
                found.append(("long_token", match.start()))
    return found


def _id_allowed(value: str, allowed_ids: Collection[str] | None) -> bool:
    if allowed_ids is None:
        return PSEUDO_ID_RE.fullmatch(value.lower()) is not None
    return value.lower() in {a.lower() for a in allowed_ids}


def scan(text: str, allowed_ids: Collection[str] | None = None) -> list[str]:
    """Findings as "rule at location" strings. Never contains the offending value."""
    findings: list[str] = []
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
        parsed = False
    else:
        parsed = True

    if not parsed:
        for rule, offset in _value_findings(text, check_long=True):
            findings.append(f"{rule} at offset {offset}")
        for match in _TEXT_SSID.finditer(text):
            ssid = match.group(1)
            if ssid and not ssid.startswith("fixture-ssid-"):
                findings.append(f"ssid_not_pseudo at offset {match.start(1)}")
        return findings

    def walk(node: Any, path: str, key: str | None) -> None:
        if isinstance(node, dict):
            for index, (child_key, child) in enumerate(node.items()):
                child_path = f"{path}.{_safe_segment(str(child_key), index)}"
                if key == "devices_status" and not _id_allowed(str(child_key), allowed_ids):
                    findings.append(f"device_key_not_allowed at {child_path}")
                for rule, _offset in _value_findings(str(child_key), check_long=False):
                    findings.append(f"{rule} at {child_path} (key)")
                walk(child, child_path, str(child_key))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]", key)
        elif isinstance(node, str):
            for rule, offset in _value_findings(node, check_long=True):
                findings.append(f"{rule} at {path} char {offset}")
            if key == "ssid" and node and not node.startswith("fixture-ssid-"):
                findings.append(f"ssid_not_pseudo at {path}")
            if (
                key == "id"
                and (_HEX6.fullmatch(node) or _HEX12.fullmatch(node))
                and not _id_allowed(node, allowed_ids)
            ):
                findings.append(f"id_not_allowed at {path}")

    walk(doc, "$", None)
    return findings


# ---------------------------------------------------------------------------
# Dev CLI
# ---------------------------------------------------------------------------


def _device_ids_from_toml(path: Path) -> list[str]:
    with path.open("rb") as fh:
        doc = tomllib.load(fh)
    entries = doc.get("device", [])
    return [str(entry["id"]) for entry in entries if isinstance(entry, dict) and "id" in entry]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shelog.sanitize",
        description="Sanitize raw Shelly JSON captures into committable fixtures.",
    )
    parser.add_argument("--devices-file", type=Path, required=True, help="devices.toml with ids")
    parser.add_argument("outdir", type=Path, help="directory for the sanitized output files")
    parser.add_argument("inputs", nargs="+", type=Path, help="raw JSON capture files")
    args = parser.parse_args(argv)

    try:
        extra_ids = _device_ids_from_toml(args.devices_file)
        docs = [json.loads(path.read_text(encoding="utf-8")) for path in args.inputs]
        outputs = sanitize_docs(docs, extra_ids)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError, SanitizeError) as err:
        print(f"sanitize failed: {type(err).__name__}", file=sys.stderr)
        return 1

    total = 0
    for path, output in zip(args.inputs, outputs, strict=True):
        findings = scan(output)
        total += len(findings)
        for finding in findings:
            print(f"{path.name}: {finding}", file=sys.stderr)
    if total:
        print(f"sanitize refused: {total} finding(s), nothing written", file=sys.stderr)
        return 1

    args.outdir.mkdir(parents=True, exist_ok=True)
    for path, output in zip(args.inputs, outputs, strict=True):
        (args.outdir / path.name).write_text(output, encoding="utf-8")
    print(f"sanitized {len(outputs)} file(s), {len(extra_ids)} configured device id(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
