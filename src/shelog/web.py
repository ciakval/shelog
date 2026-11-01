"""Public read-only web service (`shelog serve`).

This process never sees the Shelly key: it reads only SHELOG_* settings and the device
inventory, opens the poller's SQLite database per request with ``query_only`` and serves
coarse JSON. Responses carry device NAMES, never ids, MACs, coordinates or error text.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from shelog import api
from shelog.config import Device

log = logging.getLogger(__name__)

DEFAULT_MAX_SILENCE_S = 54_000
POLL_STALE_S = 15 * 60  # no successful poll for this long = collection is down
BATTERY_LOW_PCT = 20.0
CACHE = {"Cache-Control": "public, max-age=30"}
STATIC_DIR = Path(__file__).parent / "static"


@dataclass(frozen=True)
class WebSettings:
    db_path: Path
    devices_file: Path
    log_level: str


def load_web_settings(environ: dict[str, str] | None = None) -> WebSettings:
    env = os.environ if environ is None else environ
    return WebSettings(
        db_path=Path(env.get("SHELOG_DB_PATH", "data/shelog.db")),
        devices_file=Path(env.get("SHELOG_DEVICES_FILE", "devices.toml")),
        log_level=(env.get("SHELOG_LOG_LEVEL", "INFO").strip().upper() or "INFO"),
    )


def open_ro(path: Path) -> sqlite3.Connection | None:
    """Open the existing DB for reading (rw-capable for WAL, but query_only). None if absent."""
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=5, isolation_level=None)
    con.execute("PRAGMA busy_timeout = 5000")
    con.execute("PRAGMA query_only = ON")
    return con


def health(con: sqlite3.Connection | None, devices: tuple[Device, ...], now: int) -> dict[str, Any]:
    """Coarse collection status. Never includes ids, error text or raw payloads."""
    if con is None:
        return {
            "status": "starting",
            "now": now,
            "last_ok_poll": None,
            "auth": "unknown",
            "devices": [],
        }
    row = con.execute("SELECT MAX(ts) FROM poll_log WHERE kind='shelly' AND ok=1").fetchone()
    last_ok = row[0] if row else None
    auth_row = con.execute("SELECT value FROM meta WHERE key='shelly_auth'").fetchone()
    auth = auth_row[0] if auth_row and auth_row[0] in ("ok", "rejected") else "unknown"
    last: dict[str, tuple[int, float | None]] = {}
    for device_id, ts, battery in con.execute(
        "SELECT r.device_id, r.ts, r.battery FROM reading r"
        " JOIN (SELECT device_id, MAX(ts) AS ts FROM reading GROUP BY device_id) m"
        " ON r.device_id = m.device_id AND r.ts = m.ts"
    ):
        last[device_id] = (ts, battery)
    out_devices = []
    for dev in devices:
        ts, battery = last.get(dev.id, (None, None))
        limit = dev.max_silence_s or DEFAULT_MAX_SILENCE_S
        out_devices.append(
            {
                "name": dev.name,
                "group": dev.group,
                "last_report": ts,
                "silent": ts is None or now - ts > limit,
                "battery_low": battery is not None and battery < BATTERY_LOW_PCT,
            }
        )
    if auth == "rejected" or last_ok is None or now - last_ok > POLL_STALE_S:
        status = "down"
    elif any(d["silent"] or d["battery_low"] for d in out_devices):
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "now": now,
        "last_ok_poll": last_ok,
        "auth": auth,
        "devices": out_devices,
    }


def create_app(
    settings: WebSettings,
    devices: tuple[Device, ...],
    now: Callable[[], float] = time.time,
    extra_routes: list[Any] | None = None,
) -> Starlette:
    def _with_db(fn: Callable[[sqlite3.Connection | None], Any]) -> Any:
        con = open_ro(settings.db_path)
        try:
            return fn(con)
        finally:
            if con is not None:
                con.close()

    def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    def api_health(request: Request) -> Response:
        body = _with_db(lambda con: health(con, devices, int(now())))
        return JSONResponse(body, headers=CACHE)

    def _api(build: Callable[[sqlite3.Connection | None, dict[str, str]], Any]):
        def endpoint(request: Request) -> Response:
            params = dict(request.query_params)
            try:
                body = _with_db(lambda con: build(con, params))
            except api.BadRequest as err:
                return JSONResponse({"error": err.args[0]}, status_code=400)
            return JSONResponse(body, headers=CACHE)

        return endpoint

    def api_devices(request: Request) -> Response:
        return JSONResponse(api.devices_body(devices), headers=CACHE)

    def export_csv(request: Request) -> Response:
        params = dict(request.query_params)
        try:
            body = _with_db(lambda con: api.readings_body(con, devices, params, int(now())))
        except api.BadRequest as err:
            return JSONResponse({"error": err.args[0]}, status_code=400)
        return Response(
            api.readings_csv(body),
            media_type="text/csv",
            headers={**CACHE, "Content-Disposition": 'attachment; filename="shelog.csv"'},
        )

    async def on_error(request: Request, exc: Exception) -> Response:
        log.error("request failed: %s", type(exc).__name__)
        return JSONResponse({"error": "internal"}, status_code=500)

    routes: list[Any] = [
        Route("/healthz", healthz),
        Route("/api/health", api_health),
        Route("/api/devices", api_devices),
        Route("/api/readings", _api(lambda c, p: api.readings_body(c, devices, p, int(now())))),
        Route("/api/stats", _api(lambda c, p: api.stats_body(c, devices, p, int(now())))),
        Route("/api/outdoor", _api(lambda c, p: api.outdoor_body(c, p, int(now())))),
        Route("/api/export.csv", export_csv),
        *(extra_routes or []),
    ]
    if STATIC_DIR.is_dir():
        routes.append(Mount("/", StaticFiles(directory=STATIC_DIR, html=True)))
    return Starlette(routes=routes, exception_handlers={Exception: on_error})


def main(host: str, port: int) -> int:
    import uvicorn

    from shelog.config import ConfigError, load_devices

    settings = load_web_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    try:
        devices = load_devices(settings.devices_file)
    except ConfigError as err:
        log.error("config error: %s", err.args[0])
        return 2
    app = create_app(settings, devices)
    uvicorn.run(
        app,
        host=host,
        port=port,
        workers=1,
        log_level="warning",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0
