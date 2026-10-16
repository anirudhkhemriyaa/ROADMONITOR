-- Phase C target schema (Postgres with PostGIS).
-- Phase B runs the equivalent schema on embedded SQLite (roadmonitor/store.py);
-- this file is applied by docker-compose's db service when Phase C lands.
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS devices (
    device_id  TEXT PRIMARY KEY,
    name       TEXT,
    key_hash   TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trips (
    id           BIGSERIAL PRIMARY KEY,
    trip_id      TEXT UNIQUE NOT NULL,
    device_id    TEXT,
    vehicle_type TEXT,
    mount_type   TEXT,
    app_version  TEXT,
    started_at   TIMESTAMPTZ,
    ended_at     TIMESTAMPTZ,
    metadata     JSONB NOT NULL,
    n_rows       INTEGER,
    duration_s   REAL,
    effective_hz REAL,
    n_events     INTEGER DEFAULT 0,
    n_segments   INTEGER DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'received',
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       TEXT NOT NULL DEFAULT 'v1'
);
CREATE INDEX IF NOT EXISTS idx_trips_received ON trips(received_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id         BIGSERIAL PRIMARY KEY,
    trip_id    TEXT NOT NULL REFERENCES trips(trip_id),
    ts         BIGINT,
    geom       GEOMETRY(Point, 4326),   -- built from lat/lon at load time
    lat        REAL,
    lon        REAL,
    dist_m     REAL,
    peak       REAL,
    p2p        REAL,
    width_ms   REAL,
    jerk       REAL,
    energy     REAL,
    speed      REAL,
    gyro_peak  REAL,
    speed_drop REAL,
    type_hint  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_trip  ON events(trip_id);
CREATE INDEX IF NOT EXISTS idx_events_geom  ON events USING GIST(geom);

CREATE TABLE IF NOT EXISTS segments (
    id        BIGSERIAL PRIMARY KEY,
    trip_id   TEXT NOT NULL REFERENCES trips(trip_id),
    ts_start  BIGINT,
    ts_end    BIGINT,
    geom      GEOMETRY(Point, 4326),
    lat       REAL,
    lon       REAL,
    dist_m    REAL,
    rms       REAL,
    std       REAL,
    max_a     REAL,
    p95       REAL,
    kurtosis  REAL,
    band_0_5  REAL,
    band_5_15 REAL,
    band_15p  REAL,
    speed_mean REAL,
    n         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_segments_trip ON segments(trip_id);
CREATE INDEX IF NOT EXISTS idx_segments_geom ON segments USING GIST(geom);
