"""`shelog check`: prove the auth key left no trace in local files (COLL-07, D-03).

Loads the real key through the normal config path, scans the database (+WAL/SHM) and every
file under data/ for the key in raw and URL-quoted form, and prints only counts and file
names. The key, any part of it and its length are never printed.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

from shelog.config import Device, Settings

_CHUNK = 1024 * 1024


def _needles(key: str) -> tuple[bytes, ...]:
    forms = {key, urllib.parse.quote(key, safe=""), urllib.parse.quote_plus(key)}
    return tuple(sorted((f.encode("utf-8") for f in forms if f), key=len, reverse=True))


def _targets(settings: Settings, data_dir: Path) -> list[Path]:
    found: dict[Path, Path] = {}

    def add(path: Path) -> None:
        if path.is_file() and not path.is_symlink():
            found.setdefault(path.resolve(), path)

    db = settings.db_path
    for candidate in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        add(candidate)
    if data_dir.is_dir():
        for path in _walk(data_dir):
            add(path)
    return sorted(found.values())


def _walk(root: Path) -> Iterator[Path]:
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            yield Path(dirpath) / name


def _contains(path: Path, needles: tuple[bytes, ...]) -> bool:
    """Chunked search whose chunks overlap by the longest needle, so splits are caught."""
    overlap = max(len(n) for n in needles) - 1
    carry = b""
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                window = carry + chunk
                if any(n in window for n in needles):
                    return True
                carry = window[-overlap:] if overlap else b""
    except OSError:
        return False
    return False


def _schema_version(db_path: Path) -> str:
    if not db_path.is_file():
        return "missing"
    uri = f"file:{urllib.parse.quote(str(db_path.resolve()))}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            return f"v{int(con.execute('PRAGMA user_version').fetchone()[0])}"
        finally:
            con.close()
    except sqlite3.Error:
        return "unreadable"


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def run_check(
    settings: Settings,
    devices: tuple[Device, ...],
    *,
    data_dir: Path = Path("data"),
    stdout: TextIO | None = None,
) -> int:
    """Print one summary line and one `hit:` line per leaking file. 0 = clean, 1 = hits."""
    out = sys.stdout if stdout is None else stdout  # resolved per call so capture works
    needles = _needles(settings.auth_key.reveal())
    targets = _targets(settings, data_dir)
    hits = [path for path in targets if _contains(path, needles)]
    schema = _schema_version(settings.db_path)
    print(
        f"config: ok | devices: {len(devices)} | db schema: {schema}"
        f" | leak scan: {len(targets)} files, {len(hits)} hits",
        file=out,
    )
    for path in hits:
        print(f"hit: {_display(path)}", file=out)
    return 1 if hits else 0


def main(settings: Settings, devices: tuple[Device, ...]) -> int:
    return run_check(settings, devices)
