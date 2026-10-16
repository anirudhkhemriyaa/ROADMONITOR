"""Trip persistence (Phase B).

Phase B ships an embedded SQLite store so everything runs (and tests) on a
plain laptop with zero installs. The schema mirrors sql/schema_postgres.sql
(the Postgres/PostGIS deployment, used from Phase C / docker-compose).
Deviation from the plan doc, stated as an assumption: SQLAlchemy + Alembic
arrive together with the Postgres deployment; store.py keeps a narrow API
(register_device, insert_trip, trip_status, list_trips) so the swap is
mechanical.
"""
import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import cfg_get

# Postgres URLs (Phase C) are rejected loudly rather than silently faked.
SQLITE_PREFIX = "sqlite:///"

DDL = """
CREATE TABLE IF NOT EXISTS devices (
    device_id  TEXT PRIMARY KEY,
    name       TEXT,
    key_hash   TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trips (
    id           INTEGER PRIMARY KEY,
    trip_id      TEXT UNIQUE NOT NULL,
    device_id    TEXT,
    vehicle_type TEXT,
    mount_type   TEXT,
    app_version  TEXT,
    started_at   TEXT,
    ended_at     TEXT,
    metadata     TEXT NOT NULL,
    n_rows       INTEGER,
    duration_s   REAL,
    effective_hz REAL,
    n_events     INTEGER DEFAULT 0,
    n_segments   INTEGER DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'received',
    received_at  TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'v1'
);
CREATE INDEX IF NOT EXISTS idx_trips_received ON trips(received_at DESC);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    trip_id    TEXT NOT NULL,
    ts         INTEGER,
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
CREATE INDEX IF NOT EXISTS idx_events_trip ON events(trip_id);
CREATE TABLE IF NOT EXISTS segments (
    id        INTEGER PRIMARY KEY,
    trip_id   TEXT NOT NULL,
    ts_start  INTEGER,
    ts_end    INTEGER,
    lat       REAL,
    lon       REAL,
    dist_m    REAL,
    rms       REAL,
    std       REAL,
    max_a     REAL,   -- JSONL key "max" ('max' reads poorly as bare SQL column)
    p95       REAL,
    kurtosis  REAL,
    band_0_5  REAL,
    band_5_15 REAL,
    band_15p  REAL,
    speed_mean REAL,
    n         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_segments_trip ON segments(trip_id);
CREATE TABLE IF NOT EXISTS road_links (
    link_id     TEXT PRIMARY KEY,
    lat         REAL,
    lon         REAL,
    score_raw   REAL,
    score_norm  REAL,
    class       TEXT,
    n_bins      INTEGER,
    n_trips     INTEGER,
    n_devices   INTEGER,
    status      TEXT,
    confidence  REAL,
    last_seen_ms INTEGER,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_clusters (
    cluster_id  TEXT PRIMARY KEY,
    lat         REAL,
    lon         REAL,
    n_events    INTEGER,
    n_trips     INTEGER,
    n_devices   INTEGER,
    peak_med    REAL,
    width_med   REAL,
    type        TEXT,
    recency     REAL,
    status      TEXT,
    confidence  REAL,
    updated_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ts_ms(iso: str) -> float:
    """Parses the received_at iso timestamp to unix ms (None-safe upstream)."""
    return datetime.fromisoformat(iso).timestamp() * 1000.0


def _sqlite_path(url: str) -> Path:
    if not url.startswith(SQLITE_PREFIX):
        raise RuntimeError(
            "Only sqlite:/// URLs are supported in Phase B; "
            "Postgres comes with docker-compose / Phase C."
        )
    path = url[len(SQLITE_PREFIX):]
    if not path.startswith("./"):
        path = "./" + path
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


class Store:
    """Thin sqlite3 store. Single connection + lock: the write volume of this
    project (a few trips per day) never needs more."""

    def __init__(self, url: str):
        self.path = _sqlite_path(url)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.init_schema()

    def init_schema(self) -> None:
        with self.lock:
            self.conn.executescript(DDL)
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- devices ---------------------------------------------------------------

    def register_device(self, name: str = "") -> dict:
        key = secrets.token_hex(cfg_get("api.device_key_bytes", 32))
        device_id = "dev_" + secrets.token_hex(8)
        with self.lock:
            self.conn.execute(
                "INSERT INTO devices (device_id, name, key_hash, created_at) VALUES (?,?,?,?)",
                (device_id, name, hashlib.sha256(key.encode()).hexdigest(), _now()),
            )
            self.conn.commit()
        return {"device_id": device_id, "device_key": key}

    def device_key_is_known(self, key: str) -> bool:
        h = hashlib.sha256(key.encode()).hexdigest()
        row = self.conn.execute(
            "SELECT 1 FROM devices WHERE key_hash = ?", (h,)
        ).fetchone()
        return row is not None

    # ---- trips -------------------------------------------------------------------

    def insert_trip(self, metadata: dict, events: list, segments: list) -> dict:
        """Idempotent on metadata['trip_id'] (UNIQUE). Returns what the API
        should answer, including duplicate=True on a re-upload."""
        trip_id = str(metadata.get("trip_id") or "")
        batch = cfg_get("api.insert_batch_size", 1000)
        with self.lock:
            # isolation_level=None -> explicit BEGIN IMMEDIATE gives the
            # check-then-insert race safety that plain autocommit lacks.
            self.conn.isolation_level = None
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                dup = self.conn.execute(
                    "SELECT id FROM trips WHERE trip_id = ?", (trip_id,)
                ).fetchone()
                if dup is not None:
                    self.conn.execute("ROLLBACK")
                    counts = self._counts(trip_id)
                    counts.update(status="received", duplicate=True, trip_id=trip_id)
                    return counts
                self.conn.execute(
                    "INSERT INTO trips (trip_id, device_id, vehicle_type, mount_type,"
                    " app_version, started_at, ended_at, metadata, n_rows, duration_s,"
                    " effective_hz, n_events, n_segments, status, received_at, source)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        trip_id,
                        metadata.get("device_id"),
                        metadata.get("vehicle_type"),
                        metadata.get("mount_type"),
                        metadata.get("app_version"),
                        metadata.get("started_at"),
                        metadata.get("ended_at"),
                        json.dumps(metadata),
                        metadata.get("rows"),
                        metadata.get("duration_s"),
                        metadata.get("effective_hz"),
                        len(events),
                        len(segments),
                        "received",
                        _now(),
                        "v1",
                    ),
                )
                ev_rows = [(
                    trip_id, e.get("ts"), e.get("lat"), e.get("lon"), e.get("dist_m"),
                    e.get("peak"), e.get("p2p"), e.get("width_ms"), e.get("jerk"),
                    e.get("energy"), e.get("speed"), e.get("gyro_peak"),
                    e.get("speed_drop"), e.get("type_hint"),
                ) for e in events]
                seg_rows = [(
                    trip_id, s.get("ts_start"), s.get("ts_end"), s.get("lat"), s.get("lon"),
                    s.get("dist_m"), s.get("rms"), s.get("std"), s.get("max"),
                    s.get("p95"), s.get("kurtosis"), s.get("band_0_5"),
                    s.get("band_5_15"), s.get("band_15p"), s.get("speed_mean"),
                    s.get("n"),
                ) for s in segments]
                for i in range(0, len(ev_rows), batch):
                    self.conn.executemany(
                        "INSERT INTO events (trip_id, ts, lat, lon, dist_m, peak, p2p,"
                        " width_ms, jerk, energy, speed, gyro_peak, speed_drop, type_hint)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ev_rows[i:i + batch],
                    )
                for i in range(0, len(seg_rows), batch):
                    self.conn.executemany(
                        "INSERT INTO segments (trip_id, ts_start, ts_end, lat, lon,"
                        " dist_m, rms, std, max_a, p95, kurtosis, band_0_5, band_5_15,"
                        " band_15p, speed_mean, n) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        seg_rows[i:i + batch],
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            counts = self._counts(trip_id)
            counts.update(status="received", duplicate=False, trip_id=trip_id)
            return counts

    def _counts(self, trip_id: str) -> dict:
        ev = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE trip_id=?", (trip_id,)
        ).fetchone()[0]
        sg = self.conn.execute(
            "SELECT COUNT(*) FROM segments WHERE trip_id=?", (trip_id,)
        ).fetchone()[0]
        return {"n_events": ev, "n_segments": sg}

    def trip_status(self, trip_id: str):
        row = self.conn.execute(
            "SELECT trip_id, status, n_events, n_segments, received_at FROM trips"
            " WHERE trip_id = ?", (trip_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_trips(self, limit: int = 50) -> list:
        rows = self.conn.execute(
            "SELECT trip_id, vehicle_type, status, received_at, received_at, n_events,"
            " n_segments, effective_hz FROM trips ORDER BY received_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("received_at", None)
            out.append(d)
        return out

    def mark_trip_status(self, trip_id: str, status: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE trips SET status=? WHERE trip_id=?", (status, trip_id))
            self.conn.commit()

    # ---- aggregate tables + accessors (Phase C) --------------------------------

    def all_segments_with_meta(self) -> list:
        """Every segment bin with the owning trip's metadata for aggregation."""
        rows = self.conn.execute(
            "SELECT s.id, s.trip_id, t.device_id, s.ts_start, s.ts_end, s.lat, s.lon,"
            " s.dist_m, s.rms, s.std, s.max_a AS max, s.p95, s.kurtosis, s.band_0_5,"
            " s.band_5_15, s.band_15p, s.speed_mean, s.n, t.received_at"
            " FROM segments s JOIN trips t ON t.trip_id = s.trip_id"
            " ORDER BY s.trip_id, s.id",
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["row_key"] = d.pop("id")
            ts = d.get("received_at")
            d["received_at_ms"] = ts_ms(ts) if ts else None
            out.append(d)
        return out

    def all_events_with_meta(self) -> list:
        rows = self.conn.execute(
            "SELECT e.id, e.trip_id, t.device_id, e.ts, e.lat, e.lon, e.dist_m,"
            " e.peak, e.p2p, e.width_ms, e.jerk, e.energy, e.speed, e.gyro_peak,"
            " e.speed_drop, e.type_hint, t.received_at"
            " FROM events e JOIN trips t ON t.trip_id = e.trip_id"
            " ORDER BY e.trip_id, e.id",
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["row_key"] = d.pop("id")
            ts = d.get("received_at")
            d["received_at_ms"] = ts_ms(ts) if ts else None
            out.append(d)
        return out

    def replace_links(self, links: list) -> None:
        with self.lock:
            self.conn.isolation_level = None
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DELETE FROM road_links")
                self.conn.executemany(
                    "INSERT INTO road_links (link_id, lat, lon, score_raw, score_norm,"
                    " class, n_bins, n_trips, n_devices, status, confidence,"
                    " last_seen_ms, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(
                        l["link_id"], l["lat"], l["lon"], l["score_raw"],
                        l["score_norm"], l["class"], l["n_bins"], l["n_trips"],
                        l["n_devices"], l["status"], l["confidence"],
                        l["last_seen_ms"], _now(),
                    ) for l in links],
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def links(self, min_status: str = "candidate", limit: int = 1000) -> list:
        sql = ("SELECT * FROM road_links WHERE status=?" if min_status in ("candidate", "confirmed")
               else "SELECT * FROM road_links")
        args = (min_status, limit) if min_status in ("candidate", "confirmed") else (limit,)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY confidence DESC, score_norm DESC LIMIT ?", args)]

    def replace_event_clusters(self, clusters: list) -> None:
        with self.lock:
            self.conn.isolation_level = None
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DELETE FROM event_clusters")
                self.conn.executemany(
                    "INSERT INTO event_clusters (cluster_id, lat, lon, n_events,"
                    " n_trips, n_devices, peak_med, width_med, type, recency,"
                    " status, confidence, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(
                        c["cluster_id"], c["lat"], c["lon"], c["n_events"],
                        c["n_trips"], c["n_devices"], c["peak_med"], c["width_med"],
                        c["type"], c["recency"], c["status"], c["confidence"],
                        _now(),
                    ) for c in clusters],
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def event_clusters(self, min_status: str = "candidate", limit: int = 1000) -> list:
        sql = ("SELECT * FROM event_clusters WHERE status=?" if min_status in ("candidate", "confirmed")
               else "SELECT * FROM event_clusters")
        args = (min_status, limit) if min_status in ("candidate", "confirmed") else (limit,)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY confidence DESC LIMIT ?", args)]


_STORE = None


def get_store() -> Store:
    global _STORE
    if _STORE is None:
        _STORE = Store(cfg_get("db.url", "sqlite:///./data/roadmonitor.db"))
    return _STORE


def set_store(store: Store) -> None:
    """Test/ops hook: swap the process-wide store."""
    global _STORE
    _STORE = store
