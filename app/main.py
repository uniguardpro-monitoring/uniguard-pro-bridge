"""
Uniguard Pro Bridge — RTSP -> HLS Streaming Gateway
FastAPI entry point — managed edge agent mode
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .services.stream_manager import stream_manager
from .services.cloud_client import cloud_client
from .services.camera_registry import camera_registry
from .services.state_store import state_store
from .api import streams, health, setup
from .version import get_version

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


async def _on_config_updated(cameras: list) -> None:
    """Called by cloud_client when config poll receives a new camera list."""
    added, removed, updated = camera_registry.update_from_config(cameras)

    # Stop streams for removed cameras
    for cam_id in removed:
        await stream_manager.stop_stream(cam_id)

    # Stop streams for cameras whose RTSP URL changed (will restart on next request)
    for cam_id in updated:
        await stream_manager.stop_stream(cam_id)

    if added or removed or updated:
        logger.info(
            "Config updated: +%d -%d ~%d cameras",
            len(added), len(removed), len(updated),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # -- Startup ---------------------------------------------------------------
    state = state_store.load()

    # Register if no clientId yet
    cloud_ready = False
    if not state.client_id:
        tunnel_token = settings.tunnel_token
        if not tunnel_token:
            logger.warning(
                "UGBRIDGE_TUNNEL_TOKEN not set — running without cloud registration. "
                "Set the env var and restart to connect to the cloud API."
            )
        else:
            state.tunnel_token = tunnel_token
            state.client_id = await cloud_client.register(tunnel_token)
            state_store.save(state)
            logger.info("Registered with cloud, clientId=%s", state.client_id)
            cloud_ready = True
    else:
        # Check if tunnel token changed (force re-registration)
        if settings.tunnel_token and settings.tunnel_token != state.tunnel_token:
            state.tunnel_token = settings.tunnel_token
            state.client_id = await cloud_client.register(state.tunnel_token)
            state_store.save(state)
            logger.info("Re-registered with new tunnel token, clientId=%s", state.client_id)
        else:
            logger.info("Loaded existing clientId=%s", state.client_id)
        cloud_ready = True

    # Start background tasks
    if cloud_ready:
        await cloud_client.start(
            client_id=state.client_id,
            tunnel_token=state.tunnel_token,
            on_config_updated=_on_config_updated,
        )
    await stream_manager.start_cleanup_task()
    logger.info("Uniguard Pro Bridge started on %s:%d", settings.host, settings.port)

    yield

    # -- Shutdown --------------------------------------------------------------
    logger.info("Shutting down — stopping cloud client and all streams...")
    await cloud_client.stop()
    await stream_manager.stop_all()


app = FastAPI(
    title="Uniguard Pro Bridge",
    description="Managed RTSP->HLS streaming gateway for edge deployment",
    version=get_version(),
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


# -- HLS file serving ----------------------------------------------------------
#
# Served as a regular route (not StaticFiles) so that:
#   - Content-Type is explicitly correct for .m3u8 and .ts
#   - CORSMiddleware automatically applies CORS headers
#   - Activity tracking resets the idle timer per camera

_HLS_MIME = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts":   "video/mp2t",
}


@app.get("/hls/{camera_id}/{filename}")
async def serve_hls_file(camera_id: str, filename: str):
    """Serve HLS playlist and segment files with correct Content-Type."""
    stream_manager.touch_activity(camera_id)

    file_path = (HLS_DIR / camera_id / filename).resolve()

    # Path-traversal guard
    if not str(file_path).startswith(str(HLS_DIR.resolve())):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    ext = file_path.suffix.lower()
    media_type = _HLS_MIME.get(ext, "application/octet-stream")
    return FileResponse(str(file_path), media_type=media_type)


# -- Static mounts -------------------------------------------------------------

# Debug SPA (optional — kept for local troubleshooting)
if STATIC_DIR.exists():
    app.mount("/debug", StaticFiles(directory=str(STATIC_DIR), html=True), name="debug")


# -- API routers ---------------------------------------------------------------

app.include_router(streams.router, prefix="/api")
app.include_router(health.router, prefix="/api")
app.include_router(setup.router, prefix="/api")
