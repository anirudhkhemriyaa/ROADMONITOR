"""Roughness aggregation, classification and confidence (Phase C).

All thresholds come from config.yaml (loaded in roadmonitor.config) - no magic
numbers here. Pure functions only; process.py wires them to the store.
"""
import hashlib
from datetime import datetime, timezone

from .dbscan import NOISE, cluster

# Speed normalisation + recency live in config; see cfg_get usage below.


def speed_norm(rms: float, speed_mps: float, alpha: float,
               ref_speed_mps: float, floor_mps: float) -> float:
    """Normalises roughness to the reference speed: on the same road, a
    phone at 60 km/h records more shake than one at 30 km/h, so raw bins are
    scaled by (v / v_ref) ** -alpha before averaging."""
    v = max(speed_mps if speed_mps and speed_mps > 0 else 0.0, floor_mps)
    return rms * (v / ref_speed_mps) ** (-alpha)


def recency_weight(ts_ms: float, now_ms: float, tau_days: float) -> float:
    """Exponential age decay: exp(-age_days / tau_days)."""
    age_days = max(0.0, (now_ms - ts_ms)) / 86_400_000.0
    return 2.718281828459045 ** (-age_days / max(tau_days, 1e-6))


def _percentile(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    i = p / 100.0 * (len(sorted_vals) - 1)
    lo = int(i)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = i - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def classify_scores(scores: list, cuts: list) -> list:
    """Percentile cuts (config classes.cuts, e.g. [50, 75, 90]) turn the score
    distribution into classes: A (smoothest) .. D (worst)."""
    letters = ["A", "B", "C", "D"]
    s = sorted(scores)
    bounds = [_percentile(s, c) for c in cuts]
    out = []
    for v in scores:
        k = 0
        for b in bounds:
            if v > b:
                k += 1
        out.append(letters[k])
    return out


def _link_id(members_idx: list, rows: list) -> str:
    key = ";".join(sorted(f"{rows[i]['trip_id']}:{rows[i]['row_key']}"
                          for i in members_idx))
    return "lnk_" + hashlib.sha1(key.encode()).hexdigest()[:12]


def build_links(seg_rows: list, cfg: dict, now_ms: float,
                link_keys: list = None) -> list:
    """Clusters segment bins into road links and computes their scores.

    seg_rows rows need: trip_id, row_key, device_id, lat, lon, dist_m, rms,
    p95, speed_mean, ts_start, received_at_ms. Bins without GPS are skipped
    (positions unknown -> cannot be placed on a map; data stays in the store
    and in the on-device CSV).

    link_keys (optional) is aligned with `placed` order (see process.py) and
    gives an OSM node-pair per bin from mapmatch.match_points; when provided,
    bins group by that key first and only unmatched bins (None) fall back to
    DBSCAN clustering. Without map matching everything groups via DBSCAN.
    """
    eps = cfg["cluster"]["eps_m"]
    min_pts = cfg["cluster"]["min_samples"]
    tau = cfg["aggregate"]["recency_tau_days"]
    alpha = cfg["roughness"]["speed_alpha"]
    ref_v = cfg["aggregate"]["ref_speed_mps"]
    floor_v = cfg["roughness"]["speed_floor_mps"]
    w_trips = cfg["aggregate"]["conf_trips_weight"]
    min_trips = cfg["cluster"]["confirm_min_trips"]
    min_devices = cfg["cluster"]["confirm_min_devices"]
    cuts = cfg["classes"]["cuts"]

    placed = [r for r in seg_rows if r["lat"] is not None and r["lon"] is not None]
    if not placed:
        return []

    groups = {}
    if link_keys is not None and len(link_keys) == len(placed):
        unmatched = []
        for i, key in enumerate(link_keys):
            if key is None:
                unmatched.append(i)
            else:
                groups.setdefault(key, []).append(i)
        if unmatched:
            labels = cluster([(placed[i]["lat"], placed[i]["lon"])
                              for i in unmatched], eps, min_pts)
            fallback = {}
            for i, label in zip(unmatched, labels):
                if label != NOISE:
                    fallback.setdefault(label, []).append(i)
            # key "dbscan:<label>" cannot collide with "osm:u:v"
            for label, idxs in fallback.items():
                groups[f"dbscan:{label}"] = idxs
    else:
        labels = cluster([(r["lat"], r["lon"]) for r in placed], eps, min_pts)
        for i, r in enumerate(placed):
            if labels[i] != NOISE:
                groups.setdefault(labels[i], []).append(i)

    links = []
    for members in groups.values():
        weights = []
        vals_norm = []
        vals_raw = []
        for i in members:
            r = placed[i]
            w = r["n"] * recency_weight(r["ts_start"], now_ms, tau)
            weights.append(w)
            vals_raw.append((r["p95"] or 0.0, w))
            vals_norm.append((speed_norm(r["rms"] or 0.0, r["speed_mean"], alpha,
                                         ref_v, floor_v), w))
        wsum = sum(weights) or 1.0
        trips = {r["trip_id"] for r in (placed[i] for i in members)}
        devices = {r["device_id"] for r in (placed[i] for i in members)}
        links.append({
            "link_id": _link_id(members, placed),
            "lat": sum(placed[i]["lat"] * placed[i]["n"] for i in members) /
                   sum(placed[i]["n"] for i in members),
            "lon": sum(placed[i]["lon"] * placed[i]["n"] for i in members) /
                   sum(placed[i]["n"] for i in members),
            "score_raw": sum(v * w for v, w in vals_raw) / wsum,
            "score_norm": sum(v * w for v, w in vals_norm) / wsum,
            "n_bins": len(members),
            "n_trips": len(trips),
            "n_devices": len(devices),
            "last_seen_ms": max(r["ts_start"] for r in (placed[i] for i in members)),
        })

    letters = classify_scores([l["score_norm"] for l in links], cuts)
    for l, letter in zip(links, letters):
        l["class"] = letter
        conf_t = min(1.0, l["n_trips"] / max(min_trips, 1))
        conf_d = min(1.0, l["n_devices"] / max(min_devices, 1))
        l["confidence"] = round(w_trips * conf_t + (1 - w_trips) * conf_d, 3)
        l["status"] = ("confirmed" if l["n_trips"] >= min_trips
                       and l["n_devices"] >= min_devices else "candidate")
    return links


def build_event_clusters(event_rows: list, cfg: dict, now_ms: float) -> list:
    """Groups detected events into suspected potholes / breakers.

    An event without lat/lon cannot be clustered (gyro-rejected or GPS-less
    rows keep device-local only). status/confidence follow the cluster config.
    """
    eps = cfg["cluster"]["eps_m"]
    min_pts = cfg["cluster"]["min_samples"]
    min_trips = cfg["cluster"]["confirm_min_trips"]
    min_devices = cfg["cluster"]["confirm_min_devices"]
    w_trips = cfg["aggregate"]["conf_trips_weight"]
    tau = cfg["aggregate"]["recency_tau_days"]

    placed = [e for e in event_rows if e["lat"] is not None and e["lon"] is not None]
    if not placed:
        return []
    labels = cluster([(e["lat"], e["lon"]) for e in placed], eps, min_pts)

    groups = {}
    for i, e in enumerate(placed):
        if labels[i] != NOISE:
            groups.setdefault(labels[i], []).append(i)

    out = []
    for members in groups.values():
        evs = [placed[i] for i in members]
        trips = {e["trip_id"] for e in evs}
        devices = {e["device_id"] for e in evs}
        peaks = sorted(e["peak"] or 0.0 for e in evs)
        widths = sorted(e["width_ms"] or 0.0 for e in evs)
        types = [e["type_hint"] or "rough" for e in evs]
        rec = min(recency_weight(e["ts"], now_ms, tau) for e in evs)
        conf_t = min(1.0, len(trips) / max(min_trips, 1))
        conf_d = min(1.0, len(devices) / max(min_devices, 1))
        out.append({
            "cluster_id": _link_id(members, placed),
            "lat": sum(e["lat"] for e in evs) / len(evs),
            "lon": sum(e["lon"] for e in evs) / len(evs),
            "n_events": len(evs),
            "n_trips": len(trips),
            "n_devices": len(devices),
            "peak_med": _percentile(peaks, 50),
            "width_med": _percentile(widths, 50),
            "type": max(set(types), key=types.count),
            "recency": round(rec, 3),
            "status": ("confirmed" if len(trips) >= min_trips
                       and len(devices) >= min_devices else "candidate"),
            "confidence": round(w_trips * conf_t + (1 - w_trips) * conf_d, 3),
        })
    return out


def now_ms_utc() -> float:
    return datetime.now(timezone.utc).timestamp() * 1000.0
