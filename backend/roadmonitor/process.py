"""Phase C orchestrator: recompute road links + event clusters from the store.

Called by the /api/v1/analysis/recompute endpoint (inline, dev mode) and by
tasks.py (Celery, docker deployment). Full rebuild keeps the aggregates
consistent without needing incremental bookkeeping at this data scale.
"""
from .config import CFG
from .processing import aggregate as agg
from .store import get_store


def recompute_all(store=None) -> dict:
    s = store or get_store()
    now = agg.now_ms_utc()
    segs = s.all_segments_with_meta()
    events = s.all_events_with_meta()

    links = agg.build_links(segs, CFG, now)
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
