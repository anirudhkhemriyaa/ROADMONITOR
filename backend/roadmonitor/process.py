"""Phase C orchestrator: recompute road links + event clusters from the store.

Called by the /api/v1/analysis/recompute endpoint (inline, dev mode) and by
tasks.py (Celery, docker deployment). Full rebuild keeps the aggregates
consistent without needing incremental bookkeeping at this data scale.

Map matching: when config mapmatch.enabled is true, each trip's GPS-valid
bins are matched against the OSRM /match service (base_url/profile from
config) and bins group by OSM node-pair links; OSRM failures and unmatched
bins fall back to DBSCAN spatial clustering. With matching disabled the
whole thing runs on DBSCAN alone.
"""
from .config import CFG
from .processing import aggregate as agg
from .processing import mapmatch
from .store import get_store


def _sort_segs(segs: list) -> list:
    return sorted(segs, key=lambda r: (r["trip_id"], r["ts_start"] or 0,
                                       r["row_key"]))


def _match_link_keys(segs_sorted: list) -> list:
    """Per-trip OSRM matching; returns link keys aligned with segs_sorted
    (None for GPS-less bins, disabled matching or unmatched points)."""
    keys = [None] * len(segs_sorted)
    base = CFG["mapmatch"].get("base_url", "http://localhost:5000")
    profile = CFG["mapmatch"].get("profile", "driving")
    by_trip = {}
    for i, r in enumerate(segs_sorted):
        if r["lat"] is not None and r["lon"] is not None:
            by_trip.setdefault(r["trip_id"], []).append(i)
    for idxs in by_trip.values():
        coords = [(segs_sorted[i]["lat"], segs_sorted[i]["lon"]) for i in idxs]
        matched = mapmatch.match_points(
            mapmatch.http_fetch,
            mapmatch.osrm_match_url(base, coords, profile),
        )
        if len(matched) == len(idxs):
            for i, m in zip(idxs, matched):
                keys[i] = m["key"] if m is not None else None
    return keys


def recompute_all(store=None) -> dict:
    s = store or get_store()
    now = agg.now_ms_utc()
    segs = _sort_segs(s.all_segments_with_meta())
    events = s.all_events_with_meta()

    link_keys = _match_link_keys(segs) if CFG["mapmatch"].get("enabled") else None
    links = agg.build_links(segs, CFG, now, link_keys)
    clusters = agg.build_event_clusters(events, CFG, now)
    s.replace_links(links)
    s.replace_event_clusters(clusters)

    # trips that contributed anything are marked processed (events/segments
    # stay in the store so every recompute uses the full history)
    for trip_id in {r["trip_id"] for r in segs} | {r["trip_id"] for r in events}:
        s.mark_trip_status(trip_id, "processed")

    return {
        "n_segment_bins": len(segs),
        "n_events": len(events),
        "n_links": len(links),
        "n_event_clusters": len(clusters),
        "confirmed_links": sum(1 for l in links if l["status"] == "confirmed"),
        "confirmed_clusters": sum(1 for c in clusters if c["status"] == "confirmed"),
    }
