"""Phase D tests: priority ranking + the dashboard page/endpoint."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
DB_TMP = Path(tempfile.mkdtemp())
os.environ["ROADMONITOR_DB"] = "sqlite:///" + str(DB_TMP / "dash_test.db")

from roadmonitor.processing.priority import priority_score, rank_links  # noqa: E402

CFG = {"aggregate": {"recency_tau_days": 60.0}}
NOW_MS = 1_760_000_000_000.0


def link(cls="C", conf=1.0, last_seen_ms=None, score_norm=1.0):
    return {"class": cls, "confidence": conf, "score_norm": score_norm,
            "score_raw": score_norm,
            "last_seen_ms": NOW_MS - 1000 if last_seen_ms is None else last_seen_ms}


def test_class_severity_dominates():
    d = priority_score(link(cls="D"), CFG, NOW_MS)
    a = priority_score(link(cls="A"), CFG, NOW_MS)
    assert d > a
    # same class ordering for equal confidence/recency
    scores = {c: priority_score(link(cls=c), CFG, NOW_MS) for c in "ABCD"}
    assert scores["A"] < scores["B"] < scores["C"] < scores["D"]


def test_confidence_scales_priority():
    assert priority_score(link(conf=1.0), CFG, NOW_MS) > \
           priority_score(link(conf=0.2), CFG, NOW_MS)


def test_stale_links_sink():
    fresh = priority_score(link(last_seen_ms=NOW_MS - 1000), CFG, NOW_MS)
    year_old = priority_score(link(last_seen_ms=NOW_MS - 400 * 86_400_000), CFG, NOW_MS)
    assert fresh > year_old > 0
    # no recency information at all -> cannot be a priority
    assert priority_score(link(last_seen_ms=0), CFG, NOW_MS) == 0.0


def test_rank_links_sorted_worst_first():
    rows = [
        link(cls="A", conf=1.0),
        link(cls="D", conf=1.0, score_norm=9.0),   # top
        link(cls="C", conf=1.0, score_norm=5.0),
        # severity x confidence: a barely-witnessed D sinks below a solid A
        link(cls="D", conf=0.1, score_norm=8.0),
    ]
    ranked = rank_links(rows, CFG, NOW_MS)
    assert [r["class"] for r in ranked] == ["D", "C", "A", "D"]
    prios = [r["priority"] for r in ranked]
    assert prios == sorted(prios, reverse=True)
    assert all("priority" in r for r in ranked)


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from roadmonitor.main import application
    from roadmonitor.store import Store, set_store
    set_store(Store(os.environ["ROADMONITOR_DB"]))
    return TestClient(application)


def test_priority_endpoint(client):
    r = client.get("/api/v1/analysis/priority")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    if body:
        assert "priority" in body[0]


def test_dashboard_page_served(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    html = r.text.lower()
    for needle in ("road monitor", "/api/v1/analysis", "leaflet",
                   "recompute", "priority"):
        assert needle in html
