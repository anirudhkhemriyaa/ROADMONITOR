"""Phase E validation: repeatability / cross-device / speed scripts.

Synopsis: python -m roadmonitor.validation

Three controlled checks run on synthetic drives (the "one road, same vehicle"
protocol from the plan, simulated so thresholds are testable here):

1. repeatability  - the same device drives the same road twice: scores and
   classes must reproduce (class concordance + score correlation).
2. cross_device   - two different devices drive the same road: their link
   scores/classes must agree.
3. speed          - the same road at increasing speeds: the speed
   normalisation must cancel the shake-grows-with-speed effect (the planted
   physical model is rms ∝ (v/v_ref)^alpha, the same law the aggregation
   inverts), so score_norm spread across speed runs collapses to ~1 while the
   raw spread does not.

Each scenario uses isolated stores (one drive each) so links never pool.
Pass/fail thresholds live in config.yaml -> validation.
"""
import math
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import CFG
from .process import recompute_all
from .processing.aggregate import now_ms_utc
from .store import Store

# Synthetic road layout (metres north along one bearing):
BIN_STEP_M = 3.0     # spacing of bins inside a zone
ZONE_GAP_M = 12.0    # break between roughness zones (> cluster eps_m so zones never merge)


def synthetic_trip(trip_id: str, device_id: str, speed_mps: float,
                   smooth_rms: float = 0.4, pothole_rms: float = 4.0,
                   medium_scale: float = 3.0, ts_start: float = None,
                   cfg=None) -> list:
    """One drive as a list of segment-bin rows in the API/store schema.

    Roughness zones: smooth, pothole, medium over speed-varying amplitude
    rms(zone) * (v / v_ref) ** alpha (the physical premise the normalisation
    undoes; alpha/ref_speed come from config).
    """
    cfg = cfg or CFG
    v_ref = cfg["aggregate"]["ref_speed_mps"]
    alpha = cfg["roughness"]["speed_alpha"]
    n_bins = cfg["validation"]["n_bins_per_zone"]
    ts0 = ts_start if ts_start is not None else now_ms_utc() - 1000
    shake = (speed_mps / v_ref) ** alpha

    rows = []
    lat = 23.0000
    lon = 79.0000
    lat_per_m = 1.0 / 111_320.0
    for zone_rms in (smooth_rms, pothole_rms, smooth_rms * medium_scale):
        lat += ZONE_GAP_M * lat_per_m          # gap so zones stay separate links
        amp = zone_rms * shake
        for b in range(n_bins):
            lat += BIN_STEP_M * lat_per_m
            rows.append({
                "ts_start": int(ts0) + 10_000 * len(rows),
                "ts_end": int(ts0) + 10_000 * (len(rows) + 1),
                "lat": round(lat, 8), "lon": lon, "dist_m": BIN_STEP_M,
                "rms": amp, "std": amp, "max": 2.0 * amp, "p95": amp,
                "kurtosis": 0.0, "band_0_5": 0.0, "band_5_15": 0.0,
                "band_15p": 0.0, "speed_mean": speed_mps, "n": 200,
            })
    return rows


def seed_drive(trip_id: str, device_id: str, speed_mps: float, cfg=None) -> Store:
    """A fresh store holding exactly one synthetic drive, aggregated."""
    rows = synthetic_trip(trip_id, device_id, speed_mps, cfg=cfg)
    s = Store("sqlite:///" + str(Path(tempfile.mkdtemp()) / "v.db"))
    s.insert_trip({"trip_id": trip_id, "device_id": device_id,
                   "vehicle_type": "bike", "effective_hz": 50.0},
                  events=[], segments=rows)
    recompute_all(s)
    return s


# ---- metrics -----------------------------------------------------------------

def pearson(xs: list, ys: list):
    """Pearson r; None when undefined (n < 2 or zero variance)."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / math.sqrt(sxx * syy)


def nearest(targets: list, lat: float, lon: float, tol_m: float):
    """Index of the closest target within tol_m, else None."""
    from .geo import haversine_m
    best, best_d = None, tol_m
    for j, t in enumerate(targets):
        d = haversine_m(lat, lon, t["lat"], t["lon"])
        if d <= best_d:
            best, best_d = j, d
    return best


def compare_links(links_a: list, links_b: list, tol_m: float) -> dict:
    """Greedy nearest-position matching of two link sets, then class
    concordance + Pearson r over score_norm."""
    from .geo import haversine_m
    used = set()
    pairs = []
    for a in links_a:
        j = nearest(links_b, a["lat"], a["lon"], tol_m)
        if j is not None and j not in used:
            used.add(j)
            pairs.append((a, links_b[j]))
    conc = None
    corr = None
    if pairs:
        same = sum(1 for a, b in pairs if a["class"] == b["class"])
        conc = same / len(pairs)
        cr = pearson([a["score_norm"] for a, _ in pairs],
                     [b["score_norm"] for _, b in pairs])
        corr = cr
    return {"matched": len(pairs), "class_concordance": conc,
            "score_corr": corr}


def _passes(m: dict, cfg) -> dict:
    cfg = cfg or CFG
    conc = m["class_concordance"]
    corr = m["score_corr"]
    ok = (m["matched"] >= 3
          and conc is not None
          and conc >= cfg["validation"]["min_class_concordance"]
          and corr is not None
          and corr >= cfg["validation"]["min_score_corr"])
    return {"matched": m["matched"], "class_concordance": conc,
            "score_corr": None if corr is None else round(corr, 4),
            "pass": ok}


def worst_link_scores(stores: list, key: str) -> list:
    """The score of the worst roughness zone link (max score_raw, i.e. the
    planted pothole zone) in every store."""
    out = []
    for s in stores:
        ls = s.links(min_status="all")
        worst = max(ls, key=lambda l: l["score_raw"])
        out.append(worst[key])
    return out


def run_all(store_factory=seed_drive, cfg=None):
    """Runs the three scenarios on isolated stores; returns the report."""
    cfg = cfg or CFG
    v = cfg["validation"]
    now_tag = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = {"generated_at": now_tag}

    # 1. repeatability: same device, same road, two runs
    rep_a = store_factory("tripRepA", "dev_rep", 8.33, cfg=cfg)
    rep_b = store_factory("tripRepB", "dev_rep", 8.33, cfg=cfg)
    m = compare_links(rep_a.links(min_status="all"),
                      rep_b.links(min_status="all"), v["pos_tol_m"])
    report["repeatability"] = _passes(m, cfg)

    # 2. cross-device: two devices, same road
    cd_a = store_factory("tripCDA", "dev_cdA", 8.33, cfg=cfg)
    cd_b = store_factory("tripCDB", "dev_cdB", 8.33, cfg=cfg)
    m = compare_links(cd_a.links(min_status="all"),
                      cd_b.links(min_status="all"), v["pos_tol_m"])
    report["cross_device"] = _passes(m, cfg)

    # 3. speed: score_norm spread must collapse while raw spread does not
    stores = [store_factory(f"tripSp{i}", f"dev_sp{i}", v_mps, cfg=cfg)
              for i, v_mps in enumerate(cfg["validation"]["speed_runs_mps"])]
    norm = worst_link_scores(stores, "score_norm")
    raw = worst_link_scores(stores, "score_raw")
    norm_ratio = max(norm) / min(norm) if all(n > 0 for n in norm) else None
    raw_ratio = max(raw) / min(raw) if all(r > 0 for r in raw) else None
    report["speed"] = {
        "raw_ratio": None if raw_ratio is None else round(raw_ratio, 3),
        "norm_ratio": None if norm_ratio is None else round(norm_ratio, 3),
        "pass": (norm_ratio is not None
                 and norm_ratio <= cfg["validation"]["max_norm_ratio"]
                 and raw_ratio is not None
                 and raw_ratio > norm_ratio),
    }

    report["pass"] = all(report[k]["pass"] for k in
                         ("repeatability", "cross_device", "speed"))
    return report


if __name__ == "__main__":
    rep = run_all()
    for key in ("repeatability", "cross_device"):
        r = rep[key]
        print(f"{key:15s} pass={r['pass']}  matched={r['matched']}"
              f"  class_concordance={r['class_concordance']}"
              f"  score_corr={r['score_corr']}")
    r = rep["speed"]
    print(f"{'speed':15s} pass={r['pass']}  raw_ratio={r['raw_ratio']}"
          f"  norm_ratio={r['norm_ratio']}")
    print("OVERALL:", "PASS" if rep["pass"] else "FAIL")
    raise SystemExit(0 if rep["pass"] else 1)
