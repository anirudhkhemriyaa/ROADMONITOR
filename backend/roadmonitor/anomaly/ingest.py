"""Ingest: decode the app's trip payload into rows + store 10m windows.

Accepts (a) gzip-compressed CSV bytes/paths (the Android app's /upload body
persists these under data/raw/) and (b) plain CSV poured through /upload.
The app already computed its own events/segments JSONL for v1 uploads -
those are stored by the existing ingest path; this module is the server-side
read of the raw signal for detect.py. No existing endpoint changes.

CSV header (v2 app):
timestamp_ms,ax,ay,az,lat,lon,speed_kmh,gps_accuracy_m,bearing_deg,gps_age_ms,
t_ns,t_wall_ms,gx,gy,gz,grav_x,grav_y,grav_z,speed_mps,altitude,speed_acc
Legacy 10-column files also load (gravity columns absent -> detect uses the
high-pass fallback).
"""
import csv
import gzip
import io
import json
from pathlib import Path

from ..geo import haversine_m

# expected v2 columns; legacy files stop after gps_age_ms
_N_CORE = 10
_V2_COLS = ["timestamp_ms", "ax", "ay", "az", "lat", "lon", "speed_kmh",
            "gps_accuracy_m", "bearing_deg", "gps_age_ms", "t_ns", "t_wall_ms",
            "gx", "gy", "gz", "grav_x", "grav_y", "grav_z", "speed_mps",
            "altitude", "speed_acc"]


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse_trip_csv(data: bytes) -> list:
    """Universal decode: gzip or plain; v2 or legacy header. Returns row
    dicts with float-or-None fields (GPS fields may be empty = no fix)."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    text = data.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = []
    header = None
    for rec in reader:
        if not rec or all(c.strip() == "" for c in rec):
            continue
        if header is None and not any(_num(c) is not None for c in rec[:3]):
            header = [c.strip().lower() for c in rec]
            continue
        if header and len(header) > _N_CORE:
            vals = [_num(rec[i]) if i < len(rec) else None
                    for i in range(len(header))]
            rows.append(dict(zip(header, vals)))
        else:                                  # legacy positional layout
            n = len(rec)
            d = {"timestamp_ms": _num(rec[0]), "ax": _num(rec[1]),
                 "ay": _num(rec[2]), "az": _num(rec[3]), "lat": _num(rec[4]),
                 "lon": _num(rec[5]), "speed_kmh": _num(rec[6]),
                 "gps_accuracy_m": _num(rec[7]) if n > 7 else None,
                 "bearing_deg": _num(rec[8]) if n > 8 else None,
                 "gps_age_ms": _num(rec[9]) if n > 9 else None}
            if n > 10:                         # v2 extras without a header
                for i, k in enumerate(_V2_COLS[_N_CORE:], start=_N_CORE):
                    if i < n:
                        d[k] = _num(rec[i])
            rows.append(d)
    return rows


def load_trip_file(path) -> list:
    return parse_trip_csv(Path(path).read_bytes())


def vertical_accel(rows: list) -> list:
    """True vertical acceleration (m/s²) per row, stationary ~ 0.

    App formula replicated (plan 5.1): unit up u = g/|g| from the gravity
    columns; a_vert = dot(a, u) - |g| -> removes both gravity and tilt, so
    potholes modulate vertical frequency response on every mount orientation.
    Legacy rows without gravity data get a high-pass fallback: az minus its
    1 s rolling mean (tilt-insensitive only for upright mounts; documented).
    """
    def gravity_ok(r):
        g = (r.get("grav_x"), r.get("grav_y"), r.get("grav_z"))
        return None not in g and 0 < sum(v * v for v in g) ** 0.5 < 50

    cache = []
    for i, r in enumerate(rows):
        if gravity_ok(r):
            gx, gy, gz = r["grav_x"], r["grav_y"], r["grav_z"]
            gm = (gx * gx + gy * gy + gz * gz) ** 0.5
            vert = r["ax"] * gx / gm + r["ay"] * gy / gm + r["az"] * gz / gm
            cache.append(("g", vert - gm))
        elif r.get("az") is not None:
            cache.append(("azhp", None, r["az"]))   # resolve after pass
        else:
            cache.append(("skip", None))
    # legacy branch needs the 1 s rolling mean; compute once rows are known
    azs = [c[2] for c in cache if c[0] == "azhp"]
    n_win = 50
    out = [None] * len(rows)
    if azs:
        for idx_az, i in enumerate([j for j, c in enumerate(cache)
                                    if c[0] == "azhp"]):
            lo, hi = max(0, idx_az - n_win // 2), min(len(azs), idx_az + n_win // 2)
            mean = sum(azs[lo:hi]) / max(1, hi - lo)
            out[i] = azs[idx_az] - mean
    for j, c in enumerate(cache):
        if c[0] == "g":
            out[j] = c[1]
    return out


def windows_10m(rows: list, verts: list, bin_m: float = 10.0) -> list:
    """Distance-integrated 10 m roughness windows (same statistic family as
    the on-device pipeline so the two views can be compared)."""
    import math
    stats = {"n": 0, "sum": 0.0, "sum2": 0.0, "mx": 0.0, "vals": [],
             "ts_start": None, "ts_end": None, "lat": None, "lon": None,
             "spd": None, "dist": 0.0}
    win = []
    out = []
    dist = 0.0
    last = None
    for i, r in enumerate(rows):
        ts = r.get("timestamp_ms")
        lat, lon = r.get("lat"), r.get("lon")
        if lat is not None and lon is not None:
            if last is not None:
                ll, lt = last
                dist += haversine_m(ll, lt, lat, lon)
            last = (lat, lon)
        if ts is None:
            continue
        acc = {"n": stats["n"] + 1, "sum": stats["sum"] + (verts[i] or 0.0),
               "sum2": stats["sum2"] + (verts[i] or 0.0) ** 2,
               "mx": max(stats["mx"], abs(verts[i] or 0.0)),
               "vals": stats["vals"] + [verts[i] or 0.0],
               "ts_start": stats["ts_start"] or ts, "ts_end": ts,
               "lat": stats["lat"] if stats["lat"] is not None else lat,
               "lon": stats["lon"] if stats["lon"] is not None else lon,
               "spd": r.get("speed_mps") or r.get("speed_kmh"),
               "dist": dist}
        stats = acc
        if dist >= bin_m and stats["n"] > 0:
            n = stats["n"]
            mean = stats["sum"] / n
            rms = (stats["sum2"] / n) ** 0.5
            s = sorted(stats["vals"])
            p95 = s[min(n - 1, int(0.95 * (n - 1)))]
            out.append({"ts_start": stats["ts_start"], "ts_end": stats["ts_end"],
                        "lat": stats["lat"], "lon": stats["lon"],
                        "dist_m": stats["dist"], "rms": rms, "std": 0.0,
                        "max": stats["mx"], "p95": p95,
                        "speed_mean": stats["spd"], "n": n})
            stats = {"n": 0, "sum": 0.0, "sum2": 0.0, "mx": 0.0, "vals": [],
                     "ts_start": None, "ts_end": None, "lat": lat, "lon": lon,
                     "spd": stats["spd"], "dist": 0.0}
            dist = 0.0
    return out


def ingest_trip(store, trip_id: str, vehicle_type: str, data: bytes,
                source_path: str = "") -> dict:
    """Full ingest of one trip's raw CSV: parses rows, computes + stores 10m
    windows via the existing idempotent insert_trip, returns counts."""
    from .schema import ensure_tables
    ensure_tables(store)
    rows = parse_trip_csv(data)
    verts = vertical_accel(rows)
    segs = windows_10m(rows, verts)
    if not segs:
        # fall back to time-based slices (~5 s) so GPS-less trips still yield
        # windows instead of being dropped (D001-style drives stay analysable)
        segs = _time_windows(rows, verts)
    meta = {"trip_id": trip_id, "device_id": "server_ingest", "vehicle_type":
            vehicle_type, "rows": len(rows), "source_path": source_path}
    res = store.insert_trip(meta, events=[], segments=segs)
    res.update({"rows": len(rows), "n_windows": len(segs)})
    return res


def _time_windows(rows: list, verts: list, span_ms: float = 5000.0) -> list:
    out = []
    cur = []
    t0 = None
    for i, r in enumerate(rows):
        ts = r.get("timestamp_ms")
        if ts is None:
            continue
        if t0 is None:
            t0 = ts
        cur.append((ts, verts[i] or 0.0, r.get("lat"), r.get("lon")))
        if ts - t0 >= span_ms:
            n = len(cur)
            vals = [v for _, v, _, _ in cur]
            mean = sum(vals) / n
            rms = (sum(v * v for v in vals) / n) ** 0.5
            s = sorted(vals)
            lats = [la for _, _, la, _ in cur if la is not None]
            lons = [lo for _, _, _, lo in cur if lo is not None]
            out.append({"ts_start": t0, "ts_end": cur[-1][0],
                        "lat": lats[0] if lats else None,
                        "lon": lons[0] if lons else None, "dist_m": 0.0,
                        "rms": rms, "std": 0.0, "max": max(abs(v) for v in vals),
                        "p95": s[min(n - 1, int(0.95 * (n - 1)))],
                        "speed_mean": None, "n": n})
            cur, t0 = [], None
    return out
