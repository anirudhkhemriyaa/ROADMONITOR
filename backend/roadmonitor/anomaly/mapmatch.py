"""Snap events (noisy GPS) to road segments -> segment_id.

Default matcher: nearest-edge by perpendicular distance to the segment
polyline, subject to a radius (config anomaly.match_radius_m). This needs
nothing beyond this package, so it runs (and is tested) anywhere.

Swap-in matchers (same output contract: event dict gains 'segment_id'):
  - LeuvenMapMatching: pip install Levenshtein... no - LeuvenMapMatching;
    pass its map-matched trajectories through assign() below.
  - OSRM /match: reuse roadmonitor.processing.mapmatch.osrm_match_url and
    translate tracepoint keys to segment ids on the loaded net.
Fallback semantics are deliberately simple and documented as
"threshold-based, validation pending" like every other stage.
"""
import math

DEFAULT_RADIUS_M = 15.0


def _bbox_segments(segments: list, cell_deg: float = 0.002):
    """Grid index: cell -> [segment index]. ~200 m cells at Gwalior lat."""
    index = {}
    for i, s in enumerate(segments):
        lats = [c[0] for c in s["coords"]]
        lons = [c[1] for c in s["coords"]]
        a, b = min(lats), max(lats)
        x, y = min(lons), max(lons)
        for la in range(int(a / cell_deg), int(b / cell_deg) + 1):
            for lo in range(int(x / cell_deg), int(y / cell_deg) + 1):
                index.setdefault((la, lo), []).append(i)
    return index, cell_deg


def point_to_polyline_m(lat: float, lon: float, coords: list) -> float:
    """Min distance from point to a polyline, metres. Uses local equirect
    approximation around the segment centroid (fine for < 1 km spans)."""
    clat = sum(c[0] for c in coords) / len(coords)
    kx = 111_320.0 * math.cos(math.radians(clat))
    ky = 110_574.0
    px = lon * kx
    py = lat * ky
    best = float("inf")
    for j in range(len(coords) - 1):
        ax = coords[j][1] * kx
        ay = coords[j][0] * ky
        bx = coords[j + 1][1] * kx
        by = coords[j + 1][0] * ky
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        if l2 == 0:
            d2 = (px - ax) ** 2 + (py - ay) ** 2
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
            d2 = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
        best = min(best, d2 ** 0.5)
    return best


def nearest_edge(lat: float, lon: float, net: dict, index=None,
                 cell_deg: float = 0.002, radius_m: float = DEFAULT_RADIUS_M):
    """(segment_id, distance_m) of the closest segment within radius, else
    (None, None)."""
    if index is None:
        index, cell_deg = _bbox_segments(net["segments"])
    cand = set()
    for la in range(int(lat / cell_deg) - 1, int(lat / cell_deg) + 2):
        for lo in range(int(lon / cell_deg) - 1, int(lon / cell_deg) + 2):
            cand.update(index.get((la, lo), ()))
    best, best_d = None, radius_m
    for i in cand:
        s = net["segments"][i]
        d = point_to_polyline_m(lat, lon, s["coords"])
        if d <= best_d:
            best, best_d = s, d
    return (best["segment_id"], best_d) if best else (None, None)


def assign(events: list, net: dict, radius_m: float = DEFAULT_RADIUS_M) -> list:
    """Adds 'segment_id' (+ 'snap_dist_m') to each event dict via the
    nearest-edge fallback matcher."""
    index, cell_deg = _bbox_segments(net["segments"])
    out = []
    for e in events:
        lat, lon = e.get("lat"), e.get("lon")
        if lat is None or lon is None:
            out.append(e)
            continue
        seg, d = nearest_edge(lat, lon, net, index, cell_deg, radius_m)
        out.append(dict(e, segment_id=seg, snap_dist_m=d))
    return out


def persist(store, events: list) -> int:
    """Writes detected (already severity-enriched, segment-snapped) events
    to anomaly_events. Returns inserted row count."""
    from .schema import ensure_tables
    import json as _json
    ensure_tables(store)
    with store.lock:
        n = 0
        for e in events:
            store.conn.execute(
                "INSERT INTO anomaly_events (trip_id, vehicle_type, event_type,"
                " ts, lat, lon, severity, method, segment_id, features)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (e.get("trip_id"), e.get("vehicle_type"), e["event_type"],
                 e.get("ts"), e.get("lat"), e.get("lon"), e.get("severity"),
                 e.get("method", "threshold-based, unvalidated"),
                 e.get("segment_id"), _json.dumps(e.get("features") or {})))
            n += 1
        store.conn.commit()
    return n
