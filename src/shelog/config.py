"""Configuration: secrets file, environment and the device inventory.

The app loads its own secrets file from the working directory (no path on the command
line) and real environment variables win over file values. Nothing in this module ever
prints a secret or a device id: a ConfigError names a variable or a device NAME only.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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


def normalize_host(raw: str) -> str:
    host = raw.strip().lower()
    if host.startswith("http://"):
        raise ConfigError("SHELLY_HOST must not use http:// (the key would travel in clear text)")
    host = host.removeprefix("https://")
    host = host.rstrip("/")
    if not host:
        raise ConfigError("SHELLY_HOST is empty")
    return host


def _require(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ConfigError(f"missing required setting {name}")
    return value


def load_settings(
    environ: Mapping[str, str] | None = None, dotenv_path: Path = Path(".env")
) -> Settings:
    env = os.environ if environ is None else environ
    merged: dict[str, str] = dict(read_dotenv(dotenv_path))
    # Real environment variables win over file values.
    merged.update({k: v for k, v in env.items() if k.startswith(("SHELLY_", "SHELOG_"))})

    auth_key = _require(merged, "SHELLY_AUTH_KEY")
    host = normalize_host(_require(merged, "SHELLY_HOST"))

    interval_raw = merged.get("SHELOG_POLL_INTERVAL_S", "60").strip() or "60"
    try:
        interval = float(interval_raw)
    except ValueError:
        raise ConfigError("SHELOG_POLL_INTERVAL_S must be a number") from None

    return Settings(
        auth_key=Secret(auth_key),
        host=host,
        db_path=Path(merged.get("SHELOG_DB_PATH", "data/shelog.db")),
        devices_file=Path(merged.get("SHELOG_DEVICES_FILE", "devices.toml")),
        poll_interval_s=interval,
        log_level=merged.get("SHELOG_LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )


def _device_label(entry: object, index: int) -> str:
    if isinstance(entry, dict):
        name = entry.get("name")
        if isinstance(name, str) and name:
            return f"device {name!r}"
    return f"device #{index}"


def _number(entry: dict[str, object], key: str, label: str, default: float) -> float:
    value = entry.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{label}: {key} must be a number")
    return float(value)


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
    for index, entry in enumerate(entries, start=1):
        label = _device_label(entry, index)
        if not isinstance(entry, dict):
            raise ConfigError(f"{label}: not a table")
        for key in ("id", "name", "group", "model"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{label}: missing or invalid {key}")
        if entry["group"] not in GROUPS:
            raise ConfigError(f"{label}: group must be one of {sorted(GROUPS)}")
        if entry["model"] not in MODELS:
            raise ConfigError(f"{label}: model must be one of {sorted(MODELS)}")
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
                id=entry["id"].strip().lower(),
                name=entry["name"].strip(),
                group=entry["group"],
                model=entry["model"],
                note=note,
                t_offset=_number(entry, "t_offset", label, 0.0),
                rh_offset=_number(entry, "rh_offset", label, 0.0),
                max_silence_s=max_silence,
            )
        )
    return tuple(devices)
