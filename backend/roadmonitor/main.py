"""Road Monitor server - Phase B entry point.

Runs the untouched legacy app (dashboard, /upload, /api/live, /ping, the phone
serving routes) plus the API v1 router on one port. Equivalent to running
`python app.py`, but with /api/v1 available.

Run:  cd backend && python main.py   (or: uvicorn main:app from backend/)
"""
import uvicorn

from .config import cfg_get
from .store import get_store
from .api_v1 import router
from fastapi.responses import FileResponse  # noqa: E402
from pathlib import Path  # noqa: E402

_DASHBOARD_HTML = Path(__file__).resolve().parent / "static" / "dashboard.html"

# legacy FastAPI app stays exactly as it is (dashboard, live stream, /upload)
from app import app as application  # noqa: E402  (cwd = backend/, like app.py)

_application_ready = False


def _dashboard() -> FileResponse:
    return FileResponse(_DASHBOARD_HTML, media_type="text/html")


def prepare() -> None:
    """Idempotent startup: init DB schema + mount /api/v1 + /dashboard. Runs at
    import so every entry point (uvicorn, python main.py, TestClient) gets the
    same app."""
    global _application_ready
    if _application_ready:
        return
    get_store().init_schema()
    if not any(getattr(r, "path", "").startswith("/api/v1") for r in application.routes):
        application.include_router(router)
    if not any(getattr(r, "path", "") == "/dashboard" for r in application.routes):
        application.get("/dashboard")(_dashboard)
    _application_ready = True


prepare()  # runs at import: every entry point (uvicorn, python -m, TestClient)
          # gets schema + routes; legacy app.py keeps running fine standalone


if __name__ == "__main__":
    prepare()
    host = cfg_get("server.host", "0.0.0.0")
    port = int(cfg_get("server.port", 8000))
    print("  Dashboard:            http://localhost:%d" % port)
    print("  Road map (Phase D):   http://localhost:%d/dashboard" % port)
    print("  API v1:               /api/v1/{devices/register,trips,trips/{id}/status,analysis/*}")
    uvicorn.run(application, host=host, port=port)
