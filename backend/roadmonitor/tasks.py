"""Celery tasks for the Docker deployment (docker-compose.yml).

NOT exercised by the laptop test suite - the tests run the processing
functions inline (backend/tests/test_aggregation.py). With the compose stack
up (redis + db), run: celery -A roadmonitor.tasks worker
"""
from celery import Celery

from .config import CFG
from .store import Store

celery_app = Celery(
    "roadmonitor",
    broker=CFG.get("celery", {}).get("broker", "redis://localhost:6379/0"),
    backend=CFG.get("celery", {}).get("backend", "redis://localhost:6379/1"),
)

# one worker process = one store handle
_store = None


def _get_store():
    global _store
    if _store is None:
        _store = Store(CFG["db"]["url"])
    return _store


@celery_app.task(name="roadmonitor.recompute")
def recompute_task() -> dict:
    from .process import recompute_all
    return recompute_all(_get_store())


@celery_app.task(name="roadmonitor.process_trip")
def process_trip_task(trip_id: str) -> dict:
    """Per-trip hook (ingestion side). Full aggregates are rebuilt by
    recompute_task, so per-trip processing just flags it ready."""
    _get_store().mark_trip_status(trip_id, "received")
    return {"trip_id": trip_id, "queued_for_recompute": True}
