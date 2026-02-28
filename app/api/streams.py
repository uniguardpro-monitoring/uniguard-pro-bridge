from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime
import logging

from ..database import get_db
from ..models import Camera
from ..schemas import CameraCreate, CameraResponse, StreamStatus
from ..services.stream_manager import stream_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cameras", tags=["cameras"])


def _resolve_camera(camera_id: str, db: Session) -> Camera:
    """
    Resolve a camera by integer PK *or* by UniFi camera_uid string.
    This lets the Lovable frontend pass UniFi IDs directly without
    needing to know the bridge's internal SQLite IDs.
    """
    # Try integer PK first
    try:
        pk = int(camera_id)
        cam = db.query(Camera).filter(Camera.id == pk).first()
        if cam:
            return cam
    except (ValueError, TypeError):
        pass

    # Fall back to camera_uid (UniFi string ID)
    cam = db.query(Camera).filter(Camera.camera_uid == camera_id).first()
    if cam:
        return cam

    raise HTTPException(status_code=404, detail="Camera not found")


@router.get("", response_model=List[CameraResponse])
def list_cameras(db: Session = Depends(get_db)):
    return db.query(Camera).filter(Camera.is_active == True).all()


@router.post("", response_model=CameraResponse, status_code=201)
def add_camera(camera: CameraCreate, db: Session = Depends(get_db)):
    db_cam = Camera(**camera.model_dump())
    db.add(db_cam)
    db.commit()
    db.refresh(db_cam)
    return db_cam


@router.get("/by-uid/{camera_uid}", response_model=CameraResponse)
def get_camera_by_uid(camera_uid: str, db: Session = Depends(get_db)):
    """Lookup a bridge camera by its UniFi camera_uid."""
    cam = db.query(Camera).filter(Camera.camera_uid == camera_uid).first()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found for this UID")
    return cam


@router.delete("/{camera_id}")
async def delete_camera(camera_id: str, db: Session = Depends(get_db)):
    cam = _resolve_camera(camera_id, db)
    await stream_manager.stop_stream(cam.id)
    db.delete(cam)
    db.commit()
    return {"status": "deleted", "camera_id": cam.id}


@router.get("/{camera_id}/status", response_model=StreamStatus)
def get_status(camera_id: str, db: Session = Depends(get_db)):
    cam = _resolve_camera(camera_id, db)
    data = stream_manager.get_status(cam.id)
    return StreamStatus(camera_id=cam.id, **data)


@router.post("/{camera_id}/start", response_model=StreamStatus)
async def start_stream(camera_id: str, request: Request, db: Session = Depends(get_db)):
    cam = _resolve_camera(camera_id, db)

    try:
        hls_path = await stream_manager.start_stream(cam.id, cam.rtsp_url)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Update last_seen on the camera record
    cam.last_seen = datetime.utcnow()
    db.commit()

    base = str(request.base_url).rstrip("/")
    status = stream_manager.get_status(cam.id)
    return StreamStatus(
        camera_id=cam.id,
        status=status.get("status", "streaming"),
        hls_url=f"{base}{hls_path}",
        started_at=status.get("started_at"),
        last_activity=status.get("last_activity"),
        pid=status.get("pid"),
    )


@router.post("/{camera_id}/stop")
async def stop_stream(camera_id: str, db: Session = Depends(get_db)):
    cam = _resolve_camera(camera_id, db)
    await stream_manager.stop_stream(cam.id)
    return {"status": "stopped", "camera_id": cam.id}
