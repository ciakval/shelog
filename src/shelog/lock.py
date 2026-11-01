"""Single-instance lock: one poll/probe process per data directory."""

from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO


class AlreadyRunning(Exception):
    """Another process holds the instance lock on this data directory."""


def acquire_instance_lock(path: Path) -> IO[str]:
    """Take an exclusive non-blocking flock on ``path``.

    The caller keeps the returned handle open for the process lifetime; closing it (or the
    process dying) releases the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise AlreadyRunning from None
    except BaseException:
        handle.close()
        raise
    return handle
