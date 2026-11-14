from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from . import bootstrap  # noqa: F401  (first: restores /data from a backup before anything opens it)
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import VERSION
from .db import create_all
from .routers import auth, pages, pulls, repos, setup, telemetry_api, webhooks
from .services import backup, members, telemetry
from .services.settings import issue_setup_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger().addHandler(telemetry.ErrorLogHandler(level=logging.ERROR))
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_all()  # MVP: create tables on boot. Move to Alembic migrations before production.
    issue_setup_token()  # prints a one-time token to the log until the first admin signs in
    telemetry.track("server_started")
    sender = asyncio.create_task(telemetry.run_forever())
    member_sync = asyncio.create_task(members.run_forever())  # keeps inherited GitHub members current
    snapshots = asyncio.create_task(backup.run_forever())      # copies /data to /backup when it changes
    status = backup.storage_status()
    if not status["safe"] and status["external_database"] is False:
        logging.getLogger("docthecode.storage").warning(
            "DocTheCode's data directory %s is not on a persistent volume and no backup folder is mounted: "
            "data will be lost when this container is replaced. Mount a volume at %s, or a bucket at %s.",
            status["data_dir"], status["data_dir"], bootstrap.BACKUP_DIR)
    yield
    snapshots.cancel()
    member_sync.cancel()
    sender.cancel()
    # Hosts give containers a short grace period on shutdown (often ~10 s): save data first.
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.to_thread(backup.backup_now, True), timeout=8)
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await asyncio.wait_for(telemetry.client().flush(), timeout=1.5)


app = FastAPI(title="DocTheCode", version=VERSION, lifespan=lifespan)
for r in (setup.router, auth.router, repos.router, pages.router, pulls.router, webhooks.router, telemetry_api.router):
    app.include_router(r)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or "<unmatched>"


@app.middleware("http")
async def usage_stats(request: Request, call_next):
    """Times every API call by route template (never the real path) and reports crashes."""
    if not request.url.path.startswith("/api/") or request.url.path.startswith("/api/telemetry"):
        return await call_next(request)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as e:
        route = _route_template(request)
        telemetry.record_exception(e, "request", route=route, method=request.method)
        telemetry.client().record_request(route, request.method, 500, (time.perf_counter() - start) * 1000)
        logging.getLogger("docthecode").error("Unhandled error on %s %s", request.method, route, exc_info=e)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)
    telemetry.client().record_request(_route_template(request), request.method, response.status_code,
                                      (time.perf_counter() - start) * 1000)
    return response


@app.get("/api/health")
def health():
    return {"ok": True, "version": VERSION}


# Serve the built React app (production). In development Vite serves it and proxies /api here.
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        return FileResponse(FRONTEND_DIST / "index.html")
