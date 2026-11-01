# Deployment

Production: `https://shelog.remesh.cz` on Bombur, deploy dir `/home/deploy/DOCKER/shelog`
(`compose.yaml`, `.env`, `devices.toml`, `data/`). A push to `main` runs lint + tests, builds
`ghcr.io/ciakval/shelog:<sha>` and deploys it via `ciakval/bombur-deploy`.

Two services from one image:

- `poller` (`shelog poll`): the only DB writer and the only holder of the Shelly key
  (`env_file: .env`). Also runs housekeeping every 5 min: ntfy alerts, healthchecks.io ping,
  daily snapshot to `data/backup/shelog-YYYYMMDD.db` (7 kept).
- `web` (`shelog serve`, container `shelog-web:8000`, behind Caddy): read-only
  (`query_only`), explicit environment with no secrets.

## Server `.env`

See `.env.example`. Required: `SHELLY_AUTH_KEY`, `SHELLY_HOST`, `APP_UID`/`APP_GID` (= `id deploy`).
Alerts: `NTFY_TOPIC` (random, subscribe in the ntfy app), optional `NTFY_SERVER`, `NTFY_TOKEN`,
`HEALTHCHECK_PING_URL`. Weather: `SHELOG_LAT`, `SHELOG_LON` (2 decimals).

```sh
ssh -t -p 10022 ciakval@bombur.remesh.cz sudo -u deploy nano /home/deploy/DOCKER/shelog/.env
# then apply:
ssh -p 10022 ciakval@bombur.remesh.cz "sudo -u deploy sh -c 'cd /home/deploy/DOCKER/shelog && docker compose up -d'"
```

## Everyday commands (on Bombur)

```sh
ssh -p 10022 ciakval@bombur.remesh.cz
D='cd /home/deploy/DOCKER/shelog &&'
sudo -u deploy sh -c "$D docker compose ps"
sudo -u deploy sh -c "$D docker compose logs -f --tail 100 poller"   # or web
sudo -u deploy sh -c "$D docker compose exec poller shelog alert-drill"
curl -s https://shelog.remesh.cz/api/health
```

## Add or change a device

Edit `devices.toml` locally (see `devices.example.toml`), copy it up, restart both services:

```sh
scp -P 10022 devices.toml ciakval@bombur.remesh.cz:/tmp/shelog-devices.toml
ssh -p 10022 ciakval@bombur.remesh.cz 'sudo install -o deploy -g deploy -m 600 /tmp/shelog-devices.toml /home/deploy/DOCKER/shelog/devices.toml && rm /tmp/shelog-devices.toml && sudo -u deploy sh -c "cd /home/deploy/DOCKER/shelog && docker compose restart"'
```

## Roll back

Re-run an older successful Actions run, or set `IMAGE_TAG=<older sha>` in `.env` and
`docker compose up -d`. Schema migrations only add, but an older image refuses a DB whose
schema is newer than it knows (it logs `SchemaTooNew` and retries): roll back the DB too then.

## Backups and restore

Daily snapshots live in `data/backup/` (consistent, `integrity_check`ed, mode 600). Bombur's
"copy the deploy dir" backup is safe because of them. Off-server copy:

```sh
ssh -p 10022 ciakval@bombur.remesh.cz 'sudo cat /home/deploy/DOCKER/shelog/data/backup/$(sudo ls /home/deploy/DOCKER/shelog/data/backup | tail -1)' > shelog-backup.db
uv run shelog backup --verify shelog-backup.db
```

Restore (stops collection briefly):

```sh
sudo -u deploy sh -c "$D docker compose stop poller web"
sudo -u deploy sh -c "$D mv data/shelog.db data/shelog.db.broken && rm -f data/shelog.db-wal data/shelog.db-shm && cp data/backup/shelog-YYYYMMDD.db data/shelog.db"
sudo -u deploy sh -c "$D docker compose up -d"
```

Readings since the snapshot are lost from the DB but the cloud still holds each device's latest
state; merge a newer copy with `shelog import <file>` if you have one.

## Merge another database

`shelog import <file>` merges readings, raw status and poll log from another shelog DB
(idempotent, never overwrites). Put the file in `data/` and run
`docker compose exec poller shelog import /app/data/<file>`.

## Re-onboarding

`compose.yaml` is server-owned: after changing it, re-run
`~/GIT/bombur-deploy/bin/bombur-onboard shelog 8000` (it also rotates the deploy key).
