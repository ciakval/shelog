"""Keep real identifiers and the configured key out of every committable file.

Three layers:
1. structural (always runs, CI-safe): the scanner over every committed fixture;
2. real ids (owner machine): no configured device id or its 6-hex tail in any tracked or
   committable file, .planning/ included;
3. configured key (owner machine): the auth key, loaded through the app's own config path,
   in raw, URL-quoted and quote_plus form, in no tracked or committable file.

SHELOG_REQUIRE_REAL_ID_SCAN=1 turns every skip in layers 2 and 3 into a failure.

Failure messages name files and device NAMES only. The key and the ids never appear in an
assert expression, a parameter id, a log call or an f-string.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote, quote_plus

import pytest

from shelog.config import ConfigError, load_settings
from shelog.sanitize import scan

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
REQUIRE_ENV = "SHELOG_REQUIRE_REAL_ID_SCAN"
DEVICES_ENV = "SHELOG_DEVICES_FILE"
DIGEST_RUN = 32  # a hex run this long is a digest (uv.lock hashes, git object ids)
_HEX_RUN = re.compile(rb"[0-9a-fA-F]+")


def _required() -> bool:
    return os.environ.get(REQUIRE_ENV) == "1"


def _skip_or_fail(reason: str) -> NoReturn:
    if _required():
        pytest.fail(f"{REQUIRE_ENV}=1 but: {reason}")
    pytest.skip(reason)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def committable_files(root: Path) -> list[Path]:
    """Tracked plus untracked-but-not-ignored files, via git, relative to root."""
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("git not available")
    result = subprocess.run(  # noqa: S603 - fixed argument vector, no shell
        [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    names = [n for n in result.stdout.split(b"\0") if n]
    return sorted({root / os.fsdecode(n) for n in names})


def _read(path: Path) -> bytes | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _in_digest_run(data: bytes, start: int, end: int) -> bool:
    """True if the match [start, end) lies inside a contiguous hex run of DIGEST_RUN+ chars."""
    left = start
    while left > 0 and chr(data[left - 1]) in "0123456789abcdefABCDEF":
        left -= 1
    right = end
    while right < len(data) and chr(data[right]) in "0123456789abcdefABCDEF":
        right += 1
    return right - left >= DIGEST_RUN


def find_id_hits(files: list[Path], devices: dict[str, str], root: Path) -> list[tuple[str, str]]:
    """(relative file, device NAME) for each file holding a device id or its 6-hex tail."""
    hits: set[tuple[str, str]] = set()
    for path in files:
        data = _read(path)
        if data is None:
            continue
        rel = str(path.relative_to(root))
        for device_id, name in devices.items():
            for needle in {device_id, device_id[-6:]}:
                pattern = re.compile(re.escape(needle.encode()), re.IGNORECASE)
                for match in pattern.finditer(data):
                    if not _in_digest_run(data, match.start(), match.end()):
                        hits.add((rel, name))
                        break
    return sorted(hits)


def key_needles(key: str) -> list[tuple[str, bytes]]:
    """(form name, needle) pairs, one per distinct encoding of the key."""
    seen: set[bytes] = set()
    needles: list[tuple[str, bytes]] = []
    for form, text in (
        ("raw", key),
        ("quote", quote(key, safe="")),
        ("quote_plus", quote_plus(key)),
    ):
        encoded = text.encode("utf-8")
        if encoded not in seen:
            seen.add(encoded)
            needles.append((form, encoded))
    return needles


def find_key_hits(files: list[Path], key: str, root: Path) -> list[tuple[str, str]]:
    """(relative file, form name) pairs. The key itself never leaves this function."""
    needles = key_needles(key)
    hits: list[tuple[str, str]] = []
    for path in files:
        data = _read(path)
        if data is None:
            continue
        rel = str(path.relative_to(root))
        hits.extend((rel, form) for form, needle in needles if needle in data)
    return sorted(hits)


def format_key_failure(hits: list[tuple[str, str]]) -> str:
    names = ", ".join(f"{rel} ({form})" for rel, form in hits)
    return f"configured auth key found {len(hits)} time(s) in committable files: {names}"


def config_dir() -> Path:
    """The directory the app runs in: parent of the devices file."""
    override = os.environ.get(DEVICES_ENV)
    return (Path(override) if override else REPO_ROOT / "devices.toml").parent


def devices_file() -> Path:
    override = os.environ.get(DEVICES_ENV)
    return Path(override) if override else REPO_ROOT / "devices.toml"


def _default_dotenv_name() -> Path:
    default = inspect.signature(load_settings).parameters["dotenv_path"].default
    assert isinstance(default, Path)
    return default


def _configured_key_hits(files: list[Path]) -> list[tuple[str, str]]:
    """Load the key through the app's config path and search; raises ConfigError if unset."""
    settings = load_settings(dotenv_path=config_dir() / _default_dotenv_name())
    return find_key_hits(files, settings.auth_key.reveal(), REPO_ROOT)


def _git_init(path: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    for args in (
        ["init", "-q"],
        ["add", "committed.txt"],
        [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "x",
        ],
    ):
        subprocess.run([git, *args], cwd=path, check=True, env=env, capture_output=True)  # noqa: S603


# ---------------------------------------------------------------------------
# Layer 1: structural
# ---------------------------------------------------------------------------


def _fixture_files() -> list[Path]:
    return sorted(p for pattern in ("*.json", "*.toml") for p in FIXTURE_DIR.rglob(pattern))


def test_fixture_files_exist() -> None:
    assert _fixture_files(), "no fixtures found, the structural layer would be a no-op"


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.name)
def test_fixture_passes_structural_scan(path: Path) -> None:
    findings = scan(path.read_text(encoding="utf-8"))
    assert findings == [], f"{path.name}: {findings}"


def test_scanner_self_test_flags_a_planted_real_looking_mac() -> None:
    planted = json.dumps({"mac": "ec:fa:bc:12:34:56"})
    assert scan(planted), "scan() no longer flags a real-looking MAC"


# ---------------------------------------------------------------------------
# Layer 2: real ids over every committable file
# ---------------------------------------------------------------------------


def _git_available_or_skip() -> None:
    if shutil.which("git") is None:
        _skip_or_fail("git not available")


def _listing_or_skip() -> list[Path]:
    _git_available_or_skip()
    try:
        return committable_files(REPO_ROOT)
    except (subprocess.CalledProcessError, OSError):
        _skip_or_fail("git file listing failed")


def test_no_configured_device_id_in_any_committable_file() -> None:
    path = devices_file()
    if not path.is_file():
        _skip_or_fail("devices.toml not present")
    with path.open("rb") as fh:
        entries = tomllib.load(fh).get("device", [])
    devices = {
        str(entry["id"]).strip().lower(): str(entry.get("name", "?"))
        for entry in entries
        if isinstance(entry, dict) and entry.get("id")
    }
    assert devices, "devices file has no device ids to scan for"
    files = _listing_or_skip()
    assert files, "git listed no committable files, the scan would be a no-op"
    hits = find_id_hits(files, devices, REPO_ROOT)
    if hits:
        names = ", ".join(f"{rel} (device {name})" for rel, name in hits)
        pytest.fail(f"configured device id found in committable files: {names}")


def test_real_id_layer_self_test_on_a_tmp_git_repo(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    planted = "7b3e9a41c0d2"  # fabricated for this test
    digest = "a" * 20 + planted + "b" * 20  # inside a 52-char hex run
    (tmp_path / "committed.txt").write_text(f"device id {planted}\n", encoding="utf-8")
    _git_init(tmp_path)
    (tmp_path / "untracked.md").write_text(f"tail {planted[-6:].upper()}\n", encoding="utf-8")
    (tmp_path / "digest.lock").write_text(digest + "\n", encoding="utf-8")
    (tmp_path / "mac.txt").write_text(f"mac 02{planted}\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text(planted, encoding="utf-8")

    files = committable_files(tmp_path)
    names = {p.name for p in files}
    assert {"committed.txt", "untracked.md"} <= names
    assert "ignored.txt" not in names
    hits = find_id_hits(files, {planted: "Fake"}, tmp_path)
    assert ("committed.txt", "Fake") in hits
    assert ("untracked.md", "Fake") in hits
    assert ("digest.lock", "Fake") not in hits
    # 02 + 12 hex = 14 hex chars: a MAC-shaped run, still reported
    assert ("mac.txt", "Fake") in hits


# ---------------------------------------------------------------------------
# Layer 3: configured key over every committable file
# ---------------------------------------------------------------------------


def run_key_layer(files: list[Path]) -> list[tuple[str, str]]:
    """Layer 3 outcome: hits, or a skip/fail when no key is configured."""
    try:
        return _configured_key_hits(files)
    except ConfigError as err:
        if _required():
            pytest.fail(f"{REQUIRE_ENV}=1 but the key is not loadable: {err}")
        pytest.skip("no auth key configured")


def test_configured_key_is_in_no_committable_file() -> None:
    hits = run_key_layer(_listing_or_skip())
    if hits:
        pytest.fail(format_key_failure(hits))


def test_key_layer_self_test_reports_each_form_and_never_the_key(tmp_path: Path) -> None:
    planted = "Zk+/ 9fQ=pL"  # fabricated; has characters that encode differently
    (tmp_path / "tracked.txt").write_text(
        "\n".join([planted, quote(planted, safe=""), quote_plus(planted)]) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "clean.txt").write_text("nothing here\n", encoding="utf-8")
    files = [tmp_path / "tracked.txt", tmp_path / "clean.txt"]
    hits = find_key_hits(files, planted, tmp_path)
    assert hits == [
        ("tracked.txt", "quote"),
        ("tracked.txt", "quote_plus"),
        ("tracked.txt", "raw"),
    ]
    flat = repr(hits) + format_key_failure(hits)
    for form in (planted, quote(planted, safe=""), quote_plus(planted)):
        assert form not in flat


def test_key_without_special_characters_is_reported_once(tmp_path: Path) -> None:
    (tmp_path / "t.txt").write_text("abcDEF123\n", encoding="utf-8")
    assert find_key_hits([tmp_path / "t.txt"], "abcDEF123", tmp_path) == [("t.txt", "raw")]


def test_key_layer_skips_when_no_key_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEVICES_ENV, str(tmp_path / "devices.toml"))
    monkeypatch.delenv("SHELLY_AUTH_KEY", raising=False)
    monkeypatch.delenv("SHELLY_HOST", raising=False)
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception, match="no auth key configured"):
        run_key_layer([])
    monkeypatch.setenv(REQUIRE_ENV, "1")
    with pytest.raises(pytest.fail.Exception, match="not loadable"):
        run_key_layer([])
