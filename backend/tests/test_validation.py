"""Phase E validation tests: the three controlled protocols on synthetic
drives (repeatability / cross-device / speed normalisation)."""
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
DB_TMP = Path(tempfile.mkdtemp())
os.environ["ROADMONITOR_DB"] = "sqlite:///" + str(DB_TMP / "val_test.db")

from roadmonitor.validation import (  # noqa: E402
    compare_links,
    pearson,
    run_all,
    seed_drive,
    synthetic_trip,
)

FULL_CFG = {
    "aggregate": {"recency_tau_days": 60.0, "conf_trips_weight": 0.5,
                  "ref_speed_mps": 8.33},
    "roughness": {"speed_alpha": 0.7, "speed_floor_mps": 1.5},
    "cluster": {"eps_m": 8.0, "min_samples": 2,
                "confirm_min_devices": 2, "confirm_min_trips": 3},
    "classes": {"cuts": [50, 75, 90]},
    "mapmatch": {"enabled": False},
    "validation": {"pos_tol_m": 25.0, "min_class_concordance": 0.9,
                   "min_score_corr": 0.9, "max_norm_ratio": 1.25,
                   "n_bins_per_zone": 3, "speed_runs_mps": [4.0, 8.33, 16.66]},
}


def test_pearson_edges():
    assert pearson([1.0], [1.0]) is None
    assert pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None  # zero variance
    assert pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == 1.0
    assert pearson([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == -1.0


def test_synthetic_trip_layout():
    rows = synthetic_trip("t", "d", speed_mps=8.33, cfg=FULL_CFG)
    assert len(rows) == 9          # 3 zones x 3 bins
    lats = [r["lat"] for r in rows]
    # three separate zones: gaps between bin-3 and bin-4, bin-6 and bin-7
    gap1 = (lats[3] - lats[2]) / (lats[1] - lats[0])
    assert gap1 > 3                # 12 m gap vs 3 m step
    rms = [r["rms"] for r in rows]
    assert rms[0] == 0.4 and rms[3] == 4.0   # shake factor is 1 at ref speed
    assert len(set(reversed(lats))) == len(lats)  # monotonically north


def test_full_validation_suite_passes():
    report = run_all(cfg=FULL_CFG)
    assert report["repeatability"]["pass"], report
    assert report["cross_device"]["pass"], report
    assert report["speed"]["pass"], report
    assert report["pass"]
    # identical drives -> identical scores: r == 1, concordance == 1
    assert report["repeatability"]["class_concordance"] == 1.0
    assert report["repeatability"]["score_corr"] == 1.0
    # speed: normalisation collapses the physically planted speed effect
    assert report["speed"]["raw_ratio"] > 2.0
    assert report["speed"]["norm_ratio"] <= FULL_CFG["validation"]["max_norm_ratio"]


def test_validation_fails_when_road_changes():
    """Negative control: the second 'drive' is a different road -> the
    validation must not pass."""
    real_seed = seed_drive

    def shifted_seed(trip_id, device_id, speed_mps, cfg=None):
        rows = synthetic_trip(trip_id, device_id, speed_mps, cfg=cfg)
        # stretch the whole second trip 200 m north: no link matches
        rows = [dict(r, lat=r["lat"] + 200.0 / 111_320.0) for r in rows]
        from roadmonitor.store import Store
        from roadmonitor.process import recompute_all
        import tempfile as tf
        from pathlib import Path as P
        s = Store("sqlite:///" + str(P(tf.mkdtemp()) / "v.db"))
        s.insert_trip({"trip_id": trip_id, "device_id": device_id,
                       "vehicle_type": "bike", "effective_hz": 50.0},
                      events=[], segments=rows)
        recompute_all(s)
        return s

    # repeatability run where drive B is a different road, using factory
    # that returns shifted data for the B calls
    calls = {"n": 0}

    def factory(trip_id, device_id, speed_mps, cfg=None):
        calls["n"] += 1
        if "RepB" in trip_id:
            return shifted_seed(trip_id, device_id, speed_mps, cfg)
        return real_seed(trip_id, device_id, speed_mps, cfg)

    report = {"shown": compare_links(
        real_seed("tripRepA", "dev", 8.33, cfg=FULL_CFG).links(min_status="all"),
        shifted_seed("tripRepB", "dev", 8.33, cfg=FULL_CFG).links(min_status="all"),
        FULL_CFG["validation"]["pos_tol_m"])}
    m = report["shown"]
    assert m["score_corr"] is None and m["class_concordance"] is None
    # _passes marks this a fail
    from roadmonitor.validation import _passes
    assert not _passes(m, FULL_CFG)["pass"]


def test_worst_zone_score_consistent_across_speeds():
    """worst_link_scores pulls the pothole-zone link (max raw score) from
    each store; its normalized score must stay flat across speeds."""
    from roadmonitor.validation import worst_link_scores
    stores = [seed_drive(f"tripZ{i}", f"dev{i}", v, cfg=FULL_CFG)
              for i, v in enumerate(FULL_CFG["validation"]["speed_runs_mps"])]
    norms = worst_link_scores(stores, "score_norm")
    for s in stores:
        assert len(s.links(min_status="all")) == 3
    assert max(norms) / min(norms) <= FULL_CFG["validation"]["max_norm_ratio"]
