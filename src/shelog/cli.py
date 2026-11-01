"""Command line entry point: `shelog probe`, `shelog poll [--once]` and `shelog check`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from shelog import check, poller, probe
from shelog.config import ConfigError, load_devices, load_settings
from shelog.logging_setup import setup_logging


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shelog")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "probe", help="query the Shelly Cloud once and summarize (raw dump to data/probe)"
    )
    poll = sub.add_parser("poll", help="poll the Shelly Cloud and store readings in SQLite")
    poll.add_argument("--once", action="store_true", help="run a single cycle and exit")
    sub.add_parser(
        "check", help="scan local files for the auth key and summarize the configuration"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings()
        devices = load_devices(settings.devices_file)
    except ConfigError as err:
        # ConfigError messages name variables or devices only, never values.
        print(f"config error: {err.args[0]}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level, [settings.auth_key.reveal()])
    if args.command == "probe":
        return probe.main(settings, devices)
    if args.command == "check":
        return check.main(settings, devices)
    if args.command == "poll":
        return poller.main(settings, devices, once=args.once)
    return 2  # unreachable: argparse enforces a known subcommand
