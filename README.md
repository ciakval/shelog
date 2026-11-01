# shelog

Logs temperature and humidity from seven battery-powered Shelly sensors watching a drying
subfloor (plus outdoor weather) into SQLite, and shows the history at
<https://shelog.remesh.cz>. Background: `SPEC.md`; API findings: `docs/shelly-api.md`;
operations: `DEPLOYMENT.md`.

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run shelog poll [--once]        # needs the local secrets file (see .env.example) and devices.toml
uv run shelog serve                # web UI + JSON API on :8000, no key needed
uv run shelog backup --out FILE | --verify FILE
uv run shelog import FILE
uv run shelog alert-drill
```
