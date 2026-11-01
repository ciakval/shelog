"""Configuration: secrets file, environment and the device inventory.

The app loads its own secrets file from the working directory (no path on the command
line) and real environment variables win over file values. Nothing in this module ever
prints a secret or a device id: a ConfigError names a variable or a device NAME only.
"""

from __future__ import annotations

import logging
import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

GROUPS = frozenset({"subfloor", "reference"})
MODELS = frozenset({"ht_gen1", "ht_gen3", "flood_gen1"})


class ConfigError(Exception):
    """Invalid or missing configuration. The message never contains a secret value."""


class Secret:
    """Wrapper that never renders its value; call reveal() at the single point of use."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Secret is immutable")

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "***"

    def __str__(self) -> str:
        return "***"

    def __format__(self, format_spec: str) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    group: str
    model: str
    note: str = ""
    t_offset: float = 0.0
    rh_offset: float = 0.0
    max_silence_s: int | None = None


@dataclass(frozen=True)
class Settings:
    auth_key: Secret
    host: str
    db_path: Path
    devices_file: Path
    poll_interval_s: float
    log_level: str


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. No interpolation; never touches os.environ."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


_HOST_CHARS = re.compile(r"[a-z0-9.-]+")
_DEVICE_ID = re.compile(r"(?:[0-9a-f]{6}|[0-9a-f]{12})")
_DEVICE_KEYS = frozenset(
    {"id", "name", "note", "group", "model", "t_offset", "rh_offset", "max_silence_s"}
)
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
_MIN_KEY_LENGTH = 16
_MIN_INTERVAL_S = 30.0


def normalize_host(raw: str) -> str:
    """Reduce SHELLY_HOST to a bare lower-case hostname or raise ConfigError.

    Only [a-z0-9.-] survives: this blocks credentials, ports, paths, queries and fragments
    in the host, so the key can never go to a confused host or over plain http.
    """
    host = raw.strip().lower()
    if host.startswith("http://"):
        raise ConfigError("SHELLY_HOST must not use http:// (the key would travel in clear text)")
    host = host.removeprefix("https://").rstrip("/")
    if not host:
        raise ConfigError("SHELLY_HOST is empty")
    if not _HOST_CHARS.fullmatch(host):
        raise ConfigError("SHELLY_HOST must be a plain hostname (no credentials, port or path)")
    return host


def _require(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ConfigError(f"missing required setting {name}")
    return value


def _check_key(key: str) -> None:
    # Never echo the value or its length.
    if key.lower() == "change_me" or len(key) < _MIN_KEY_LENGTH:
        raise ConfigError("SHELLY_AUTH_KEY is a placeholder or too short to be a real key")


def _parse_interval(raw: str) -> float:
    try:
        interval = float(raw)
    except ValueError:
        raise ConfigError("SHELOG_POLL_INTERVAL_S must be a number") from None
    if not math.isfinite(interval) or interval < _MIN_INTERVAL_S:
        raise ConfigError("SHELOG_POLL_INTERVAL_S must be a finite number of at least 30")
    return interval


def load_settings(
    environ: Mapping[str, str] | None = None, dotenv_path: Path = Path(".env")
) -> Settings:
    env = os.environ if environ is None else environ
    merged: dict[str, str] = dict(read_dotenv(dotenv_path))
    # Real environment variables win over file values.
    merged.update({k: v for k, v in env.items() if k.startswith(("SHELLY_", "SHELOG_"))})

    auth_key = _require(merged, "SHELLY_AUTH_KEY")
    _check_key(auth_key)
    host = normalize_host(_require(merged, "SHELLY_HOST"))
    if not host.endswith(".shelly.cloud"):
        # Shelly can relocate hosts, so this is a warning; the value is not echoed.
        log.warning("SHELLY_HOST is not a *.shelly.cloud host")

    interval = _parse_interval(merged.get("SHELOG_POLL_INTERVAL_S", "60").strip() or "60")
    log_level = merged.get("SHELOG_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if log_level not in _LOG_LEVELS:
        raise ConfigError("SHELOG_LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR")

    return Settings(
        auth_key=Secret(auth_key),
        host=host,
        db_path=Path(merged.get("SHELOG_DB_PATH", "data/shelog.db")),
        devices_file=Path(merged.get("SHELOG_DEVICES_FILE", "devices.toml")),
        poll_interval_s=interval,
        log_level=log_level,
    )


def _device_label(entry: object, index: int) -> str:
    if isinstance(entry, dict):
        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            return f"device {name.strip()!r}"
    return f"device #{index}"


def _number(entry: dict[str, object], key: str, label: str) -> float:
    value = entry.get(key, 0.0)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{label}: {key} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"{label}: {key} must be a finite number")
    return number


def load_devices(path: Path) -> tuple[Device, ...]:
    try:
        with path.open("rb") as fh:
            doc = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError("devices file not found") from None
    except tomllib.TOMLDecodeError:
        raise ConfigError("devices file is not valid TOML") from None

    entries = doc.get("device")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("devices file has no [[device]] entries")

    devices: list[Device] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        label = _device_label(entry, index)
        if not isinstance(entry, dict):
            raise ConfigError(f"{label}: not a table")
        unknown = sorted(set(entry) - _DEVICE_KEYS)
        if unknown:
            # Key names are config vocabulary (typos), never secrets.
            raise ConfigError(f"{label}: unknown key {', '.join(unknown)}")
        for key in ("id", "name", "group", "model"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{label}: missing or invalid {key}")
        device_id = entry["id"].strip().lower()
        if not _DEVICE_ID.fullmatch(device_id):
            raise ConfigError(f"{label}: id must be 6 or 12 hex characters")
        name = entry["name"].strip()
        if device_id in seen_ids:
            raise ConfigError(f"{label}: duplicate id")
        if name in seen_names:
            raise ConfigError(f"{label}: duplicate name")
        seen_ids.add(device_id)
        seen_names.add(name)
        if entry["group"] not in GROUPS:
            raise ConfigError(f"{label}: group must be one of {sorted(GROUPS)}")
        if entry["model"] not in MODELS:
            raise ConfigError(f"{label}: model must be one of {sorted(MODELS)}")
        if entry["model"] == "flood_gen1" and "rh_offset" in entry:
            raise ConfigError(f"{label}: rh_offset is not allowed on a flood_gen1")
        note = entry.get("note", "")
        if not isinstance(note, str):
            raise ConfigError(f"{label}: note must be a string")
        max_silence = entry.get("max_silence_s")
        if max_silence is not None and (
            isinstance(max_silence, bool) or not isinstance(max_silence, int) or max_silence <= 0
        ):
            raise ConfigError(f"{label}: max_silence_s must be a positive integer")
        devices.append(
            Device(
                id=device_id,
                name=name,
                group=entry["group"],
                model=entry["model"],
                note=note,
                t_offset=_number(entry, "t_offset", label),
                rh_offset=_number(entry, "rh_offset", label),
                max_silence_s=max_silence,
            )
        )
    return tuple(devices)
