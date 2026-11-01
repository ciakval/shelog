"""The urllib adapter against a real loopback HTTP server (no fakes)."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import SENTINEL_KEY

from shelog.shelly import transport
from shelog.shelly.transport import MAX_BODY, TransportError, send


class _Handler(BaseHTTPRequestHandler):
    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.requests.append(  # type: ignore[attr-defined]
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )
        if self.path == "/ok":
            payload = b'{"isok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/429":
            self.send_response(429)
            self.send_header("Retry-After", "7")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/redirect":
            self.send_response(307)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(1.5)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/drip":
            # Headers at once, then one byte every 0.3 s: no single socket read ever times out.
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            for _ in range(100):
                try:
                    self.wfile.write(b"a")
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.3)
        elif self.path == "/garbage":
            self.wfile.write(b"this is not http\r\n\r\n")
            self.wfile.flush()
            self.close_connection = True
        elif self.path == "/big":
            payload = b"a" * (MAX_BODY + 100)
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    do_POST = _handle

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _url(srv: ThreadingHTTPServer, path: str) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def test_ok_response(server):
    raw = send(_url(server, "/ok"), form={"auth_key": SENTINEL_KEY}, timeout=5)
    assert raw.status == 200
    assert json.loads(raw.body) == {"isok": True}
    assert raw.headers["content-type"] == "application/json"


def test_key_is_only_in_the_body_never_path_or_headers(server):
    send(_url(server, "/ok"), form={"auth_key": SENTINEL_KEY}, timeout=5)
    (req,) = server.requests
    assert req["path"] == "/ok"
    assert SENTINEL_KEY.encode() in req["body"]
    assert SENTINEL_KEY not in json.dumps(req["headers"])
    assert req["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_429_surfaces_status_and_lowercased_retry_after(server):
    raw = send(_url(server, "/429"), timeout=5)
    assert raw.status == 429
    assert raw.headers["retry-after"] == "7"


def test_redirect_is_not_followed(server):
    raw = send(_url(server, "/redirect"), form={"auth_key": SENTINEL_KEY}, timeout=5)
    assert raw.status == 307
    assert len(server.requests) == 1


def test_timeout_maps_to_timeout(server):
    with pytest.raises(TransportError) as info:
        send(_url(server, "/slow"), timeout=0.5)
    assert info.value.category == "timeout"


def test_timeout_is_a_wall_clock_deadline_for_a_slow_drip_body(server):
    started = time.monotonic()
    with pytest.raises(TransportError) as info:
        send(_url(server, "/drip"), timeout=1.0)
    elapsed = time.monotonic() - started
    assert info.value.category == "timeout"
    assert elapsed < 2.5


def test_timeout_bounds_a_connection_that_never_answers():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)  # accepts the TCP handshake, never replies
        port = sock.getsockname()[1]
        started = time.monotonic()
        with pytest.raises(TransportError) as info:
            send(f"http://127.0.0.1:{port}/x", form={"auth_key": SENTINEL_KEY}, timeout=0.7)
        assert info.value.category == "timeout"
        assert time.monotonic() - started < 2.5


def test_malformed_http_maps_to_network_not_an_escaping_exception(server):
    with pytest.raises(TransportError) as info:
        send(_url(server, "/garbage"), form={"auth_key": SENTINEL_KEY}, timeout=5)
    assert info.value.category == "network"
    assert SENTINEL_KEY not in repr(info.value)


def test_body_is_capped_at_one_byte_over_max(server):
    raw = send(_url(server, "/big"), timeout=10)
    assert len(raw.body) == MAX_BODY + 1


def test_closed_port_maps_to_network():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(TransportError) as info:
        send(f"http://127.0.0.1:{port}/ok", form={"auth_key": SENTINEL_KEY}, timeout=2)
    assert info.value.category == "network"


def test_errors_carry_only_the_category_and_no_cause(server):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    errors = []
    for url, timeout in ((f"http://127.0.0.1:{port}/ok", 2), (_url(server, "/slow"), 0.5)):
        with pytest.raises(TransportError) as info:
            send(url, form={"auth_key": SENTINEL_KEY}, timeout=timeout)
        errors.append(info.value)
    for err in errors:
        assert str(err) == err.category
        assert SENTINEL_KEY not in repr(err)
        assert err.__cause__ is None


def test_json_body_sets_content_type(server):
    send(_url(server, "/ok"), json_body={"a": 1}, timeout=5)
    (req,) = server.requests
    assert req["headers"]["Content-Type"] == "application/json"
    assert json.loads(req["body"]) == {"a": 1}


def test_no_redirect_handler_is_installed_in_the_opener():
    assert any(isinstance(h, transport._NoRedirect) for h in transport._OPENER.handlers)
