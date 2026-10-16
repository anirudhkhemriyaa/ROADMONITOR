"""Repair-priority scoring (priority.py).

priority = w1*severity + w2*frequency + w3*poi + w4*road_class + w5*vulnerable
All five terms are normalised to 0..1 and the w* weights to 1.0 in total
(config anomaly.priority_weights). The weights are engineering defaults -
set them formally later via AHP pairwise comparison; the formula needs no
change when the weights change (that is the point of keeping them in
config.yaml).

  severity   - segment sev_p95 ("threshold-based, unvalidated")
  frequency  - min(1, n_trips / min_confirm_trips): more independent runs
               witnessing the defect -> higher confidence in prevalence
  poi        - highest POI weight (hospital 1.0, school 0.9, hostel/college/
               university 0.7, marketplace/bus_stand 0.6) among POIs within
               poi_buffer_m measured ALONG THE ROAD NETWORK (graph distance
               from the segment's endpoint nodes), not straight-line
  road_class - class weight (primary 1.0 ... service 0.2) from OSM highway
  vulnerable - 1.0 if any vulnerable-users POI (school/hospital/college/
               university/dormitory) is within the buffer else 0.0

Network distance: Dijkstra over the road graph (nodes = segment endpoints,
edge length = polyline length), multi-source from each POI anchored to its
nearest edge, limited to poi_buffer_m.
"""
import heapq
import json
import math
from datetime import datetime, timezone

from .mapmatch import _bbox_segments

VULNERABLE_TYPES = {"school", "hospital", "college", "university", "dormitory"}


# ---- road graph ----------------------------------------------------------------

def build_graph(net: dict) -> dict:
    """Undirected adjacency: node -> [(node, length_m, segment_id)]."""
    graph = {}
    for s in net["segments"]:
        coords = s["coords"]
        u, v = (round(coords[0][0], 7), round(coords[0][1], 7)), \
               (round(coords[-1][0], 7), round(coords[-1][1], 7))
        length = s.get("length_m") or 0.0
        graph.setdefault(u, []).append((v, length, s["segment_id"]))
        graph.setdefault(v, []).append((u, length, s["segment_id"]))
    return graph


def _dijkstra(graph, sources: dict, limit: float) -> dict:
    """sources: node -> start distance (virtual POI nodes anchor at 0).
    Returns node -> best distance, cutoff at limit."""
    dist = {}
    pq = [(d, n) for n, d in sources.items()]
    heapq.heapify(pq)
    while pq:
        d, n = heapq.heappop(pq)
        if n in dist:
            continue
        dist[n] = d
        if d >= limit:
            continue
        for v, w, _seg in graph.get(n, ()):
            nd = d + w
            if v not in dist and nd <= limit:
                heapq.heappush(pq, (nd, v))
    return dist


def snap_poi_to_graph(poi: dict, net: dict, index=None) -> tuple:
    """Anchors a POI to its nearest edge, returning virtual nodes
    {u: d_from_u, v: length - d_from_u} so the buffer measures along the
    road, not as the crow flies. Virtual nodes never enter net_segments."""
    from .mapmatch import _bbox_segments, point_to_polyline_m
    if index is None:
        index, _ = _bbox_segments(net["segments"])
    best, best_d = None, float("inf")
    for i, s in enumerate(net["segments"]):
        d = point_to_polyline_m(poi["lat"], poi["lon"], s["coords"])
        if d < best_d:
            best, best_d = s, d
    if best is None:
        return {}
    # along-edge distance of the perpendicular foot:
    coords = best["coords"]
    cum = [0.0]
    from .roadnet import polyline_length_m
    for j in range(len(coords) - 1):
        cum.append(cum[-1] + polyline_length_m([coords[j], coords[j + 1]]))
    lo, hi = 0, len(coords) - 1
    best_along, best_seg_d = 0.0, float("inf")
    for j in range(hi):
        # approximate: pick the carbon endpoint projection
        for f in range(11):
            frac = f / 10.0
            la = coords[j][0] + (coords[j + 1][0] - coords[j][0]) * frac
            lo_ = coords[j][1] + (coords[j + 1][1] - coords[j][1]) * frac
            from math import cos, radians
            d = math.hypot((la - poi["lat"]) * 110574.0,
                           (lo_ - poi["lon"]) * 111320.0 * cos(radians(poi["lat"])))
            if d < best_seg_d:
                best_seg_d = d
                best_along = cum[j] + frac * (cum[j + 1] - cum[j])
    L = best.get("length_m") or cum[-1]
    u = (round(coords[0][0], 7), round(coords[0][1], 7))
    v = (round(coords[-1][0], 7), round(coords[-1][1], 7))
    return {u: best_along, v: max(0.0, L - best_along)}


# ---- priority ------------------------------------------------------------------

def poi_impact(poi: dict, net: dict, index, cfg) -> dict:
    """Dijkstra-limited buffer for one POI: returns
    {segment_id: (weight, poi_name, poi_type, network_dist_m)} over the
    segments within poi_buffer_m measured along the road network."""
    anchor = snap_poi_to_graph(poi, net, index)
    if not anchor:
        return {}
    graph = build_graph(net)
    dist = _dijkstra(_graph_with_virtual(graph, anchor), {"__poi__": 0.0},
                     cfg["anomaly"]["poi_buffer_m"])
    # a segment is touched when either endpoint node is within the buffer
    w = cfg["anomaly"]["poi_weights"].get(poi["poi_type"], 0.3)
    out = {}
    for s in net["segments"]:
        u = (round(s["coords"][0][0], 7), round(s["coords"][0][1], 7))
        v = (round(s["coords"][-1][0], 7), round(s["coords"][-1][1], 7))
        d = min(dist.get(u, math.inf), dist.get(v, math.inf))
        if d <= cfg["anomaly"]["poi_buffer_m"]:
            out[s["segment_id"]] = (w, p_name(poi), poi["poi_type"], d)
    return out


def p_name(poi: dict) -> str:
    return poi.get("name") or poi["poi_type"]


def _graph_with_virtual(graph, anchor: dict):
    g = {n: list(nbrs) for n, nbrs in graph.items()}
    g["__poi__"] = [(n, d, "__poi__") for n, d in anchor.items()]
    for n, d in anchor.items():
        g.setdefault(n, []).append(("__poi__", d, "__poi__"))
    return g


def compute(store, net: dict, cfg) -> dict:
    """Full priority recompute: reads segment_stats, adds priority +
    components, writes back. Returns summary counts."""
    import json as _json
    w = cfg["anomaly"]["priority_weights"]
    min_trips = cfg["anomaly"].get("min_confirm_trips", 3)
    class_w = cfg["anomaly"]["road_class_weights"]

    pois = [dict(r) for r in store.conn.execute(
        "SELECT * FROM net_pois").fetchall()]
    index, _ = _bbox_segments(net["segments"])
    poi_by_seg = {}
    for p in pois:
        for seg_id, (pw, pname, ptype, pd) in poi_impact(p, net, index, cfg).items():
            cur = poi_by_seg.get(seg_id)
            if cur is None or pw > cur[0]:
                poi_by_seg[seg_id] = (pw, pname, ptype, pd)

    road_class = {}
    for s in net["segments"]:
        road_class[s["segment_id"]] = s.get("road_class") or "unclassified"

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updated = 0
    with store.lock:
        rows = [dict(r) for r in store.conn.execute(
            "SELECT * FROM segment_stats").fetchall()]
        for st in rows:
            comps = _json.loads(st.get("components") or "{}")
            severity = st.get("sev_p95") or 0.0
            freq = min(1.0, (st.get("n_trips") or 0) / max(min_trips, 1))
            cur = poi_by_seg.get(st["segment_id"], (0.0, None, "", None))
            poi_c = cur[0]
            poi_name, poi_type, poi_dist = cur[1], cur[2], cur[3]
            vuln = 1.0 if poi_type in VULNERABLE_TYPES else 0.0
            rc = class_w.get(road_class.get(st["segment_id"], "unclassified"), 0.4)
            priority = (w["severity"] * severity + w["frequency"] * freq
                        + w["poi"] * poi_c + w["road_class"] * rc
                        + w["vulnerable"] * vuln)
            components = {
                "priority_terms": {
                    "severity": round(severity, 4),
                    "frequency": round(freq, 4),
                    "poi": poi_c,
                    "poi_name": poi_name,
                    "poi_type": poi_type,
                    "poi_dist_m": round(poi_dist, 1) if poi_dist is not None else None,
                    "road_class": rc,
                    "vulnerable": vuln,
                    "min_confirm_trips": min_trips,
                    "method": "threshold-based, unvalidated",
                },
                **comps,
            }
            store.conn.execute(
                "UPDATE segment_stats SET priority=?, components=?, updated_at=?"
                " WHERE segment_id=?",
                (round(priority, 4), _json.dumps(components), now,
                 st["segment_id"]))
            updated += 1
        store.conn.commit()
    return {"scored": updated,
            "confirmed": sum(1 for s in rows if s.get("confirmed"))}
