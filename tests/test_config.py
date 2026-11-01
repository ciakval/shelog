"""Strict validation of settings, host, dotenv parsing and devices.toml (COLL-07, COLL-08)."""

from __future__ import annotations

import logging
import math
import os
import re
from pathlib import Path

import pytest

from shelog.config import (
    ConfigError,
    Secret,
    load_devices,
    load_settings,
    normalize_host,
    read_dotenv,
)

REPO = Path(__file__).resolve().parent.parent
GOOD_KEY = "SENTINEL-KEY-0123456789abcdef"
GOOD_HOST = "shelly-00-eu.shelly.cloud"


def _env(**overrides: str) -> dict[str, str]:
    env = {"SHELLY_AUTH_KEY": GOOD_KEY, "SHELLY_HOST": GOOD_HOST}
    env.update(overrides)
    return env


# --- host ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Shelly-12-EU.shelly.cloud/", "shelly-12-eu.shelly.cloud"),
        ("shelly-12-eu.shelly.cloud", "shelly-12-eu.shelly.cloud"),
        ("  shelly-12-eu.shelly.cloud  ", "shelly-12-eu.shelly.cloud"),
    ],
)
def test_normalize_host_accepts(raw: str, expected: str) -> None:
    assert normalize_host(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://h.shelly.cloud",
        "h.shelly.cloud@evil.example",
        "h.shelly.cloud:443",
        "h.shelly.cloud/path",
        "h.shelly.cloud?x=1",
        "h.shelly.cloud#f",
        "",
        "   ",
        "h .shelly.cloud",
        "https://user:pw@h.shelly.cloud",
    ],
)
def test_normalize_host_rejects(raw: str) -> None:
    with pytest.raises(ConfigError):
        normalize_host(raw)


def test_foreign_host_warns_without_echoing_it(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="shelog.config"):
        settings = load_settings(_env(SHELLY_HOST="other.example.org"), Path("nonexistent"))
    assert settings.host == "other.example.org"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "other.example.org" not in warnings[0].getMessage()


def test_shelly_host_gives_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="shelog.config"):
        load_settings(_env(), Path("nonexistent"))
    assert not caplog.records


# --- key -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["", "   ", "CHANGE_ME", "change_me", "Change_Me", "short-key-15chr"]
)
def test_placeholder_or_short_key_rejected(bad: str) -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings(_env(SHELLY_AUTH_KEY=bad), Path("nonexistent"))
    message = str(exc.value)
    assert "SHELLY_AUTH_KEY" in message
    if bad.strip():
        assert bad not in message
    assert str(len(bad)) not in message


def test_sixteen_char_key_passes() -> None:
    settings = load_settings(_env(SHELLY_AUTH_KEY="a" * 16), Path("nonexistent"))
    assert settings.auth_key.reveal() == "a" * 16


# --- interval and log level ----------------------------------------------------------


@pytest.mark.parametrize("bad", ["29", "0", "-5", "abc", "nan", "inf", "-inf"])
def test_bad_interval_rejected(bad: str) -> None:
    with pytest.raises(ConfigError, match="SHELOG_POLL_INTERVAL_S"):
        load_settings(_env(SHELOG_POLL_INTERVAL_S=bad), Path("nonexistent"))


@pytest.mark.parametrize("ok", ["30", "60", "60.5"])
def test_good_interval_passes(ok: str) -> None:
    settings = load_settings(_env(SHELOG_POLL_INTERVAL_S=ok), Path("nonexistent"))
    assert settings.poll_interval_s == float(ok)
    assert math.isfinite(settings.poll_interval_s)


@pytest.mark.parametrize("bad", ["TRACE", "verbose", "CRITICAL", "10"])
def test_bad_log_level_rejected(bad: str) -> None:
    with pytest.raises(ConfigError, match="SHELOG_LOG_LEVEL"):
        load_settings(_env(SHELOG_LOG_LEVEL=bad), Path("nonexistent"))


@pytest.mark.parametrize("ok", ["DEBUG", "info", "Warning", "ERROR"])
def test_good_log_level_passes(ok: str) -> None:
    settings = load_settings(_env(SHELOG_LOG_LEVEL=ok), Path("nonexistent"))
    assert settings.log_level == ok.upper()


# --- dotenv parsing ------------------------------------------------------------------


def _write_dotenv(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "secrets.file"
    path.write_text(text, encoding="utf-8")
    return path


def test_dotenv_forms(tmp_path: Path) -> None:
    path = _write_dotenv(
        tmp_path,
        "\n".join(
            [
                "# a comment",
                "",
                "export A=1",
                "B='two words'",
                'C="three words"',
                "D=has=equals=inside",
                "E=$OTHER",
                "   F  =  spaced  ",
            ]
        ),
    )
    parsed = read_dotenv(path)
    assert parsed["A"] == "1"
    assert parsed["B"] == "two words"
    assert parsed["C"] == "three words"
    assert parsed["D"] == "has=equals=inside"
    assert parsed["E"] == "$OTHER"
    assert parsed["F"] == "spaced"


def test_dotenv_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_dotenv(tmp_path / "absent") == {}


def test_real_environment_overrides_file_and_environ_untouched(tmp_path: Path) -> None:
    path = _write_dotenv(
        tmp_path,
        f"SHELLY_AUTH_KEY={GOOD_KEY}\nSHELLY_HOST={GOOD_HOST}\nSHELOG_LOG_LEVEL=DEBUG\n",
    )
    before = dict(os.environ)
    settings = load_settings({"SHELOG_LOG_LEVEL": "ERROR"}, path)
    assert settings.log_level == "ERROR"
    assert settings.auth_key.reveal() == GOOD_KEY
    assert dict(os.environ) == before


def test_default_dotenv_is_read_from_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(f"SHELLY_AUTH_KEY={GOOD_KEY}\nSHELLY_HOST={GOOD_HOST}\n")
    monkeypatch.chdir(tmp_path)
    settings = load_settings({})
    assert settings.host == GOOD_HOST


def test_missing_required_setting_names_the_variable() -> None:
    with pytest.raises(ConfigError, match="SHELLY_HOST"):
        load_settings({"SHELLY_AUTH_KEY": GOOD_KEY}, Path("nonexistent"))


def test_settings_never_render_the_key() -> None:
    settings = load_settings(_env(), Path("nonexistent"))
    for rendered in (repr(settings), str(settings), f"{settings}", f"{settings.auth_key}"):
        assert GOOD_KEY not in rendered
    assert isinstance(settings.auth_key, Secret)
    assert "***" in repr(settings)


# --- devices.toml --------------------------------------------------------------------

_GOOD = """
[[device]]
id = "aabbcc"
name = "Alpha"
group = "subfloor"
model = "ht_gen1"
"""


def _devices(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "devices.toml"
    path.write_text(body, encoding="utf-8")
    return path


class Raw(str):
    """Marks a value rendered verbatim into TOML (bare nan / inf literals)."""


def _entry(**fields: object) -> str:
    base: dict[str, object] = {
        "id": "aabbcc",
        "name": "Alpha",
        "group": "subfloor",
        "model": "ht_gen1",
    }
    base.update(fields)
    lines = ["[[device]]"]
    for key, value in base.items():
        if isinstance(value, Raw):
            rendered = str(value)
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, str):
            rendered = f'"{value}"'
        else:
            rendered = str(value)
        lines.append(f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def test_minimal_device_loads_with_defaults(tmp_path: Path) -> None:
    (device,) = load_devices(_devices(tmp_path, _GOOD))
    assert device.t_offset == 0.0
    assert device.rh_offset == 0.0
    assert device.max_silence_s is None


def test_six_and_twelve_hex_ids_load_and_are_lowercased(tmp_path: Path) -> None:
    body = _entry(id="AABBCC", name="A") + _entry(id="0200000000AB", name="B")
    devices = load_devices(_devices(tmp_path, body))
    assert [d.id for d in devices] == ["aabbcc", "0200000000ab"]


@pytest.mark.parametrize(
    ("body", "name"),
    [
        (_entry(t_ofset=0.5), "unknown key"),
        (_entry() + _entry(name="Beta"), "duplicate id"),
        (_entry(id="AABBCC") + _entry(id="aabbcc", name="Beta"), "duplicate id case"),
        (_entry(id="aabbcc") + _entry(id="ddeeff"), "duplicate name"),
        (_entry(group="kitchen"), "bad group"),
        (_entry(model="ht_gen2"), "bad model"),
        (_entry(id="xyz"), "bad id"),
        (_entry(id="aabbccdd"), "8 hex id"),
        (_entry(id="gggggg"), "non hex id"),
        (_entry(t_offset=True), "bool t_offset"),
        (_entry(t_offset="nan"), "string t_offset"),
        (_entry(model="flood_gen1", rh_offset=1.0), "flood rh_offset"),
        (_entry(max_silence_s=0), "zero silence"),
        (_entry(max_silence_s=-5), "negative silence"),
        (_entry(max_silence_s=1.5), "float silence"),
        (_entry(max_silence_s=True), "bool silence"),
        ("", "empty file"),
    ],
)
def test_bad_devices_rejected(tmp_path: Path, body: str, name: str) -> None:
    with pytest.raises(ConfigError):
        load_devices(_devices(tmp_path, body))


@pytest.mark.parametrize("literal", ["nan", "inf", "-inf"])
def test_non_finite_offsets_rejected(tmp_path: Path, literal: str) -> None:
    body = _entry(t_offset=Raw(literal))  # bare TOML nan / inf literals, not strings
    with pytest.raises(ConfigError, match="Alpha"):
        load_devices(_devices(tmp_path, body))
    body = _entry(rh_offset=Raw(literal))
    with pytest.raises(ConfigError, match="Alpha"):
        load_devices(_devices(tmp_path, body))


def test_device_errors_name_the_device_never_the_id(tmp_path: Path) -> None:
    device_id = "a1b2c3d4e5f6"
    bodies = [
        _entry(id=device_id, name="Kitchen-Probe", group="kitchen"),
        _entry(id=device_id, name="Kitchen-Probe", model="ht_gen2"),
        _entry(id=device_id, name="Kitchen-Probe", t_ofset=1),
        _entry(id=device_id, name="Kitchen-Probe", max_silence_s=0),
        _entry(id=device_id, name="Kitchen-Probe") + _entry(id=device_id, name="Other"),
        _entry(id=device_id, name="Kitchen-Probe") + _entry(id="ddeeff", name="Kitchen-Probe"),
    ]
    for body in bodies:
        with pytest.raises(ConfigError) as exc:
            load_devices(_devices(tmp_path, body))
        assert device_id not in str(exc.value)
        assert "Kitchen-Probe" in str(exc.value) or "Other" in str(exc.value)


def test_bad_id_error_never_echoes_the_id(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_devices(_devices(tmp_path, _entry(id="secret-ish-xyz")))
    assert "secret-ish-xyz" not in str(exc.value)
    assert "Alpha" in str(exc.value)


def test_nameless_device_falls_back_to_index(tmp_path: Path) -> None:
    body = '[[device]]\nid = "aabbcc"\ngroup = "subfloor"\nmodel = "ht_gen1"\n'
    with pytest.raises(ConfigError, match="#1"):
        load_devices(_devices(tmp_path, body))


def test_valid_offsets_and_silence(tmp_path: Path) -> None:
    body = _entry(t_offset=-0.4, rh_offset=2, max_silence_s=54000)
    (device,) = load_devices(_devices(tmp_path, body))
    assert (device.t_offset, device.rh_offset, device.max_silence_s) == (-0.4, 2.0, 54000)


def test_flood_without_rh_offset_loads(tmp_path: Path) -> None:
    (device,) = load_devices(_devices(tmp_path, _entry(model="flood_gen1")))
    assert device.model == "flood_gen1"


# --- committed example files ----------------------------------------------------------


def test_devices_example_loads() -> None:
    devices = load_devices(REPO / "devices.example.toml")
    assert [d.name for d in devices] == [
        "LR-1",
        "LR-2",
        "LR-3",
        "LR-4",
        "LR-Flood",
        "Work",
        "Bedroom",
    ]
    assert [d.model for d in devices] == [
        "ht_gen1",
        "ht_gen1",
        "ht_gen1",
        "ht_gen3",
        "flood_gen1",
        "ht_gen1",
        "ht_gen1",
    ]
    assert [d.group for d in devices] == ["subfloor"] * 5 + ["reference"] * 2
    assert all(d.max_silence_s for d in devices)
    assert all(re.fullmatch(r"02000000000[1-7]", d.id) for d in devices)


def test_env_example_lists_every_variable() -> None:
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    assignments = {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    source = (REPO / "src" / "shelog" / "config.py").read_text(encoding="utf-8")
    read_by_app = set(re.findall(r'"((?:SHELLY|SHELOG)_[A-Z_]+)"', source))
    assert read_by_app, "config.py reads no variables?"
    assert read_by_app <= set(assignments)
    assert assignments["SHELLY_AUTH_KEY"] == ""
    assert assignments["SHELLY_HOST"] == ""
