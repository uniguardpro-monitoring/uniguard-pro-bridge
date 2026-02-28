"""
Uniguard Pro Bridge — RTSP → HLS Streaming Gateway
FastAPI entry point
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import init_db
from .services.stream_manager import stream_manager
from .api import streams, discovery, health

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
HLS_DIR = Path(settings.hls_dir)

# Must exist before StaticFiles mounts it (lifespan runs too late)
HLS_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ─────────────────────────────────────────────────────────────
    init_db()
    await stream_manager.start_cleanup_task()
    logger.info("Uniguard Pro Bridge started on %s:%d", settings.host, settings.port)
    yield
    # ── Shutdown ────────────────────────────────────────────────────────────
    logger.info("Shutting down — stopping all active streams …")
    await stream_manager.stop_all()


app = FastAPI(
    title="Uniguard Pro Bridge",
    description="Lightweight RTSP→HLS streaming gateway for Raspberry Pi",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── HLS activity tracking middleware ─────────────────────────────────────────

@app.middleware("http")
async def track_hls_activity(request: Request, call_next):
    """
    Any request for an HLS file (playlist or segment) resets that stream's
    idle timer, preventing premature shutdown while a client is watching.
    """
    path = request.url.path
    if path.startswith("/hls/"):
        parts = path.split("/")
        if len(parts) >= 3:
            try:
                camera_id = int(parts[2])
                stream_manager.touch_activity(camera_id)
            except (ValueError, IndexError):
                pass
    return await call_next(request)


# ── Static mounts ─────────────────────────────────────────────────────────────

# HLS output files — served by FFmpeg, consumed by HLS.js
app.mount("/hls", StaticFiles(directory=str(HLS_DIR)), name="hls")

# Frontend SPA assets
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── API routers ───────────────────────────────────────────────────────────────

app.include_router(streams.router, prefix="/api")
app.include_router(discovery.router, prefix="/api")
app.include_router(health.router, prefix="/api")


# ── SPA catch-all ─────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    # Let /api and /hls requests 404 naturally; send everything else to the SPA
    if full_path.startswith("api/") or full_path.startswith("hls/"):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(str(STATIC_DIR / "index.html"))
