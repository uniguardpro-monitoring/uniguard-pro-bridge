from fastapi import APIRouter
from ..services.stream_manager import stream_manager
from ..version import get_version

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check():
    return {
        "version": get_version(),
        "active_streams": stream_manager.active_count(),
    }
