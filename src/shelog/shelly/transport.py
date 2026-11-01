"""urllib POST seam. The only module in the package that touches sockets.

Errors carry a coarse category only: urllib exception text can embed URLs, so it is
never propagated (``raise ... from None``).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MAX_BODY = 1_048_576


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


def send(
    url: str,
    *,
    form: Mapping[str, str] | None = None,
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float,
) -> RawResponse:
    """POST ``form`` (urlencoded) or ``json_body`` (JSON) and return the raw response."""
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
    try:
        # https is enforced by config (normalize_host) and by the client building the URL.
        with _OPENER.open(req, timeout=timeout) as resp:
            return RawResponse(resp.status, _lower_headers(resp.headers), resp.read(MAX_BODY + 1))
    except urllib.error.HTTPError as err:
        try:
            return RawResponse(err.code, _lower_headers(err.headers), err.read(MAX_BODY + 1))
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
