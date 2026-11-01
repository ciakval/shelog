"""Injectable clock so scheduling and rate limiting are testable without sleeping."""

from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def time(self) -> float: ...

    def wait(self, stop: threading.Event, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds; return True when ``stop`` is set."""
        ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def wait(self, stop: threading.Event, timeout: float) -> bool:
        return stop.wait(timeout)
