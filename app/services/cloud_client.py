"""
Cloud Client — manages communication with the UniGuard Pro cloud API.

Handles:
  - One-time registration (POST /register) with retry-until-success
  - Periodic config polling (GET /config) every 60s
  - Periodic heartbeat (POST /heartbeat) every 30s
"""

import asyncio
import logging
import random
import time
from typing import Optional, Callable, Awaitable

import httpx

from ..config import settings
from ..version import get_version
from .stream_manager import stream_manager

logger = logging.getLogger(__name__)


class CloudClient:
    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._config_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._client_id: Optional[str] = None
        self._tunnel_token: Optional[str] = None
        self._on_config_updated: Optional[Callable[..., Awaitable]] = None
        self._start_time = time.time()

    async def register(self, tunnel_token: str) -> str:
        """
        POST /register with exponential backoff retry until success.
        Returns the assigned clientId.
        """
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        ) as client:
            delay = 2.0
            max_delay = 60.0

            while True:
                try:
                    resp = await client.post(
                        f"{settings.cloud_api_url}/register",
                        json={
                            "tunnelToken": tunnel_token,
                            "version": get_version(),
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    client_id = data["clientId"]
                    logger.info("Registered with cloud API, clientId=%s", client_id)
                    return client_id
                except Exception as exc:
                    jitter = random.uniform(0, 2)
                    logger.warning(
                        "Registration failed (%s) — retrying in %.1fs",
                        exc, delay + jitter,
                    )
                    await asyncio.sleep(delay + jitter)
                    delay = min(delay * 2, max_delay)

    async def start(
        self,
        client_id: str,
        tunnel_token: str,
        on_config_updated: Callable[..., Awaitable],
    ) -> None:
        """Start background polling loops after registration."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        )
        self._client_id = client_id
        self._tunnel_token = tunnel_token
        self._on_config_updated = on_config_updated
        self._config_task = asyncio.create_task(self._config_poll_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("Cloud client started (config=%ds, heartbeat=%ds)",
                    settings.config_poll_interval, settings.heartbeat_interval)

    async def stop(self) -> None:
        for task in (self._config_task, self._heartbeat_task):
            if task:
                task.cancel()
        # Await cancellation
        for task in (self._config_task, self._heartbeat_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._client:
            await self._client.aclose()
        logger.info("Cloud client stopped")

    # ── Config polling ─────────────────────────────────────────────────────

    async def _config_poll_loop(self) -> None:
        # Do an immediate first poll, then loop on interval
        await self._poll_config()
        while True:
            try:
                await asyncio.sleep(settings.config_poll_interval)
                await self._poll_config()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Config poll loop error: %s", exc)

    async def _poll_config(self) -> None:
        try:
            resp = await self._client.get(
                f"{settings.cloud_api_url}/config",
                params={"clientId": self._client_id},
            )
            resp.raise_for_status()
            data = resp.json()

            cameras = data.get("cameras", [])
            streaming_cfg = data.get("streaming", {})

            # Pass segment duration from cloud config into each camera dict
            segment_duration = streaming_cfg.get("hls_segment_duration", settings.hls_segment_time)
            for cam in cameras:
                cam.setdefault("segment_duration", segment_duration)

            if self._on_config_updated:
                await self._on_config_updated(cameras)

            logger.debug("Config poll: %d cameras", len(cameras))
        except Exception as exc:
            logger.warning("Config poll failed: %s", exc)

    # ── Heartbeat ──────────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(settings.heartbeat_interval)
                await self._send_heartbeat()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Heartbeat loop error: %s", exc)

    async def _send_heartbeat(self) -> None:
        try:
            resp = await self._client.post(
                f"{settings.cloud_api_url}/heartbeat",
                json={
                    "clientId": self._client_id,
                    "version": get_version(),
                    "activeStreams": stream_manager.active_count(),
                    "uptime": int(time.time() - self._start_time),
                },
            )
            resp.raise_for_status()
            logger.debug("Heartbeat sent OK")
        except Exception as exc:
            logger.warning("Heartbeat failed: %s", exc)


cloud_client = CloudClient()
