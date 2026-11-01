"""The auth key must not appear in logs, CLI output, the database or files under data/ (COLL-07)."""

from __future__ import annotations

import io
import logging
import os
import re
import sqlite3
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import pytest

from shelog import check, cli
from shelog.config import Device, Secret, Settings
from shelog.db import connect_rw, migrate, set_meta
from shelog.logging_setup import RedactingFormatter, setup_logging

SRC = Path(__file__).resolve().parent.parent / "src" / "shelog"


def _forms(secret: str) -> tuple[str, str, str]:
    return secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)


@pytest.fixture
def awkward_key() -> str:
    # Contains characters whose quote() and quote_plus() forms differ from the raw form
    # and from each other.
    return "KEY with/space+plus&amp=0123456789"


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    saved = {n: logging.getLogger(n).level for n in ("httpx", "httpcore", "urllib3")}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, lvl in saved.items():
        logging.getLogger(name).setLevel(lvl)


# --- redacting formatter ---------------------------------------------------------------


def test_every_leak_path_is_scrubbed_on_stderr(
    capfd: pytest.CaptureFixture[str], awkward_key: str
) -> None:
    setup_logging("DEBUG", [awkward_key])
    log = logging.getLogger("shelog.test")
    raw, quoted, plus = _forms(awkward_key)
    log.info("message with %s inside", raw)
    log.info("arg form %s", quoted)
    log.info("plus form %s", plus)
    log.info("fmt %(k)s", {"k": raw})
    try:
        raise ValueError(f"upstream said {raw} and {quoted}")
    except ValueError:
        log.exception("it failed")
    log.warning("with stack", stack_info=True)
    err = capfd.readouterr().err
    assert "***" in err
    for form in (raw, quoted, plus):
        assert form not in err
    assert "Traceback" in err  # the traceback is still there, just scrubbed
    first = err.splitlines()[0]
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ", first)


def test_http_libraries_are_quiet_and_single_handler(awkward_key: str) -> None:
    setup_logging("DEBUG", [awkward_key])
    setup_logging("INFO", [awkward_key])
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, RedactingFormatter)
    assert root.level == logging.INFO
    for name in ("httpx", "httpcore", "urllib3"):
        assert logging.getLogger(name).level == logging.WARNING


def test_formatter_ignores_empty_secrets() -> None:
    fmt = RedactingFormatter(["", "abcdefghijklmnop"])
    record = logging.LogRecord("n", logging.INFO, __file__, 1, "x abcdefghijklmnop y", None, None)
    assert fmt.format(record).endswith("x *** y")


def test_secret_never_renders_its_value() -> None:
    secret = Secret("super-secret-value-123")
    for rendered in (str(secret), repr(secret), format(secret), f"{secret}", f"{secret!r}"):
        assert rendered == "***"
    assert secret.reveal() == "super-secret-value-123"


# --- shelog check ------------------------------------------------------------------------


def _scan(settings: Settings, devices: tuple[Device, ...], data_dir: Path) -> tuple[int, str]:
    out = io.StringIO()
    code = check.run_check(settings, devices, data_dir=data_dir, stdout=out)
    return code, out.getvalue()


def test_clean_data_dir_reports_zero_hits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Settings,
    test_devices: tuple[Device, ...],
    sentinel_key: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    data = test_settings.db_path.parent
    con = connect_rw(test_settings.db_path)
    migrate(con)
    con.close()
    (data / "probe").mkdir()
    (data / "probe" / "index.json").write_text('{"devices": 3}', encoding="utf-8")
    code, text = _scan(test_settings, test_devices, data)
    assert code == 0
    lines = text.strip().splitlines()
    assert len(lines) == 1
    assert "config: ok" in lines[0]
    assert f"devices: {len(test_devices)}" in lines[0]
    assert "db schema: v1" in lines[0]
    assert "0 hits" in lines[0]
    assert re.search(r"leak scan: \d+ files", lines[0])
    for form in _forms(sentinel_key):
        assert form not in text


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_file_fails_the_scan_instead_of_counting_as_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Settings,
    test_devices: tuple[Device, ...],
    sentinel_key: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    data = test_settings.db_path.parent
    con = connect_rw(test_settings.db_path)
    migrate(con)
    con.close()
    locked = data / "locked.log"
    locked.write_text("nothing to see", encoding="utf-8")
    locked.chmod(0o000)
    try:
        code, text = _scan(test_settings, test_devices, data)
    finally:
        locked.chmod(0o600)
    assert code == 1
    assert "0 hits, 1 unreadable" in text
    assert "unreadable: " in text
    assert "locked.log" in text
    for form in _forms(sentinel_key):
        assert form not in text


def test_planted_log_and_wal_hits_are_found_and_not_echoed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Settings,
    test_devices: tuple[Device, ...],
    sentinel_key: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    data = test_settings.db_path.parent
    con = connect_rw(test_settings.db_path)
    migrate(con)
    con.execute("PRAGMA wal_autocheckpoint = 0")
    set_meta(con, "planted", sentinel_key)  # stays in the -wal file while con is open
    wal = Path(f"{test_settings.db_path}-wal")
    assert wal.exists() and sentinel_key.encode() in wal.read_bytes()
    (data / "poll.log").write_text(f"oops {sentinel_key} oops\n", encoding="utf-8")
    (data / "split.log").write_text(
        "x" * (1024 * 1024 - 5) + urllib.parse.quote_plus(sentinel_key) + "tail",
        encoding="utf-8",
    )
    try:
        code, text = _scan(test_settings, test_devices, data)
    finally:
        con.close()
    assert code == 1
    assert "hit: data/poll.log" in text
    assert "hit: data/split.log" in text
    assert "hit: data/shelog.db-wal" in text
    assert "hit: data/probe" not in text
    assert "hits" in text.splitlines()[0]
    for form in _forms(sentinel_key):
        assert form not in text


def test_missing_db_is_reported_and_not_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Settings,
    test_devices: tuple[Device, ...],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert not test_settings.db_path.exists()
    code, text = _scan(test_settings, test_devices, tmp_path / "data")
    assert code == 0
    assert "db schema: missing" in text
    assert not test_settings.db_path.exists()
    assert not test_settings.db_path.parent.exists()


def test_check_does_not_modify_an_existing_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Settings,
    test_devices: tuple[Device, ...],
) -> None:
    monkeypatch.chdir(tmp_path)
    con = connect_rw(test_settings.db_path)
    migrate(con)
    con.close()
    before = test_settings.db_path.read_bytes()
    _scan(test_settings, test_devices, test_settings.db_path.parent)
    assert test_settings.db_path.read_bytes() == before
    check_con = sqlite3.connect(test_settings.db_path)
    try:
        assert check_con.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        check_con.close()


# --- CLI ----------------------------------------------------------------------------------


def test_cli_check_without_config_exits_2_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("SHELLY_AUTH_KEY", "SHELLY_HOST", "SHELOG_DEVICES_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["check"]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("config error:")
    assert "Traceback" not in captured.err + captured.out


def test_cli_check_runs_from_a_valid_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sentinel_key: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SHELOG_DEVICES_FILE", raising=False)  # the owner scan run sets it
    monkeypatch.setenv("SHELLY_AUTH_KEY", sentinel_key)
    monkeypatch.setenv("SHELLY_HOST", "shelly-00-eu.shelly.cloud")
    (tmp_path / "devices.toml").write_text(
        '[[device]]\nid = "aabbcc"\nname = "A"\ngroup = "subfloor"\nmodel = "ht_gen1"\n',
        encoding="utf-8",
    )
    assert cli.main(["check"]) == 0
    out = capsys.readouterr().out
    assert "config: ok | devices: 1 | db schema: missing" in out
    assert sentinel_key not in out


# --- source scan --------------------------------------------------------------------------

_EXC_NAMES = r"(?:e|ex|exc|err|error|exception)"
_STR_OF_EXC = re.compile(rf"\bstr\(\s*{_EXC_NAMES}\s*\)")
_FSTRING_EXC = re.compile(rf"\{{\s*{_EXC_NAMES}\s*(?:![rsa])?\s*(?::[^}}]*)?\}}")
_PERCENT_EXC = re.compile(rf"%[sr].*,\s*{_EXC_NAMES}\s*\)")
_LOG_OR_PRINT = re.compile(r"\b(?:log|logger|logging|print)\w*\b")


def _offending_lines() -> list[str]:
    bad: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            hit = _STR_OF_EXC.search(line) or _FSTRING_EXC.search(line)
            if not hit and _LOG_OR_PRINT.search(line):
                hit = _PERCENT_EXC.search(line)
            if hit:
                bad.append(f"{path.relative_to(SRC)}:{number}: {stripped}")
    return bad


def test_no_log_or_print_call_passes_an_exception_text() -> None:
    assert _offending_lines() == []


def test_source_scan_pattern_catches_the_known_bad_forms() -> None:
    bad_forms = [
        'log.error(f"failed: {e}")',
        'print(f"failed: {exc}", file=sys.stderr)',
        'log.warning("failed: " + str(err))',
        'log.error("failed: %s", err)',
        'log.error("failed: %s", exc)',
    ]
    good_forms = [
        'log.error("failed: %s", type(err).__name__)',
        'print(f"config error: {err.args[0]}")',
        'log.info("saw %s devices", count)',
    ]

    def flagged(line: str) -> bool:
        return bool(
            _STR_OF_EXC.search(line)
            or _FSTRING_EXC.search(line)
            or (_LOG_OR_PRINT.search(line) and _PERCENT_EXC.search(line))
        )

    assert all(flagged(line) for line in bad_forms)
    assert not any(flagged(line) for line in good_forms)
