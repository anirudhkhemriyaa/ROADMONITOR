"""Unit tests for the rule-based detector and severity module.

The signal generator follows the same synthetic-signal discipline as the
Phase A pipeline tests: each rule gets a signal that satisfies exactly one
rule, plus controls (gyro turn, stationary/parked).
"""
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
os.environ["ROADMONITOR_DB"] = "sqlite:///" + str(Path(tempfile.mkdtemp()) / "anom.db")

from roadmonitor.anomaly import detect, ingest, severity  # noqa: E402

HZ = 50
CFG = {
    "anomaly": {
        "sample_hz": HZ,
        "base": {"dip_abs_ms2": 4.0, "spike_abs_ms2": 9.0,
                 "pothole_window_ms": [40, 200], "bump_window_ms": [200, 600],
                 "bump_peak_abs_ms2": 5.0, "rough_rms_ms2": 2.4,
                 "rough_window_ms": 1000, "rough_overlap": 0.6,
                 "gyro_z_rad_s": 0.5, "gyro_shield_ms": 400, "refractory_ms": 300},
        "vehicle_scale": {"bike": 1.0, "car": 1.2, "bus": 1.5,
                          "e_rickshaw": 0.75},
    },
    "severity": {
        "weights": {"p95": 0.6, "rms": 0.4},
        "refs": {"bike": {"p95": 6.0, "rms": 2.5},
                 "car": {"p95": 4.5, "rms": 1.8}},
    },
    "speed": {"min_kmh": 5.0, "max_kmh": 120.0},
}


def make_rows(vert_fn, gyro_fn=lambda i: 0.0, n=15 * HZ, speed_kmh=30.0,
              gps=True):
    """Rows with constant speed, straight line of GPS, gravity = -Z so the
    stereo formula reduces to vert[i] - g (gravity removal in code)."""
    rows = []
    for i in range(n):
        ts = 1_760_000_000_000 + i * 1000 // HZ
        v = vert_fn(i)
        rows.append({
            "timestamp_ms": ts, "ax": 0.0, "ay": 0.0, "az": 9.81 + v,
            "lat": 26.2183 + i * 1e-5 if gps else None,
            "lon": 78.1828 if gps else None,
            "speed_kmh": speed_kmh, "grav_x": 0.0, "grav_y": 0.0,
            "grav_z": 9.81, "gz": gyro_fn(i),
        })
    return rows


def test_vertical_accel_removes_gravity():
    rows = make_rows(lambda i: 0.0)                 # flat road: a_vert ~ 0
    verts = ingest.vertical_accel(rows)
    assert max(abs(v) for v in verts) < 1e-6
    rows_plus = make_rows(lambda i: 5.0 if 100 <= i < 110 else 0.0)
    verts2 = ingest.vertical_accel(rows_plus)
    assert abs(verts2[105] - 5.0) < 1e-6            # signal preserved


def test_legacy_rows_fall_back_to_highpass():
    rows = [{"timestamp_ms": i * 20, "az": 9.81, "lat": None, "lon": None}
            for i in range(300)]
    rows[200] = dict(rows[200], az=15.0)
    verts = ingest.vertical_accel(rows)
    assert abs(verts[200] - 5.0) < 2.5              # spike isolated, not row value


def test_pothole_detected():
    def vert(i):
        if i in range(100, 103):
            return -6.0                             # dip
        if i in range(110, 114):
            return 12.0                             # impact spike
        return 0.0
    ev = detect.run(make_rows(vert), "bike", CFG)
    kinds = [e["event_type"] for e in ev]
    assert "pothole" in kinds


def test_no_pothole_without_dip():
    def vert(i):
        return 12.0 if i in range(110, 114) else 0.0   # spike only
    ev = detect.run(make_rows(vert), "bike", CFG)
    assert "pothole" not in [e["event_type"] for e in ev]


def test_speed_bump_detected():
    def vert(i):
        if i < 200 or i > 250:
            return 0.0
        # smooth 500 ms half-wave peaking at 6 (< spike 9)
        p = (i - 200) / 50.0
        return 6.0 * (1 - abs(2 * p - 1)) ** 2
    ev = detect.run(make_rows(vert), "bike", CFG)
    assert "speed_bump" in [e["event_type"] for e in ev]


def test_rough_road_sustained():
    def vert(i):
        return (3.2 if (i // 10) % 2 == 0 else -3.2) if i < 400 else 0.0
    ev = detect.run(make_rows(vert), "bike", CFG)
    assert "rough" in [e["event_type"] for e in ev]


def test_gyro_gate_drops_events_near_turns():
    def vert(i):
        if i in range(100, 103):
            return -6.0
        if i in range(110, 114):
            return 12.0
        return 0.0
    gyro = lambda i: 0.9 if 90 <= i < 140 else 0.0   # swerve around the dip
    ev = detect.run(make_rows(vert, gyro_fn=gyro), "bike", CFG)
    assert "pothole" not in [e["event_type"] for e in ev]


def test_speed_gate_blocks_stationary_hits():
    def vert(i):
        if i in range(100, 103):
            return -6.0
        if i in range(110, 114):
            return 12.0
        return 0.0
    ev = detect.run(make_rows(vert, speed_kmh=0.0), "bike", CFG)
    assert ev == []                                  # parked jolt is not a road defect


def test_vehicle_scale_changes_thresholds():
    t_bike = detect.scale_thresholds(CFG, "bike")
    t_bus = detect.scale_thresholds(CFG, "bus")
    assert t_bus["spike_abs_ms2"] == t_bike["spike_abs_ms2"] * CFG["anomaly"]["vehicle_scale"]["bus"]


def test_events_have_output_schema_and_latlon():
    def vert(i):
        if i in range(100, 103):
            return -6.0
        if i in range(110, 114):
            return 12.0
        return 0.0
    ev = detect.run(make_rows(vert), "bike", CFG)
    assert ev, "expected detections"
    for e in ev:
        assert {"event_type", "ts", "lat", "lon", "vehicle_type"} <= set(e)
        assert e["lat"] is not None and e["lon"] is not None
        assert e["vehicle_type"] == "bike"


def test_severity_bounds_and_vehicle_normalisation():
    big = severity.event_severity({"rms": 4.0, "p95": 12.0}, "bike", CFG)
    mid = severity.event_severity({"rms": 2.0, "p95": 4.0}, "bike", CFG)
    zero = severity.event_severity({"rms": 0.0, "p95": 0.0}, "bike", CFG)
    assert big > mid > zero >= 0.0 and big <= 1.0
    # same raw magnitude on a heavier vehicle: tighter anchors push the
    # normalised severity up (effort-adjusted, comparable across the fleet)
    car = severity.event_severity({"rms": 2.0, "p95": 4.0}, "car", CFG)
    bike = severity.event_severity({"rms": 2.0, "p95": 4.0}, "bike", CFG)
    assert car > bike
    assert severity.enrich([], CFG) == []
    e = severity.enrich([{"event_type": "rough", "vehicle_type": "car",
                          "features": {"rms": 1.5}}], CFG)[0]
    assert e["method"] == "threshold-based, unvalidated"
    assert 0.0 < e["severity"] < 1.0


def test_10m_windows():
    rows = make_rows(lambda i: 1.0 if i % 3 == 0 else -1.0)  # rms ≈ 0.816
    verts = ingest.vertical_accel(rows)
    for i, r in enumerate(rows):                    # give it a track
        r["lat"] = 26.2183 + i * (10.0 / 111_320.0) * 10
        r["lon"] = 78.1828
    # 15 s of travel: ~4 km/h... speed it up instead: 1.1 m per row * 750 rows
    rows = make_rows(lambda i: 1.0 if i % 3 == 0 else -1.0, n=750)
    for i, r in enumerate(rows):
        r["lat"] = 26.2183 + i * (1.1 / 111_320.0)
        r["lon"] = 78.1828
    verts = ingest.vertical_accel(rows)
    segs = ingest.windows_10m(rows, verts)
    assert len(segs) >= 10
    for s in segs[:3]:
        assert 0.9 < s["rms"] <= 1.0                 # alternating ±1 rms = 1.0
        assert s["n"] > 0 and s["p95"] >= s["rms"] - 1e-9


def test_ingest_roundtrip_into_store():
    from roadmonitor.store import Store
    from roadmonitor.anomaly.schema import ensure_tables
    s = Store("sqlite:///" + str(Path(tempfile.mkdtemp()) / "i.db"))
    ensure_tables(s)
    rows = make_rows(lambda i: 1.0 if i % 3 == 0 else -1.0, n=750)
    for i, r in enumerate(rows):
        r["lat"] = 26.2183 + i * (11.0 / 111_320.0)
        r["lon"] = 78.1828
    data = ("\n".join([",".join(str(round(r[k], 6)) if r[k] is not None else ""
                                for k in ("timestamp_ms", "ax", "ay", "az",
                                          "lat", "lon", "speed_kmh"))
                       for r in rows])).encode()
    res = ingest.ingest_trip(s, "tripAnom1", "bike", data)
    assert res["duplicate"] is False
    assert res["n_windows"] == res["n_segments"] > 20    # 11 m/row → ~68 windows
    types = s.conn.execute(
        "SELECT COUNT(*) FROM anomaly_events").fetchone()[0]
    assert types == 0                                # detect fills it separately
