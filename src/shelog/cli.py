"""Command line entry point: `shelog probe` (plan 01-02 adds `poll`)."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence

from shelog import probe
from shelog.config import ConfigError, load_devices, load_settings


def _configure_logging(level_name: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level_name, logging.INFO))
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shelog")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "probe", help="query the Shelly Cloud once and summarize (raw dump to data/probe)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings()
        devices = load_devices(settings.devices_file)
    except ConfigError as err:
        print(f"config error: {err}", file=sys.stderr)
        return 2
    _configure_logging(settings.log_level)
    if args.command == "probe":
        return probe.main(settings, devices)
    return 2  # unreachable: argparse enforces a known subcommand
