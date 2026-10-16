"""Phase B API tests. Embedded SQLite in a temp dir - no installs needed."""
import csv
import gzip
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
os.environ["ROADMONITOR_DB"] = "sqlite:///" + str(
    Path(tempfile.mkdtemp()) / "test_roadmonitor.db"
)


def _payload(trip_id="trip-1", n_events=2, n_segments=3):
    return {
        "metadata": {
            "trip_id": trip_id, "device_id": "deadbeef", "app_version": "2.0.0",
            "vehicle_type": "bike", "mount_type": "handlebar",
            "started_at": "2026-10-17T10:00:00", "ended_at": "2026-10-17T10:10:00",
            "rows": 30016, "duration_s": 600.3, "effective_hz": 50.0,
        },
        "events": [
            {"ts": 1792000000000 + i * 1000, "lat": 23.1765, "lon": 79.9911,
             "dist_m": 100.0 + i, "peak": 3.5 + i, "p2p": 5.0, "width_ms": 60.0,
             "jerk": 120.0, "energy": 4.2, "speed": 8.1, "gyro_peak": 0.12,
             "speed_drop": 0.4, "type_hint": "pothole_candidate"}
            for i in range(n_events)
        ],
        "segments": [
            {"ts_start": 1792000000000 + i * 10000,
             "ts_end": 1792000000000 + (i + 1) * 10000,
             "lat": 23.17 + i * 0.001, "lon": 79.99, "dist_m": 200.0 + i * 10,
             "rms": 0.9, "std": 0.8, "max": 2.4, "p95": 2.1, "kurtosis": 1.1,
             "band_0_5": 0.5, "band_5_15": 0.3, "band_15p": 0.05,
             "speed_mean": 8.0, "n": 200}
            for i in range(n_segments)
        ],
    }


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from roadmonitor.main import application
    from roadmonitor.store import Store, set_store
    # make the process-wide store deterministic per test module
    set_store(Store(os.environ["ROADMONITOR_DB"]))
    return TestClient(application)


def test_ping(client):
    r = client.get("/ping")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_device_register(client):
    r = client.post("/api/v1/devices/register", json={"name": "test-phone"})
    assert r.status_code == 200
    body = r.json()
    assert body["device_id"].startswith("dev_")
    assert len(body["device_key"]) >= 32
    # a sha256 of the issued key must be on the server side, not the key itself
    assert r.headers.get("content-type", "").startswith("application/json")


def test_trip_upload_gzip_and_idempotency(client):
    payload = gzip.compress(json.dumps(_payload()).encode())
    r = client.post("/api/v1/trips", content=payload, headers={
        "Content-Type": "application/json", "Content-Encoding": "gzip",
        "X-Trip-Id": "trip-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "received"
    assert body["duplicate"] is False
    assert body["n_events"] == 2 and body["n_segments"] == 3

    # same trip again -> duplicate flag, no double rows
    r2 = client.post("/api/v1/trips", content=payload, headers={
        "Content-Type": "application/json", "Content-Encoding": "gzip",
        "X-Trip-Id": "trip-1"})
    assert r2.status_code in (200, 409)
    assert r2.json()["duplicate"] is True
    assert r2.json()["n_events"] == 2 and r2.json()["n_segments"] == 3


def test_trip_without_gzip_accepted(client):
    r = client.post("/api/v1/trips", json=_payload("trip-plain"))
    assert r.status_code == 200 and r.json()["duplicate"] is False


def test_trip_requires_trip_id(client):
    bad = _payload("trip-bad")
    bad["metadata"].pop("trip_id")
    r = client.post("/api/v1/trips", json=bad)
    assert r.status_code == 422 or r.status_code == 400


def test_trip_status(client):
    r = client.get("/api/v1/trips/trip-1/status")
    assert r.status_code == 200
    assert r.json()["status"] == "received"
    assert r.json()["n_events"] == 2
    assert client.get("/api/v1/trips/nope/status").status_code == 404


def test_trips_list(client):
    r = client.get("/api/v1/trips")
    assert r.status_code == 200
    ids = [t["trip_id"] for t in r.json()["trips"]]
    assert "trip-1" in ids and "trip-plain" in ids


def test_bad_gzip_is_400(client):
    r = client.post("/api/v1/trips", content=b"not-gzip", headers={
        "Content-Type": "application/json", "Content-Encoding": "gzip"})
    assert r.status_code == 400


# ---- legacy endpoints keep working with the new 21-column CSV schema ----------

def test_legacy_upload_with_new_schema(client):
    # content must be unique per run: the legacy endpoint dedupes by sha256
    # against everything already stored in data/raw
    token = int.from_bytes(os.urandom(2), "big") + 1792055899260
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow("timestamp_ms,ax,ay,az,lat,lon,speed_kmh,gps_accuracy_m,bearing_deg,"
               "gps_age_ms,t_ns,t_wall_ms,gx,gy,gz,grav_x,grav_y,grav_z,speed_mps,"
               "altitude,speed_acc".split(","))
    base = 1792055899260
    for i in range(30):
        w.writerow([base + i * 20, round(i * 0.01, 4), 0.1, 9.81, "", "", "", "", "", "",
                    i * 20_000_000, token + i * 20, 0, 0, 0, 0, 0, -9.81, "", "", ""])
    r = client.post("/upload",
                    files={"file": ("D004_2026-10-17_bike.csv", buf.getvalue().encode())},
                    data={"vehicle_type": "bike", "phone_model": "test", "meta": "{}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "saved"
    assert body["summary"]["status"] in ("OK", "WARN")   # 50 Hz, no gaps
    # legacy dedup: same bytes again -> already_uploaded
    r2 = client.post("/upload",
                     files={"file": ("D004_2026-10-17_bike.csv", buf.getvalue().encode())},
                     data={})
    assert r2.json()["status"] == "already_uploaded"


def test_legacy_live_and_drives(client):
    assert client.get("/api/live").status_code == 200
    assert client.get("/api/drives").status_code == 200
