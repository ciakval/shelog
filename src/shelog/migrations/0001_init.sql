-- 0001_init.sql: complete v1 schema. Applied by db.migrate() inside one transaction;
-- user_version is bumped by the runner, not by this file.

CREATE TABLE poll_log (
  id            INTEGER PRIMARY KEY,
  ts            INTEGER NOT NULL CHECK (ts > 0),      -- attempt start, unix s UTC
  kind          TEXT    NOT NULL DEFAULT 'shelly',    -- 'shelly' | 'weather' (no CHECK: new kinds must not need a table rebuild)
  ok            INTEGER NOT NULL CHECK (ok IN (0, 1)),
  http_status   INTEGER,                              -- NULL when no HTTP response (timeout, DNS, TLS)
  error         TEXT,                                 -- closed category enum from code, NEVER exception text / URL / device id
  n_devices     INTEGER,                              -- configured devices present in the response
  n_new         INTEGER,                              -- reading rows inserted this cycle
  n_unchanged   INTEGER,                              -- report ts == last stored (dedup drop)
  n_rejected    INTEGER,                              -- implausible/regressed ts or unparsable device block
  retry_after_s INTEGER,                              -- from Retry-After on 429, else NULL
  latency_ms    INTEGER
) STRICT;
CREATE INDEX poll_log_kind_ts ON poll_log (kind, ts);

CREATE TABLE reading (
  device_id TEXT    NOT NULL,                         -- normalized lowercase hex id from devices.toml
  ts        INTEGER NOT NULL CHECK (ts > 0),          -- report time, unix s UTC (see ts_source)
  ts_source TEXT    NOT NULL,                         -- 'cloud' (_updated) | 'device' | 'poll'
  t_c       REAL,                                     -- raw, uncalibrated; NULL = no valid reading
  rh        REAL,                                     -- NULL for Flood or invalid
  battery   REAL,                                     -- percent
  flood     INTEGER CHECK (flood IN (0, 1)),          -- Flood only
  polled_at INTEGER NOT NULL,                         -- when WE first saw this report
  poll_id   INTEGER REFERENCES poll_log (id),
  source    TEXT    NOT NULL DEFAULT 'poll',          -- 'poll' | 'cloud_stats' (future backfill)
  PRIMARY KEY (device_id, ts, source)
) WITHOUT ROWID, STRICT;

CREATE TABLE raw_status (
  device_id TEXT    NOT NULL,
  ts        INTEGER NOT NULL,
  source    TEXT    NOT NULL DEFAULT 'poll',
  body      TEXT    NOT NULL,                         -- per-device status JSON exactly as received
  PRIMARY KEY (device_id, ts, source)
) WITHOUT ROWID, STRICT;

CREATE TABLE outdoor (
  ts         INTEGER NOT NULL,
  variable   TEXT    NOT NULL,
  value      REAL,
  interval_s INTEGER NOT NULL DEFAULT 3600,
  fetched_at INTEGER NOT NULL,
  PRIMARY KEY (ts, variable)
) WITHOUT ROWID, STRICT;

CREATE TABLE alert_state (
  key         TEXT    PRIMARY KEY,
  since       INTEGER NOT NULL,
  notified_at INTEGER
) STRICT;

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;
