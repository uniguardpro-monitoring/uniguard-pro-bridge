import shutil
import subprocess
import time
from pathlib import Path
from fastapi import APIRouter
from ..schemas import HealthResponse
from ..services.stream_manager import stream_manager
from ..config import settings
from ..version import get_version

router = APIRouter(tags=["health"])

_START_TIME = time.time()
_REPO_DIR = Path(__file__).parent.parent.parent


def _read_git_commit() -> str | None:
    """Read the current short git commit hash, if available."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(_REPO_DIR),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


@router.get("/health", response_model=HealthResponse)
def health_check():
    return HealthResponse(
        status="ok",
        version=get_version(),
        git_commit=_read_git_commit(),
        active_streams=stream_manager.active_count(),
        ffmpeg_available=shutil.which(settings.ffmpeg_path) is not None,
        uptime_seconds=round(time.time() - _START_TIME, 1),
    )
