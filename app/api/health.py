import shutil
import time
from fastapi import APIRouter
from ..schemas import HealthResponse
from ..services.stream_manager import stream_manager
from ..config import settings

router = APIRouter(tags=["health"])

_START_TIME = time.time()
VERSION = "1.0.0"


@router.get("/health", response_model=HealthResponse)
def health_check():
    return HealthResponse(
        status="ok",
        version=VERSION,
        active_streams=stream_manager.active_count(),
        ffmpeg_available=shutil.which(settings.ffmpeg_path) is not None,
        uptime_seconds=round(time.time() - _START_TIME, 1),
    )
