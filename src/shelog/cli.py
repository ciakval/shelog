"""Command line entry point: `shelog probe|poll|serve|check|...`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from shelog import check, poller, probe
from shelog.config import ConfigError, load_devices, load_settings
from shelog.logging_setup import setup_logging


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shelog")
    sub = parser.add_subparsers(dest="command", required=True)
    probe_parser = sub.add_parser(
        "probe", help="query the Shelly Cloud and summarize (raw dump to data/probe)"
    )
    probe_parser.add_argument(
        "--errors",
        action="store_true",
        help="also capture error envelopes (wrong key, unknown id, key placements, bad route)",
    )
    probe_parser.add_argument(
        "--wrong-host",
        action="store_true",
        help="also send one request with a dummy key to a sibling regional host",
    )
    probe_parser.add_argument(
        "--save-fixtures",
        type=Path,
        metavar="DIR",
        help="write sanitized fixtures (one shared pseudonym mapping) to DIR, or nothing",
    )
    poll = sub.add_parser("poll", help="poll the Shelly Cloud and store readings in SQLite")
    poll.add_argument("--once", action="store_true", help="run a single cycle and exit")
    serve = sub.add_parser("serve", help="serve the public read-only web page and JSON API")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104  # container listens on all
    serve.add_argument("--port", type=int, default=8000)
    bk = sub.add_parser("backup", help="write a consistent, verified snapshot of the database")
    group = bk.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path, metavar="FILE", help="write the snapshot here")
    group.add_argument("--verify", type=Path, metavar="FILE", help="verify an existing snapshot")
    imp = sub.add_parser("import", help="merge another shelog database into this one")
    imp.add_argument("source", type=Path)
    sub.add_parser(
        "check", help="scan local files for the auth key and summarize the configuration"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "serve":
        # The public web process never loads the Shelly key (it is not in its environment).
        from shelog import web

        return web.main(args.host, args.port)
    if args.command in ("backup", "import"):
        return _db_command(args)
    try:
        settings = load_settings()
        devices = load_devices(settings.devices_file)
    except ConfigError as err:
        # ConfigError messages name variables or devices only, never values.
        print(f"config error: {err.args[0]}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level, [settings.auth_key.reveal()])
    if args.command == "probe":
        return probe.main(
            settings,
            devices,
            errors=args.errors,
            wrong_host=args.wrong_host,
            save_fixtures=args.save_fixtures,
        )
    if args.command == "check":
        return check.main(settings, devices)
    if args.command == "poll":
        return poller.main(settings, devices, once=args.once)
    return 2  # unreachable: argparse enforces a known subcommand


def _db_path() -> Path:
    """DB path without loading (or needing) the Shelly settings."""
    import os

    from shelog.config import read_dotenv

    value = os.environ.get("SHELOG_DB_PATH") or read_dotenv(Path(".env")).get("SHELOG_DB_PATH")
    return Path(value or "data/shelog.db")


def _db_command(args: argparse.Namespace) -> int:
    from shelog import backup, importer
    from shelog.db import connect_rw, migrate

    if args.command == "backup" and args.verify is not None:
        try:
            count = backup.verify(args.verify)
        except backup.BackupError as err:
            print(f"backup invalid: {err.args[0]}", file=sys.stderr)
            return 1
        print(f"backup ok: integrity_check passed, {count} readings")
        return 0
    con = connect_rw(_db_path())
    try:
        migrate(con)
        if args.command == "backup":
            count = backup.snapshot(con, args.out)
            print(f"backup written and verified: {count} readings")
            return 0
        try:
            result = importer.import_db(con, args.source)
        except FileNotFoundError:
            print("import failed: source database not found", file=sys.stderr)
            return 1
        print(
            f"imported readings={result.readings} raw_status={result.raw_status}"
            f" poll_log={result.poll_log} already_present={result.skipped_readings}"
        )
        return 0
    finally:
        con.close()
