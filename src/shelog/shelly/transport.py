"""urllib POST seam. The only module in the package that touches sockets.

Errors carry a coarse category only: urllib exception text can embed URLs, so it is
never propagated (``raise ... from None``).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MAX_BODY = 1_048_576
_CHUNK = 16_384


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: dict[str, str]  # lower-cased names
    body: bytes


class TransportError(Exception):
    """Transport failure. ``category`` is 'timeout' or 'network'; no original text kept."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: a key-bearing body must not be re-sent elsewhere."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _lower_headers(headers: Any) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _read_capped(resp: Any, deadline: float) -> bytes:
    """Read at most MAX_BODY + 1 bytes, giving up once the wall-clock deadline has passed."""
    parts: list[bytes] = []
    remaining = MAX_BODY + 1
    read = getattr(resp, "read1", resp.read)  # read1 returns after one socket read
    while remaining > 0:
        if time.monotonic() >= deadline:
            raise TimeoutError
        chunk = read(min(_CHUNK, remaining))
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def _send_blocking(req: urllib.request.Request, timeout: float, deadline: float) -> RawResponse:
    try:
        # https is enforced by config (normalize_host) and by the client building the URL.
        with _OPENER.open(req, timeout=timeout) as resp:
            return RawResponse(
                resp.status, _lower_headers(resp.headers), _read_capped(resp, deadline)
            )
    except urllib.error.HTTPError as err:
        try:
            return RawResponse(err.code, _lower_headers(err.headers), _read_capped(err, deadline))
        finally:
            err.close()
    except TimeoutError:
        raise TransportError("timeout") from None
    except urllib.error.URLError as err:
        if isinstance(err.reason, TimeoutError):
            raise TransportError("timeout") from None
        raise TransportError("network") from None
    except OSError:
        raise TransportError("network") from None


def send(
    url: str,
    *,
    form: Mapping[str, str] | None = None,
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float,
) -> RawResponse:
    """POST ``form`` (urlencoded) or ``json_body`` (JSON) and return the raw response.

    ``timeout`` bounds the WHOLE exchange (DNS, connect, headers and body), not each socket
    operation: the request runs in a daemon worker and the caller stops waiting at the
    deadline, so a hung resolver or a slow-drip body cannot stall a poll cycle (or shutdown)
    beyond ``timeout``. An abandoned worker ends itself at its next chunk or socket timeout.
    """
    out_headers = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        out_headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        out_headers["Content-Type"] = "application/json"
    else:
        data = b""
    req = urllib.request.Request(url, data=data, headers=out_headers, method="POST")  # noqa: S310
    deadline = time.monotonic() + timeout
    box: list[RawResponse | BaseException] = []

    def work() -> None:
        try:
            box.append(_send_blocking(req, timeout, deadline))
        except BaseException as err:  # handed to the caller below
            box.append(err)

    worker = threading.Thread(target=work, name="shelly-send", daemon=True)
    worker.start()
    worker.join(timeout)
    if not box:
        raise TransportError("timeout")
    result = box[0]
    if isinstance(result, BaseException):
        raise result
    return result
