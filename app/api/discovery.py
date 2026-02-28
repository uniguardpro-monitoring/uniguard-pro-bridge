from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime
import logging

from ..database import get_db
from ..models import NVR, Camera
from ..schemas import (
    NVRCreate, NVRResponse, NVRImportRequest,
    ScanRequest, DiscoveredDevice, CameraResponse,
)
from ..services.discovery import scan_subnet, query_unifi_cameras, get_local_subnet

logger = logging.getLogger(__name__)
router = APIRouter(tags=["discovery"])


# ── NVR management ─────────────────────────────────────────────────────────────

@router.get("/nvrs", response_model=List[NVRResponse])
def list_nvrs(db: Session = Depends(get_db)):
    return db.query(NVR).all()


@router.post("/nvrs", response_model=NVRResponse, status_code=201)
def add_nvr(nvr: NVRCreate, db: Session = Depends(get_db)):
    existing = db.query(NVR).filter(NVR.ip_address == nvr.ip_address).first()
    if existing:
        raise HTTPException(status_code=409, detail="NVR with this IP already exists")
    db_nvr = NVR(**nvr.model_dump())
    db.add(db_nvr)
    db.commit()
    db.refresh(db_nvr)
    return db_nvr


@router.delete("/nvrs/{nvr_id}")
def delete_nvr(nvr_id: int, db: Session = Depends(get_db)):
    nvr = db.query(NVR).filter(NVR.id == nvr_id).first()
    if not nvr:
        raise HTTPException(status_code=404, detail="NVR not found")
    db.delete(nvr)
    db.commit()
    return {"status": "deleted", "nvr_id": nvr_id}


@router.post("/nvrs/{nvr_id}/import", response_model=List[CameraResponse])
async def import_cameras_from_nvr(
    nvr_id: int,
    body: NVRImportRequest,
    db: Session = Depends(get_db),
):
    """
    Authenticate to the UniFi Protect API on this NVR and import all
    RTSP-enabled cameras into the database.
    """
    nvr = db.query(NVR).filter(NVR.id == nvr_id).first()
    if not nvr:
        raise HTTPException(status_code=404, detail="NVR not found")

    try:
        camera_data = await query_unifi_cameras(nvr.ip_address, body.username, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    added: List[Camera] = []
    for cam in camera_data:
        # Skip if already in DB (match by camera_uid + nvr_id)
        existing = (
            db.query(Camera)
            .filter(Camera.camera_uid == cam["camera_uid"], Camera.nvr_id == nvr_id)
            .first()
        )
        if existing:
            # Update the RTSP URL in case the alias changed
            existing.rtsp_url = cam["rtsp_url"]
            existing.last_seen = datetime.utcnow()
            added.append(existing)
        else:
            db_cam = Camera(
                nvr_id=nvr_id,
                name=cam["name"],
                rtsp_url=cam["rtsp_url"],
                camera_uid=cam["camera_uid"],
                channel_name=cam.get("channel_name"),
                last_seen=datetime.utcnow(),
            )
            db.add(db_cam)
            added.append(db_cam)

    # Mark the NVR as verified
    nvr.api_verified = True
    nvr.username = body.username
    nvr.last_seen = datetime.utcnow()
    db.commit()
    for cam in added:
        db.refresh(cam)

    return added


# ── LAN scan ──────────────────────────────────────────────────────────────────

@router.post("/discovery/scan", response_model=List[DiscoveredDevice])
async def scan_lan(body: ScanRequest, db: Session = Depends(get_db)):
    """
    Scan the /24 LAN subnet for devices with port 7441 open (UniFi RTSPS).
    Auto-creates NVR records for newly discovered hosts.
    """
    subnet = body.subnet or get_local_subnet()
    devices = await scan_subnet(subnet=subnet, port=7441)

    # Auto-register newly discovered NVRs
    for device in devices:
        ip = device["ip"]
        if not db.query(NVR).filter(NVR.ip_address == ip).first():
            db.add(NVR(name=f"UniFi NVR @ {ip}", ip_address=ip))

    db.commit()
    return devices
