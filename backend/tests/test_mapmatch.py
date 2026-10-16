"""Tests for the OSRM map-matching adapter and its wiring into build_links.

The HTTP transport is faked, so these run without OSRM/Docker (per the
master rules: every algorithm gets a synthetic-signal unit test).
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
DB_TMP = Path(tempfile.mkdtemp())
os.environ["ROADMONITOR_DB"] = "sqlite:///" + str(DB_TMP / "mm_test.db")

from roadmonitor.processing import mapmatch  # noqa: E402
from roadmonitor.processing.aggregate import build_links  # noqa: E402

MM_CFG = {
    "cluster": {"eps_m": 8.0, "min_samples": 2,
                "confirm_min_devices": 2, "confirm_min_trips": 3},
    "aggregate": {"recency_tau_days": 60.0, "conf_trips_weight": 0.5,
                  "ref_speed_mps": 8.33},
    "roughness": {"speed_alpha": 0.7, "speed_floor_mps": 1.5},
    "classes": {"cuts": [50, 75, 90]},
}
NOW_MS = 1_760_000_000_000.0


def seg(trip_id, device_id, lat, lon, rms=0.5, p95=1.0, speed=8.33,
        ts=None, n=200):
    return {"trip_id": trip_id, "row_key": lat * 1e5 + lon * 1e3,
            "device_id": device_id, "lat": lat, "lon": lon, "dist_m": 0.0,
            "rms": rms, "p95": p95, "speed_mean": speed, "n": n,
            "ts_start": ts if ts is not None else NOW_MS - 1000,
            "received_at_ms": NOW_MS}


OSRM_OK = {
    "code": "Ok",
    "tracepoints": [
        {"location": [79.0000, 23.0000], "matchings_index": 0,
         "waypoint_index": 0, "distance": 2.0},
        {"location": [79.0001, 23.0001], "matchings_index": 0,
         "waypoint_index": 1, "distance": 3.0},
        {"location": [79.0002, 23.0002], "matchings_index": 0,
         "waypoint_index": 2, "distance": 5.0},
        None,                                   # unmatched point
    ],
    "matchings": [
        # leg 0: nodes 100->101->102 ; leg 1: nodes 102->103
        {"legs": [
            {"annotation": {"nodes": [100, 101, 101, 102]}},
            {"annotation": {"nodes": [102, 103]}},
        ]},
    ],
}


def test_url_uses_lon_lat_order():
    url = mapmatch.osrm_match_url("http://osrm:5000/",
                                  [(23.0, 79.0), (23.1, 79.1)])
    assert url.startswith("http://osrm:5000/match/v1/driving/79.000000,23.000000;79.100000,23.100000")
    assert "annotations=true" in url


def test_match_points_assigns_node_pairs():
    rec = mapmatch.match_points(lambda url: OSRM_OK, "http://fake/match")
    assert len(rec) == 4
    # waypoint 0 starts leg 0 -> first pair of leg 0
    assert rec[0]["key"] == "osm:100:101"
    # waypoint 1 starts leg 1 -> first pair of leg 1
    assert rec[1]["key"] == "osm:102:103"
    # final waypoint closes leg 1 -> last pair of leg 1
    assert rec[2]["key"] == "osm:102:103"
    # null tracepoint -> None
    assert rec[3] is None
    assert rec[0]["snap_lat"] == 23.0 and rec[0]["snap_lon"] == 79.0


def test_match_points_failure_returns_empty():
    assert mapmatch.match_points(lambda url: {"code": "NoMatch"}, "http://x") == []
    def boom(url):
        raise OSError("connection refused")
    assert mapmatch.match_points(boom, "http://x") == []


def test_match_points_malformed_tracepoint_is_none():
    bad = {"code": "Ok", "matchings": [{"legs": []}], "tracepoints": [None,
           {"location": [1.0, 2.0], "matchings_index": 0, "waypoint_index": 0}]}
    # matching without legs/annotation data must not crash: key falls back to None
    rec = mapmatch.match_points(lambda url: bad, "http://x")
    assert rec == [None, None]


BUILD_LINKS_CFG = MM_CFG
THIRD_TRIP_OK = None  # placeholder, see test below


def test_build_links_groups_by_osm_key_with_dbscan_fallback():
    rows = [
        seg("t1", "d1", 23.0, 79.0, rms=0.2, p95=0.3),   # way A
        seg("t1", "d1", 23.001, 79.001, rms=0.2, p95=0.3),   # way A
        # unmatched bins 11 km away cluster among themselves via DBSCAN
        seg("t1", "d1", 30.0, 30.0, rms=1.0, p95=1.5),
        seg("t2", "d2", 30.0, 30.00001, rms=1.0, p95=1.5),
    ]
    keys = ["osm:100:101", "osm:100:101", None, None]
    links = build_links(rows, BUILD_LINKS_CFG, NOW_MS, link_keys=keys)
    # way A bins share one link even though placed 140 m apart (way beyond
    # eps_m=8), the unmatched pair forms exactly one DBSCAN cluster
    assert len(links) == 2
    by_lat = {round(l["lat"], 3): l for l in links}
    way = by_lat[23.001]          # centroid of the two way-A bins
    assert way["n_bins"] == 2 and way["n_trips"] == 1 and way["n_devices"] == 1
    cluster2 = [l for l in links if l is not way][0]
    assert cluster2["n_bins"] == 2 and abs(cluster2["lat"] - 30.0) < 1e-5
