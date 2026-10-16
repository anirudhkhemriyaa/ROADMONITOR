"""One-call orchestrator for the anomaly pipeline (used by the API and the
simulate_trips.py demo):

    net -> detect -> severity -> mapmatch -> aggregate -> priority

Everything stays rule-based ("threshold-based, unvalidated").
"""
from . import aggregate as agg_mod
from . import mapmatch as mm_mod
from . import priority as prio_mod
from . import roadnet as rn_mod
from . import severity as sev_mod
from .detect import run as detect_run


def process_trip_csv(store, trip_id: str, vehicle_type: str, data: bytes,
                     cfg, source_path: str = "memory") -> dict:
    """Ingest one trip from CSV bytes and run detection+severity+snapping on
    the freshly loaded network. Returns counts."""
    from .ingest import ingest_trip, parse_trip_csv
    net = rn_mod.load_network(store, "auto")
    rows = parse_trip_csv(data)
    ing = ingest_trip(store, trip_id, vehicle_type, data,
                      source_path=source_path)
    events = detect_run(rows, vehicle_type, cfg)
    events = sev_mod.enrich(events, cfg)
    for e in events:
        e["trip_id"] = trip_id
    events = mm_mod.assign(events, net,
                           radius_m=cfg["anomaly"]["match_radius_m"])
    mm_mod.persist(store, events)
    return {"ingest": ing, "events": len(events)}


def recompute(store, cfg, net_mode: str = "auto") -> dict:
    """Network (re)load + aggregate + priority full recompute."""
    net = rn_mod.load_network(store, net_mode)
    agg = agg_mod.compute(store, cfg)
    prio = prio_mod.compute(store, net, cfg)
    return {
        "net_source": net.get("source"),
        "n_segments": len(net["segments"]),
        "n_pois": len(net["pois"]),
        "segments_with_events": agg["segments_with_events"],
        "confirmed": agg["confirmed"],
        "prioritised": prio["scored"],
        "method": "threshold-based, unvalidated",
    }
