"""Phase C aggregation/clustering tests: synthetic signals for the server side
(same discipline as the Phase A pipeline unit tests)."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
DB_TMP = Path(tempfile.mkdtemp())
os.environ["ROADMONITOR_DB"] = "sqlite:///" + str(DB_TMP / "proc_test.db")

from roadmonitor.processing.aggregate import (  # noqa: E402
    build_event_clusters,
    build_links,
    classify_scores,
    recency_weight,
    speed_norm,
)
from roadmonitor.processing.dbscan import NOISE, cluster  # noqa: E402
from roadmonitor.geo import haversine_m  # noqa: E402


CFG = {
    "cluster": {"eps_m": 8.0, "min_samples": 2,
                "confirm_min_devices": 2, "confirm_min_trips": 3},
    "aggregate": {"recency_tau_days": 60.0, "conf_trips_weight": 0.5,
                  "ref_speed_mps": 8.33},
    "roughness": {"speed_alpha": 0.7, "speed_floor_mps": 1.5},
    "classes": {"cuts": [50, 75, 90]},
}
NOW_MS = 1_760_000_000_000.0


def seg(trip_id, device_id, lat, lon, rms=0.5, p95=1.0, speed=8.33,
        ts=NOW_MS - 1000, n=200):
    return {"trip_id": trip_id, "row_key": lat * 1e5 + lon * 1e3, "device_id": device_id,
            "lat": lat, "lon": lon, "dist_m": 0.0, "rms": rms, "p95": p95,
            "speed_mean": speed, "ts_start": ts, "received_at_ms": NOW_MS}


def ev(trip_id, device_id, lat, lon, peak=4.0, width=60.0, ts=NOW_MS - 1000):
    return {"trip_id": trip_id, "row_key": lat * 1e5, "device_id": device_id,
            "lat": lat, "lon": lon, "peak": peak, "width_ms": width,
            "type_hint": "pothole_candidate", "ts": ts, "received_at_ms": NOW_MS}


# ---- primitives ------------------------------------------------------------------

def test_haversine_known_value():
    # Delhi ~ 74.2 km from Meerut along that bearing; a rough check is enough
    d = haversine_m(28.7041, 77.1025, 28.9845, 77.7064)
    assert 65000 < d < 100000


def test_dbscan_two_clusters_and_noise():
    pts = [(23.0, 79.0), (23.0, 79.00001), (23.00002, 79.0),   # tight cluster
           (24.0, 80.0), (24.0, 80.00001), (24.00002, 80.0),   # second cluster
           (30.0, 30.0)]                                       # noise point (min_pts=3)
    labels = cluster(pts, eps_m=15.0, min_pts=3)
    groups = [l for l in labels if l != NOISE]
    assert labels[6] == NOISE
    assert groups.count(0) == 3 and groups.count(1) == 3


def test_speed_normalisation_reduces_fast_bins():
    fast = speed_norm(1.0, 16.66, alpha=0.7, ref_speed_mps=8.33, floor_mps=1.5)
    slow = speed_norm(1.0, 4.0, alpha=0.7, ref_speed_mps=8.33, floor_mps=1.5)
    assert fast < 1.0 < slow
    # speeds below the floor are clamped to the floor
    assert speed_norm(1.0, 0.1, 0.7, 8.33, 1.5) == speed_norm(1.0, 1.5, 0.7, 8.33, 1.5)


def test_recency_decay():
    fresh = recency_weight(NOW_MS - 1000, NOW_MS, tau_days=60.0)
    month_old = recency_weight(NOW_MS - 30 * 86_400_000, NOW_MS, tau_days=60.0)
    year_old = recency_weight(NOW_MS - 400 * 86_400_000, NOW_MS, tau_days=60.0)
    assert 0.99 < fresh <= 1.0
    assert 0 < year_old < month_old < fresh


def test_classify_percentile_cuts():
    scores = list(range(100))          # 0..99 uniform
    letters = classify_scores(scores, cuts=[50, 75, 90])
    assert letters[40] == "A"          # < p50
    assert letters[60] == "B"          # p50..p75
    assert letters[80] == "C"          # p75..p90
    assert letters[95] == "D"          # > p90


# ---- end-to-end on the store ------------------------------------------------------

@pytest.fixture(scope="module")
def store():
    from roadmonitor.store import Store, set_store
    s = Store(os.environ["ROADMONITOR_DB"])
    set_store(s)
    return s


def _plant_links_store(s, devices=("devA", "devB"), baseline_rms=0.4, bad_rms=4.0):
    """Two simulated phones drive the same road: a smooth stretch and a
    planted bad patch far away from it."""
    for d_i, dev in enumerate(devices):
        smooth = [seg(f"tripS{d_i}", dev, 23.0 + i * 1e-5, 79.0, rms=baseline_rms,
                      p95=baseline_rms, ts=NOW_MS - (i + 1) * 86_400_000)
                  for i in range(6)]
        bad = [seg(f"tripS{d_i}", dev, 23.1 + i * 1e-5, 79.0, rms=bad_rms,
                   p95=bad_rms, ts=NOW_MS - (i + 1) * 86_400_000)
               for i in range(3)]
        s.insert_trip({"trip_id": f"tripS{d_i}", "device_id": dev,
                       "vehicle_type": "bike", "effective_hz": 50.0},
                      events=[], segments=[])
        # direct insert (bypasses the API code path that decodes the phone JSON);
        # per-bin columns: rms, std, max_a, p95 use the planted amplitudes
        rows = smooth + bad
        s.conn.executemany(
            "INSERT INTO segments (trip_id, ts_start, ts_end, lat, lon, dist_m,"
            " rms, std, max_a, p95, kurtosis, band_0_5, band_5_15, band_15p,"
            " speed_mean, n) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["trip_id"], int(r["ts_start"]), int(r["ts_start"]) + 10000,
              r["lat"], r["lon"], r["dist_m"], r["rms"], r["rms"], r["rms"] * 2,
              r["p95"], 0.0, 0.0, 0.0, 0.0, r["speed_mean"], 200) for r in rows],
        )
        s.conn.commit()


def _plant_events_store(s):
    """Two devices x two trips report the same pothole; one isolated glitch."""
    for d_i, dev in enumerate(("devE1", "devE2")):
        for t in range(2):
            tid = f"tripE{d_i}{t}"
            s.insert_trip({"trip_id": tid, "device_id": dev}, events=[], segments=[])
            s.conn.execute(
                "INSERT INTO events (trip_id, ts, lat, lon, peak, width_ms, type_hint)"
                " VALUES (?,?,?,?,?,?,?)",
                (tid, NOW_MS, 23.5 + d_i * 1e-6, 79.5 + t * 1e-6, 6.0, 60.0,
                 "pothole_candidate"))
            s.conn.execute(
                "INSERT INTO events (trip_id, ts, lat, lon, peak, width_ms, type_hint)"
                " VALUES (?,?,?,?,?,?,?)",
                (tid, NOW_MS, 40.0, 40.0 + (d_i * 2 + t) * 5e-4, 4.0, 60.0,
                 "rough"))  # lone spot (unique, >8 m from every other event)
        s.conn.commit()


def test_link_aggregation_end_to_end(store):
    _plant_links_store(store)
    from roadmonitor.process import recompute_all
    result = recompute_all(store)
    assert result["n_links"] >= 2
    by_score = store.links(min_status="all")
    bad = [l for l in by_score if l["score_raw"] > 2.0]
    smooth = [l for l in by_score if l["score_raw"] < 1.0]
    assert bad and smooth, by_score
    assert bad[0]["class"] == "D" and bad[0]["status"] == "candidate"
    # 2 devices, 2 trips -> confirmed requires 3 trips; stays candidate
    assert bad[0]["n_devices"] == 2 and bad[0]["n_trips"] == 2
    smooth_link = smooth[0]
    assert smooth_link["n_trips"] == 2


def test_event_clustering_end_to_end(store):
    _plant_events_store(store)
    from roadmonitor.process import recompute_all
    recompute_all(store)
    clusters = store.event_clusters(min_status="all")
    assert clusters, "expected at least one event cluster"
    big = [c for c in clusters if c["n_events"] >= 4]
    assert big and big[0]["n_devices"] == 2 and big[0]["n_trips"] == 4
    # 4 trips (2 per device) + 2 devices clears the confirm thresholds
    assert big[0]["status"] == "confirmed"
    assert big[0]["peak_med"] == pytest.approx(6.0)
    # the pothole is the only cluster: the lone events (>8 m from each other)
    # each miss min_samples and stay unclustered noise
    assert len(clusters) == 1


def test_confirmed_requires_min_trips_and_devices(store):
    from roadmonitor.process import recompute_all
    # add a third trip from a third device on the shared pothole
    s = store
    s.insert_trip({"trip_id": "tripE9", "device_id": "devE3"}, events=[], segments=[])
    for i in range(3):
        s.conn.execute(
            "INSERT INTO events (trip_id, ts, lat, lon, peak, width_ms, type_hint)"
            " VALUES (?,?,?,?,?,?,?)",
            (f"tripE9", NOW_MS, 23.5 + (i % 2) * 1e-6, 79.5 + (i % 2) * 1e-6,
             6.0, 60.0, "pothole_candidate"))
        s.conn.commit()
    recompute_all(store)
    big = [c for c in store.event_clusters(min_status="confirmed")
           if c["n_events"] >= 5]
    assert big, "pothole cluster should be confirmed with 3 trips + 2 devices"
    assert big[0]["confidence"] == 1.0


def test_gpsless_bins_do_not_break_recompute(store):
    from roadmonitor.process import recompute_all
    store.insert_trip({"trip_id": "tripNoGps", "device_id": "dev0"}, events=[], segments=[])
    store.conn.execute(
        "INSERT INTO segments (trip_id, ts_start, ts_end, lat, lon, dist_m, rms,"
        " speed_mean, n) VALUES (?,?,?,?,?,?,?,?,?)",
        ("tripNoGps", int(NOW_MS), int(NOW_MS) + 10000, None, None, 10.0, 1.0, 8.0, 200))
    store.conn.commit()
    r = recompute_all(store)          # must not raise
    assert r["n_segment_bins"] >= 1


def test_analysis_endpoints(store):
    from fastapi.testclient import TestClient
    from roadmonitor.main import application
    c = TestClient(application)
    links = c.get("/api/v1/analysis/links").json()
    events = c.get("/api/v1/analysis/events").json()
    assert isinstance(links, list) and isinstance(events, list)
    r = c.post("/api/v1/analysis/recompute")
    assert r.status_code == 200 and r.json()["n_links"] >= 2
