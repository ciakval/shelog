"""Logging with the auth key scrubbed from every emitted line (COLL-07, D-05).

Redaction happens on the fully formatted record, so messages, %-arguments, exception
tracebacks and stack_info are all covered regardless of which handler runs first.
"""

from __future__ import annotations

import logging
import sys
import time
import urllib.parse
from collections.abc import Iterable

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%SZ"
_QUIET_LOGGERS = ("httpx", "httpcore", "urllib3")
_MASK = "***"


def secret_forms(secrets: Iterable[str]) -> tuple[str, ...]:
    """The raw value plus its URL-quoted forms, longest first, empty values dropped."""
    forms: set[str] = set()
    for secret in secrets:
        if not secret:
            continue
        forms.update((secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)))
    return tuple(sorted(forms, key=len, reverse=True))


class RedactingFormatter(logging.Formatter):
    """Formatter that replaces every form of every secret with ``***``."""

    converter = staticmethod(time.gmtime)

    def __init__(self, secrets: Iterable[str], fmt: str | None = None) -> None:
        super().__init__(fmt or _FORMAT, datefmt=_DATEFMT)
        self._forms = secret_forms(secrets)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for form in self._forms:
            text = text.replace(form, _MASK)
        return text


def setup_logging(level: str, secrets: Iterable[str]) -> None:
    """Install ONE redacting stderr handler on the root logger."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
