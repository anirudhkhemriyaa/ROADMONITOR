"""Demo script: fake trips on the (sample) Gwalior network.

    cd backend
    .venv\\Scripts\\python.exe scripts\\simulate_trips.py            # sample grid
    .venv\\Scripts\\python.exe scripts\\simulate_trips.py --source osmnx

Generates synthetic drives along the sample street grid (offline, no OSMnx
needed) with planted defects (SYNTHETIC demo data - never presented as real
road conditions):
  - potholes: dip ~120 ms before an impact spike, pair ~100 ms wide
  - speed bumps: 1.6 s smooth half-wave heaves
  - rough roads: sustained vertical RMS oscillation

Timing is specified in seconds and converted to trip-progress units;
amplitudes scale with vehicle_scale[vehicle] because a heavier vehicle
excites less vertical shake for the same physical defect (the same relation
the detector thresholds use).

Route geometry keeps every planted defect >= ~230 m from the trip start/end
because the pipeline (correctly) discounts events within boundary_grace_m
(200 m) of a trip boundary - the app's 150 m privacy trim makes that region
sparse. Then runs the real chain: ingest -> detect -> severity -> mapmatch
-> aggregate -> priority and prints the ranked repair list.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roadmonitor.anomaly import pipeline          # noqa: E402
from roadmonitor.anomaly import roadnet as rn     # noqa: E402
from roadmonitor.config import CFG                # noqa: E402
from roadmonitor.geo import haversine_m           # noqa: E402
from roadmonitor.store import Store               # noqa: E402

HZ = int(CFG["anomaly"]["sample_hz"])
STEP = 0.0015          # grid cell size, must match roadnet.load_sample_network
C_LON, C_LAT = 78.1828, 26.2183


def route_geometry(coords, speed_mps):
    """Leg lengths + total + duration (s) for a polyline travelled at speed."""
    legs = [haversine_m(coords[j][0], coords[j][1],
                        coords[j + 1][0], coords[j + 1][1])
            for j in range(len(coords) - 1)]
    total = sum(legs)
    return legs, total, total / speed_mps


def _rows_along(coords, legs, total, speed_mps, vert_fn, t0, jitter_m=1.5):
    """Rows following a polyline at constant speed, 50 Hz, GPS per sample
    (small GPS jitter so map matching is realistically imperfect)."""
    import random
    rnd = random.Random(42)
    n = max(2, int(total / speed_mps * HZ))
    rows = []
    for i in range(n):
        ratio = i / max(1, n - 1)
        d = ratio * total
        acc = 0.0
        j = len(legs) - 1
        for k, ll in enumerate(legs):
            if d <= acc + ll:
                j = k
                break
            acc += ll
        f = (d - acc) / max(legs[j], 1e-9)
        lat = coords[j][0] + (coords[j + 1][0] - coords[j][0]) * f
        lon = coords[j][1] + (coords[j + 1][1] - coords[j][1]) * f
        rows.append({
            "timestamp_ms": int(t0 + i * 1000 / HZ),
            "ax": 0.0, "ay": 0.0, "az": 9.81 + vert_fn(ratio),
            "lat": lat + (rnd.random() - 0.5) * (jitter_m / 111_320.0),
            "lon": lon + (rnd.random() - 0.5) * (jitter_m / 111_320.0),
            "speed_kmh": speed_mps * 3.6, "gx": 0.0, "gy": 0.0, "gz": 0.0,
            "grav_x": 0.0, "grav_y": 0.0, "grav_z": 9.81,
            "gps_accuracy_m": 5.0,
        })
    return rows


def make_vehicle_plan():
    """Routes on the sample grid with planted interior defects."""
    def pt(row, col):
        return (C_LAT + row * STEP, C_LON + col * STEP)

    return {
        "route1": {"pts": [pt(0, 0), pt(0, 1), pt(0, 2), pt(0, 3), pt(0, 4)],
                   "defects": [("pothole", 0.4), ("pothole", 0.7)],
                   "vehicle": "bike", "speed": 8.0},
        "route2": {"pts": [pt(1, 0), pt(1, 1), pt(1, 2), pt(1, 3), pt(1, 4)],
                   "defects": [("bump", 0.35), ("bump", 0.55), ("bump", 0.75)],
                   "vehicle": "car", "speed": 11.0},
        "route3": {"pts": [pt(3, 0), pt(3, 1), pt(3, 2), pt(3, 3), pt(3, 4)],
                   "defects": [("rough", 0.3, 0.8)],
                   "vehicle": "bus", "speed": 7.0},
        "route4": {"pts": [pt(4, 0), pt(4, 1), pt(4, 2), pt(4, 3)],
                   "defects": [("bump", 0.3), ("pothole", 0.6)],
                   "vehicle": "e_rickshaw", "speed": 5.5},
    }


def _vert_fn_factory(route, dur_s):
    """Vertical-acceleration signal generator (m/s²) for one planned route.

    timing in seconds -> progress units (x s = x/dur_s of the trip).
    amplitudes scale by vehicle_scale."""
    scale = CFG["anomaly"]["vehicle_scale"].get(route["vehicle"], 1.0)

    def fn(ratio):
        sig = 0.0
        for spec in route["defects"]:
            kind = spec[0]
            if kind == "pothole":
                c = spec[1]
                if c - (0.12 / dur_s) <= ratio <= c - (0.04 / dur_s):
                    sig -= 6.0 * scale       # dip ~120..40 ms before impact
                elif c <= ratio <= c + (0.10 / dur_s):
                    sig += 12.0 * scale      # impact spike ~100 ms
            elif kind == "bump":
                c = spec[1]
                wave = 1.6 / dur_s           # 1.6 s half-wave heave
                p = (ratio - c) / wave + 0.5
                if 0.0 <= p <= 1.0:
                    sig += 6.0 * scale * (1 - abs(2 * p - 1)) ** 2
            elif kind == "rough":
                a, b = spec[1], spec[2]
                if a <= ratio <= b:
                    # ±3.2 alternating every ~100 ms (broadband stand-in)
                    if int((ratio - a) / (0.1 / dur_s)) % 2 == 0:
                        sig += 3.2 * scale
                    else:
                        sig -= 3.2 * scale
        return sig
    return fn


def _csv_bytes(rows) -> bytes:
    cols = ["timestamp_ms", "ax", "ay", "az", "lat", "lon", "speed_kmh",
            "gps_accuracy_m", "bearing_deg", "gps_age_ms", "gx", "gy", "gz",
            "grav_x", "grav_y", "grav_z", "speed_mps", "altitude", "speed_acc"]
    lines = [",".join(cols)]
    for r in rows:
        lines.append(",".join(
            "" if r.get(c) is None else (f"{r[c]:.6f}"
                                         if isinstance(r[c], (int, float))
                                         else str(r[c])) for c in cols))
    return "\n".join(lines).encode()


def main():
    ap = argparse.ArgumentParser(description="Synthetic-trip demo for the "
                                 "Road Anomaly Priority Pipeline")
    ap.add_argument("--source", choices=["sample", "osmnx"], default="sample")
    ap.add_argument("--trips", type=int, default=4, help="trips per route")
    ap.add_argument("--place", default="Gwalior, Madhya Pradesh, India")
    ap.add_argument("--db", default=None, help="sqlite db URL or None for active store")
    args = ap.parse_args()

    if args.db:
        store = Store(args.db)
    else:
        from roadmonitor.store import get_store
        store = get_store()
    net_mode = "sample" if args.source == "sample" else "osmnx"
    net = rn.load_network(store, net_mode, args.place)
    print(f"network: {net['source']} - {len(net['segments'])} segments -"
          f" {len(net['pois'])} POIs")

    plan = make_vehicle_plan()
    t0 = 1_760_000_000_000
    for rname, route in plan.items():
        legs, total, dur_s = route_geometry(route["pts"], route["speed"])
        vert = _vert_fn_factory(route, dur_s)
        for k in range(args.trips):
            trip_id = f"sim_{rname}_{k}"
            rows = _rows_along(route["pts"], legs, total, route["speed"],
                               vert, t0 + k * 3_600_000)
            data = _csv_bytes(rows)
            res = pipeline.process_trip_csv(store, trip_id, route["vehicle"],
                                            data, CFG, source_path="simulated")
            print(f"  {trip_id:14s} rows={res['ingest']['rows']}"
                  f" windows={res['ingest']['n_windows']}"
                  f" events={res['events']}")

    report = pipeline.recompute(store, CFG, net_mode)
    print("\nrecompute:", report)

    print("\nranked repair list (threshold-based, unvalidated):")
    rows = store.conn.execute(
        "SELECT st.priority, st.sev_p95, st.n_trips, st.n_events,"
        " st.confirmed, ns.name FROM segment_stats st"
        " JOIN net_segments ns ON ns.segment_id = st.segment_id"
        " ORDER BY st.priority DESC LIMIT 10").fetchall()
    for i, r in enumerate(rows, 1):
        print(f"  {i:2d}. prio={r[0]:.3f} sev={r[1]:.2f} trips={r[2]}"
              f" events={r[3]} confirmed={bool(r[4])} road={r[5]}")


if __name__ == "__main__":
    main()
