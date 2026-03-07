from fastapi import APIRouter, HTTPException, Request
import logging

from ..services.stream_manager import stream_manager, CHANNELS
from ..services.camera_registry import camera_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cameras", tags=["cameras"])


@router.get("")
def list_cameras():
    """List all cameras from the cloud config registry."""
    ids = camera_registry.all_ids()
    result = []
    for cid in ids:
        cam = camera_registry.get(cid)
        if cam:
            status = stream_manager.get_camera_status(cid)
            result.append({
                "id": cam.camera_id,
                "name": cam.name,
                "has_high": bool(cam.rtsp_url),
                "has_low": bool(cam.rtsp_url_low),
                "rtsp_high": cam.rtsp_url or None,
                "rtsp_low": cam.rtsp_url_low or None,
                "streams": status,
            })
    return result


@router.post("/{camera_id}/start/{channel}")
async def start_stream(camera_id: str, channel: str, request: Request):
    """Start HLS stream for a camera channel (high or low). Returns HLS URL."""
    if channel not in CHANNELS:
        raise HTTPException(status_code=400, detail=f"Channel must be one of: {', '.join(CHANNELS)}")

    cam = camera_registry.get(camera_id)
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found in config")

    rtsp_url = cam.rtsp_url if channel == "high" else cam.rtsp_url_low
    if not rtsp_url:
        raise HTTPException(status_code=404, detail=f"No {channel} resolution stream configured")

    try:
        hls_path = await stream_manager.start_stream(camera_id, channel, rtsp_url)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    base = str(request.base_url).rstrip("/")
    return {
        "hls_url": f"{base}{hls_path}",
        "status": "streaming",
    }


@router.post("/{camera_id}/stop")
async def stop_stream(camera_id: str):
    """Stop all HLS streams (high + low) for a camera."""
    await stream_manager.stop_camera_streams(camera_id)
    return {"status": "stopped", "camera_id": camera_id}


@router.get("/{camera_id}/status")
def get_status(camera_id: str):
    """Get stream status for both channels of a camera."""
    return stream_manager.get_camera_status(camera_id)
