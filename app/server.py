"""Ahmed Reaction Studio — FastAPI application factory (P1-22 … P1-27).

Local-first, zero-cloud, zero-npm. The browser does all live media work;
Python orchestrates; FFmpeg renders. Binds 0.0.0.0 so Termux/LAN browsers
can reach it.
"""
from __future__ import annotations

import contextlib
import logging
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import AppConfig, get_config
from app.core.database import reset_db
from app.core.logging import get_logger, setup_logging
from app.core.storage import get_storage
from app.media.ffmpeg import get_ffmpeg
from app.media.ffprobe import get_ffprobe
from app.workers.export_worker import get_export_worker
from app.workers.job_manager import get_job_manager

APP_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = APP_ROOT / "web"


def create_app(cfg: AppConfig | None = None) -> FastAPI:
    cfg = cfg or get_config()
    setup_logging(cfg)
    log = get_logger("server")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ---- startup ----------------------------------------------------
        started = time.time()
        storage = get_storage()
        storage.sweep_temp(24.0)  # P1-18: TTL sweep on boot
        setup_logging(cfg)
        boot_log = get_logger("boot")
        boot_log.info("starting Ahmed Reaction Studio (storage root %s)", storage.roots["projects"],
                      extra={"event": "startup"})
        # FFmpeg / FFprobe detection + logging (P1-34)
        runner = get_ffmpeg()
        caps = await runner.detect()
        if caps.get("available"):
            boot_log.info("ffmpeg %s at %s", caps.get("version"), caps.get("path"),
                          extra={"event": "ffmpeg_detected", "path": caps.get("path")})
        else:
            boot_log.error("ffmpeg NOT found: %s", caps.get("remediation"),
                           extra={"event": "ffmpeg_missing"})
        probe = get_ffprobe()
        pv = await probe.version()
        boot_log.info("ffprobe %s", pv, extra={"event": "ffprobe_detected"})
        # orphaned job sweep from a previous crash (P9-28)
        get_job_manager().sweep_orphans()
        worker = get_export_worker()
        worker.start()
        boot_log.info("boot complete in %.2fs", time.time() - started, extra={"event": "boot_done"})
        yield
        # ---- shutdown ---------------------------------------------------
        worker = get_export_worker()
        worker.stop()
        boot_log.info("shutdown complete", extra={"event": "shutdown"})

    app = FastAPI(
        title="Ahmed Reaction Studio",
        version="1.0.0",
        description="Local-first reaction video studio — 100% local, zero cloud, zero npm.",
        lifespan=lifespan,
    )

    # ---- request id middleware + structured errors (P1-24) ---------------
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Local-Only"] = "1"
        log.info("%s %s -> %s (%.1fms)", request.method, request.url.path,
                 response.status_code, (time.perf_counter() - start) * 1000,
                 extra={"event": "http"})
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path,
                      extra={"event": "unhandled_error"})
        return JSONResponse(
            status_code=500,
            content={"error": "internal error", "detail": str(exc)[:300],
                     "request_id": getattr(request.state, "request_id", "")},
        )

    # ---- routers ----------------------------------------------------------
    from app.api import export, health, media, projects, recording  # noqa: PLC0415

    app.include_router(health.router)
    app.include_router(media.router)
    app.include_router(projects.router)
    app.include_router(recording.router)
    app.include_router(export.router)

    # ---- static web shell (native ES modules, zero build) (P1-23) --------
    app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")

    @app.get("/", include_in_schema=False)
    async def index():
        from fastapi.responses import FileResponse  # noqa: PLC0415
        return FileResponse(WEB_DIR / "index.html")

    # Local-only posture (GR-01): no route in this app performs any outbound
    # network call; every handler touches only local storage and local
    # subprocesses (FFmpeg/FFprobe) with argument arrays.

    return app


app = None


def main() -> None:  # pragma: no cover — manual entry
    import uvicorn

    cfg = get_config()
    uvicorn.run(
        "app.server:build_app",
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=cfg.logging.level.lower(),
    )


def build_app() -> FastAPI:  # uvicorn entrypoint
    global app
    if app is None:
        app = create_app()
    return app


if __name__ == "__main__":  # pragma: no cover
    main()
