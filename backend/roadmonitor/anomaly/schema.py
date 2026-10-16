"""Additive DB tables for the anomaly pipeline.

Mirrors the sqlite DDL used in the app store; the geometry columns stay
plain WKT text today, so the same DDL works on PostGIS later by making
geom GEOMETRY(LINESTRING/POINT, 4326) - see sql/schema_postgres.sql notes.
Keeping this additive means the existing trips/events/segments store and
every legacy endpoint stay untouched.
"""

DDL = """
CREATE TABLE IF NOT EXISTS anomaly_events (
    id           INTEGER PRIMARY KEY,
    trip_id      TEXT NOT NULL,
    vehicle_type TEXT,
    event_type   TEXT NOT NULL,          -- pothole | speed_bump | rough
    ts           INTEGER,                -- unix ms
    lat          REAL,
    lon          REAL,
    severity     REAL,                   -- 0..1 (severity.py)
    method       TEXT NOT NULL DEFAULT 'threshold-based, unvalidated',
    segment_id   TEXT,                   -- filled by roadnet+mapmatch
    features     TEXT                    -- JSON: peak, dip, width_ms, rms, ...
);
CREATE INDEX IF NOT EXISTS idx_anom_events_seg ON anomaly_events(segment_id);
CREATE INDEX IF NOT EXISTS idx_anom_events_trip ON anomaly_events(trip_id);

CREATE TABLE IF NOT EXISTS net_segments (
    segment_id  TEXT PRIMARY KEY,        -- stable id (roadnet.py)
    geom        TEXT NOT NULL,           -- WKT LINESTRING
    name        TEXT,                    -- OSM road name (may be null)
    road_class  TEXT,                    -- highway tag or upload-provided class
    length_m    REAL,
    source      TEXT NOT NULL DEFAULT 'osm'   -- osm | upload | sample
);
CREATE TABLE IF NOT EXISTS net_pois (
    poi_id   TEXT PRIMARY KEY,
    poi_type TEXT NOT NULL,              -- school | hospital | college | ...
    lat      REAL NOT NULL,
    lon      REAL NOT NULL,
    name     TEXT,
    source   TEXT NOT NULL DEFAULT 'osm'
);
CREATE TABLE IF NOT EXISTS segment_stats (
    segment_id      TEXT PRIMARY KEY,
    n_events        INTEGER,
    n_trips         INTEGER,
    n_vehicle_types INTEGER,
    sev_mean        REAL,
    sev_p95         REAL,
    confirmed       INTEGER,             -- boolean: >= min_confirm_trips
    priority        REAL,
    components      TEXT,                -- JSON of the priority terms
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stats_priority ON segment_stats(priority DESC);
"""


def ensure_tables(store) -> None:
    store.conn.executescript(DDL)
    store.conn.commit()
