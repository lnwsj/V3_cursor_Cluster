"""
V3_cursor_Cluster gateway — FastAPI app entry point.
"""
from __future__ import annotations
import logging
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path

from .config import get_settings
from .db import init_pool, close_pool
from . import storage
from .routes import health, files, jobs, workers, admin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("v3cluster.gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    log.info("Starting V3_cursor_Cluster gateway on %s:%d", s.gateway_host, s.gateway_port)
    log.info("Public URL: %s", s.gateway_public_url)
    log.info("Storage root: %s", s.storage_root)
    storage.init_root()
    await init_pool()
    log.info("DB pool ready")
    try:
        yield
    finally:
        await close_pool()
        log.info("Gateway shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title="V3_cursor_Cluster Gateway",
        version="1.0.0",
        description="Cluster green-screen (chroma key) rendering — 1 gateway + N workers",
        lifespan=lifespan,
    )

    # CORS (open in v1; tighten via gateway_public_url allowlist in prod)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-RateLimit-Limit", "X-RateLimit-Remaining", "Retry-After"],
    )

    # Routers
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(files.router,  prefix="/api/v1")
    app.include_router(jobs.router,   prefix="/api/v1")
    app.include_router(workers.router,prefix="/api/v1")
    app.include_router(admin.router,  prefix="/api/v1")

    # Static UI
    static_dir = Path(__file__).parent.parent / "ui"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="ui-static")

        @app.get("/")
        async def root():
            return FileResponse(static_dir / "index.html")

        @app.get("/favicon.ico")
        async def favicon():
            f = static_dir / "favicon.ico"
            if f.exists():
                return FileResponse(f)
            return JSONResponse({}, status_code=204)

    return app


app = create_app()
