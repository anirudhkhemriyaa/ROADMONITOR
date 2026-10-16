"""API v1 routes (Phase B).

Auth: optional X-Device-Key (sha256-checked in the store). Lenient until the
Phase A app learns to register keys; flip config api.auth.require_device_key
to enforce.
"""
import gzip
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, field_validator

from .config import cfg_get
from .store import get_store

router = APIRouter(prefix="/api/v1")


# ---- auth -------------------------------------------------------------------

def check_device_key(x_device_key: str | None = Header(default=None)) -> None:
    required = cfg_get("api.auth.require_device_key", False)
    if x_device_key is None:
        if required:
            raise HTTPException(401, "missing X-Device-Key")
        return
    if not get_store().device_key_is_known(x_device_key):
        raise HTTPException(401, "unknown device key")


# ---- devices ------------------------------------------------------------------

class DeviceRegisterIn(BaseModel):
    name: str = ""


@router.post("/devices/register", dependencies=[Depends(check_device_key)])
def device_register(body: DeviceRegisterIn):
    """Returns a per-device secret once. Only its sha256 is stored."""
    return get_store().register_device(body.name)


# ---- trips ---------------------------------------------------------------------

class EventIn(BaseModel):
    ts: int
    lat: float | None = None
    lon: float | None = None
    dist_m: float | None = None
    peak: float | None = None
    p2p: float | None = None
    width_ms: float | None = None
    jerk: float | None = None
    energy: float | None = None
    speed: float | None = None
    gyro_peak: float | None = None
    speed_drop: float | None = None
    type_hint: str = "rough"


class SegmentIn(BaseModel):
    ts_start: int
    ts_end: int
    lat: float | None = None
    lon: float | None = None
    dist_m: float | None = None
    rms: float | None = None
    std: float | None = None
    max: float | None = None
    p95: float | None = None
    kurtosis: float | None = None
    band_0_5: float | None = None
    band_5_15: float | None = None
    band_15p: float | None = None
    speed_mean: float | None = None
    n: int | None = Field(default=None, ge=0)


class TripIn(BaseModel):
    """Body = the app's upload JSON (plan 4.4). metadata is kept raw."""
    metadata: dict
    events: list[EventIn] = []
    segments: list[SegmentIn] = []

    @field_validator("metadata")
    @classmethod
    def metadata_has_trip_id(cls, v: dict) -> dict:
        if not str(v.get("trip_id") or "").strip():
            raise ValueError("metadata.trip_id is required")
        return v


async def _read_json_gzip(request: Request) -> dict:
    raw = await request.body()
    limit = int(cfg_get("api.request.max_body_mb", 25) * 1024 * 1024)
    if len(raw) > limit:
        raise HTTPException(413, "body too large")
    if request.headers.get("content-encoding", "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            raise HTTPException(400, "invalid gzip body")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "invalid JSON body")


@router.post("/trips", dependencies=[Depends(check_device_key)])
async def post_trip(request: Request):
    """Receives one finished trip. Idempotent on trip_id:
    re-uploads return 200 with duplicate=true and no double inserts."""
    body = await _read_json_gzip(request)
    try:
        trip = TripIn.model_validate_json(json.dumps(body))
    except ValidationError as ve:
        raise HTTPException(422, str(ve))
    events = [e.model_dump() for e in trip.events]
    segments = [s.model_dump() for s in trip.segments]
    result = get_store().insert_trip(trip.metadata, events, segments)
    return result


@router.get("/trips/{trip_id}/status", dependencies=[Depends(check_device_key)])
def trip_status(trip_id: str):
    status = get_store().trip_status(trip_id)
    if status is None:
        raise HTTPException(404, "unknown trip_id")
    return status


@router.get("/trips", dependencies=[Depends(check_device_key)])
def trips_list(limit: int = 50):
    """Ops listing (dashboard / debugging). Newest first."""
    limit = max(1, min(limit, 500))
    return {"trips": get_store().list_trips(limit)}


# ---- Phase C: processing results ------------------------------------------------

@router.get("/analysis/links", dependencies=[Depends(check_device_key)])
def analysis_links(min_status: str = "candidate", limit: int = 1000):
    """Aggregated road links (roughness classes + confidence)."""
    limit = max(1, min(limit, 5000))
    return get_store().links(min_status, limit)


@router.get("/analysis/events", dependencies=[Depends(check_device_key)])
def analysis_events(min_status: str = "candidate", limit: int = 1000):
    """Clustered events (suspected potholes / breakers)."""
    limit = max(1, min(limit, 5000))
    return get_store().event_clusters(min_status, limit)


@router.get("/analysis/priority", dependencies=[Depends(check_device_key)])
def analysis_priority(min_status: str = "candidate", limit: int = 1000):
    """Links ranked by fix-first priority (class x confidence x recency)."""
    limit = max(1, min(limit, 5000))
    from .processing.priority import rank_links
    from .config import CFG
    from .processing.aggregate import now_ms_utc
    return rank_links(get_store().links(min_status, limit), CFG, now_ms_utc())


@router.post("/analysis/recompute", dependencies=[Depends(check_device_key)])
def analysis_recompute():
    """Inline recompute (dev). In the docker deployment Celery runs this."""
    from .process import recompute_all
    return recompute_all()
