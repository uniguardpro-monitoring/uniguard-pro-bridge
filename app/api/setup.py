from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import logging

from ..services.cloud_client import cloud_client
from ..services.state_store import state_store, BridgeState
from ..services.camera_registry import camera_registry
from ..services.stream_manager import stream_manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["setup"])


class SetupRequest(BaseModel):
    tunnelToken: str


class SetupResponse(BaseModel):
    status: str


async def _on_config_updated(cameras: list) -> None:
    """Config update callback for cloud client started via setup endpoint."""
    added, removed, updated = camera_registry.update_from_config(cameras)

    for cam_id in removed:
        await stream_manager.stop_camera_streams(cam_id)
    for cam_id in updated:
        await stream_manager.stop_camera_streams(cam_id)

    if added or removed or updated:
        logger.info(
            "Config updated: +%d -%d ~%d cameras",
            len(added), len(removed), len(updated),
        )


@router.post("/setup", response_model=SetupResponse)
async def setup_bridge(body: SetupRequest):
    """
    Accept a tunnel token, register with the cloud API, and start polling.
    Called by the web app via bridge-proxy after cloudflared is running.
    """
    state = state_store.load()

    # If already registered with this token, no-op success
    if state.client_id and state.tunnel_token == body.tunnelToken:
        return SetupResponse(status="ok")

    # Stop existing cloud client if running (re-registration)
    await cloud_client.stop()

    # Register with cloud API
    try:
        client_id = await cloud_client.register(body.tunnelToken, max_attempts=3)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Registration failed: {exc}")

    # Save state
    state = BridgeState(client_id=client_id, tunnel_token=body.tunnelToken)
    state_store.save(state)

    # Start cloud polling
    await cloud_client.start(
        client_id=client_id,
        tunnel_token=body.tunnelToken,
        on_config_updated=_on_config_updated,
    )

    logger.info("Bridge setup complete via API, clientId=%s", client_id)
    return SetupResponse(status="ok")
