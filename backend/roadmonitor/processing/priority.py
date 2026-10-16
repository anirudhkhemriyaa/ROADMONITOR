"""Priority ranking of road links (Phase D).

priority_score combines the three drivers from the plan: severity (class
A..D -> 1..4), confidence (0..1 from trips/devices) and recency (exponential
age decay with the same tau as aggregation). Higher = fix first.

Pure function; the dashboard table and any future export consume this.
"""
from .aggregate import recency_weight

_CLASS_RANK = {"A": 1, "B": 2, "C": 3, "D": 4}


def priority_score(link: dict, cfg: dict, now_ms: float) -> float:
    rank = _CLASS_RANK.get(link.get("class"), 1)
    conf = link.get("confidence") or 0.0
    last_seen = link.get("last_seen_ms") or 0.0
    return round(rank * conf * recency_weight(last_seen, now_ms,
                                              cfg["aggregate"]["recency_tau_days"]),
                 4)


def rank_links(links: list, cfg: dict, now_ms: float) -> list:
    """Returns the links sorted by priority score (worst first); each row
    gains a 'priority' field."""
    rows = [dict(l, priority=priority_score(l, cfg, now_ms)) for l in links]
    rows.sort(key=lambda r: (-r["priority"], -r["score_norm"]))
    return rows
