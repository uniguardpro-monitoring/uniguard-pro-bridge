from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List


# ── NVR ──────────────────────────────────────────────────────────────────────

class NVRCreate(BaseModel):
    name: str
    ip_address: str
    username: Optional[str] = None
    password: Optional[str] = None
    rtsp_port: int = 7441


class NVRResponse(BaseModel):
    id: int
    name: str
    ip_address: str
    username: Optional[str]
    rtsp_port: int
    api_verified: bool
    created_at: datetime
    last_seen: Optional[datetime]

    class Config:
        from_attributes = True


# ── Camera ────────────────────────────────────────────────────────────────────

class CameraCreate(BaseModel):
    name: str
    rtsp_url: str
    nvr_id: Optional[int] = None
    camera_uid: Optional[str] = None
    channel_name: Optional[str] = None


class CameraResponse(BaseModel):
    id: int
    nvr_id: Optional[int]
    name: str
    rtsp_url: str
    camera_uid: Optional[str]
    channel_name: Optional[str]
    is_active: bool
    created_at: datetime
    last_seen: Optional[datetime]

    class Config:
        from_attributes = True


# ── Streams ───────────────────────────────────────────────────────────────────

class StreamStatus(BaseModel):
    camera_id: int
    status: str                         # idle | starting | streaming | error
    hls_url: Optional[str] = None
    started_at: Optional[datetime] = None
    last_activity: Optional[datetime] = None
    pid: Optional[int] = None


# ── Discovery ─────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    subnet: Optional[str] = None        # e.g. "192.168.1" — auto-detected if None


class DiscoveredDevice(BaseModel):
    ip: str
    port: int


class NVRImportRequest(BaseModel):
    username: str
    password: str


# ── Health ────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    version: str
    git_commit: Optional[str] = None
    active_streams: int
    ffmpeg_available: bool
    uptime_seconds: float
