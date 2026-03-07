from fastapi import APIRouter, HTTPException, Request
import logging

from ..services.stream_manager import stream_manager
from ..services.camera_registry import camera_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cameras", tags=["cameras"])


@router.post("/{camera_id}/start")
async def start_stream(camera_id: str, request: Request):
    """Start HLS stream for a camera. Returns HLS URL."""
    cam = camera_registry.get(camera_id)
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found in config")

    try:
        hls_path = await stream_manager.start_stream(camera_id, cam.rtsp_url)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    base = str(request.base_url).rstrip("/")
    return {
        "hls_url": f"{base}{hls_path}",
        "status": "streaming",
    }


@router.post("/{camera_id}/stop")
async def stop_stream(camera_id: str):
    """Stop HLS stream for a camera."""
    await stream_manager.stop_stream(camera_id)
    return {"status": "stopped", "camera_id": camera_id}


@router.get("/{camera_id}/status")
def get_status(camera_id: str):
    """Get stream status for a camera."""
    return stream_manager.get_status(camera_id)
