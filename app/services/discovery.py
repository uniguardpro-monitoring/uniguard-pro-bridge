"""
LAN Discovery Service

Strategy:
  1. Detect the Pi's own /24 subnet via a UDP socket trick (no root needed).
  2. Concurrently probe every host in the subnet for TCP port 7441 (UniFi RTSPS).
  3. Optionally, once an NVR IP + credentials are known, hit the UniFi Protect
     REST API to enumerate cameras and their RTSP stream aliases.

All network I/O is async-friendly and designed to be fast on a Pi.
"""

import asyncio
import socket
import logging
from typing import List, Optional

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


# ── Subnet detection ──────────────────────────────────────────────────────────

def get_local_ip() -> str:
    """Return the Pi's primary LAN IP without sending any packets."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "192.168.1.1"
    finally:
        s.close()


def get_local_subnet() -> str:
    """Return the /24 prefix of the Pi's LAN, e.g. '192.168.1'."""
    ip = get_local_ip()
    parts = ip.split(".")
    return f"{parts[0]}.{parts[1]}.{parts[2]}"


# ── Port scanner ──────────────────────────────────────────────────────────────

async def _probe_host(ip: str, port: int, timeout: float) -> Optional[str]:
    """Return the IP if *port* is open, else None."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return ip
    except Exception:
        return None


async def scan_subnet(
    subnet: Optional[str] = None,
    port: int = 7441,
) -> List[dict]:
    """
    Scan <subnet>.1–254 for open *port*.
    Returns list of {"ip": str, "port": int}.
    """
    subnet = subnet or get_local_subnet()
    logger.info("Scanning %s.0/24 port %d …", subnet, port)

    tasks = [
        _probe_host(f"{subnet}.{i}", port, settings.scan_timeout_seconds)
        for i in range(1, 255)
    ]
    results = await asyncio.gather(*tasks)
    found = [{"ip": ip, "port": port} for ip in results if ip]
    logger.info("Found %d device(s) on port %d", len(found), port)
    return found


# ── UniFi Protect API ─────────────────────────────────────────────────────────

async def query_unifi_cameras(
    nvr_ip: str, username: str, password: str
) -> List[dict]:
    """
    Authenticate to the UniFi Protect API and enumerate RTSP-enabled camera
    channels.  Returns a list of camera dicts ready to be persisted.

    Raises ValueError with a human-readable reason on failure.
    """
    base = f"https://{nvr_ip}"
    # UniFi Protect uses self-signed TLS — disable cert verification on LAN
    async with httpx.AsyncClient(verify=False, timeout=15.0, follow_redirects=True) as client:
        # Step 1 — authenticate
        try:
            login = await client.post(
                f"{base}/api/auth/login",
                json={"username": username, "password": password},
            )
        except httpx.RequestError as exc:
            raise ValueError(f"Cannot reach NVR at {nvr_ip}: {exc}") from exc

        if login.status_code not in (200, 201):
            raise ValueError(
                f"Login failed (HTTP {login.status_code}). "
                "Check your username and password."
            )

        # Step 2 — list cameras (cookies set automatically by httpx)
        cam_resp = await client.get(f"{base}/proxy/protect/api/cameras")
        if cam_resp.status_code != 200:
            raise ValueError(
                f"Camera list failed (HTTP {cam_resp.status_code}). "
                "Ensure this account has Protect access."
            )

        cameras: List[dict] = []
        for cam in cam_resp.json():
            cam_id = cam.get("id", "")
            cam_name = cam.get("name", "Unknown Camera")

            for ch in cam.get("channels", []):
                if not ch.get("isRtspEnabled", False):
                    continue
                alias = ch.get("rtspAlias", "")
                if not alias:
                    continue
                cameras.append({
                    "camera_uid": cam_id,
                    "name": f"{cam_name} — {ch.get('name', 'Main')}",
                    "channel_name": ch.get("name", "Main"),
                    "rtsp_url": f"rtsps://{nvr_ip}:7441/{alias}",
                })
                # Only take the first enabled channel per camera by default.
                # Remove this break to import all quality tiers.
                break

        logger.info("Imported %d camera(s) from %s", len(cameras), nvr_ip)
        return cameras
