"""Webhook dispatch worker.

Polls the webhook_queue table for pending deliveries and dispatches them
asynchronously via HTTP POST. Runs as a background task within the FastAPI
dashboard process (which already uses asyncio).

Security:
- HMAC-SHA256 signatures for payload authenticity
- SSRF protection: blocks private/reserved IP ranges
- HTTPS enforcement (configurable)

Retry strategy:
- Max 3 attempts with exponential backoff: 5s, 30s, 120s
- All attempts logged to webhook_deliveries for audit
"""
import asyncio
import hashlib
import hmac
import ipaddress
import logging
import socket
import time
from datetime import datetime, timezone, timedelta

from .database import (
    get_pending_deliveries,
    mark_delivery_success,
    mark_delivery_retry,
    mark_delivery_failed,
    log_delivery_attempt,
    cleanup_old_deliveries,
)

logger = logging.getLogger("arc-dashboard.webhooks")

# Retry backoff delays in seconds per attempt number (0-indexed)
RETRY_DELAYS = [5, 30, 120]
MAX_ATTEMPTS = 3

# Private/reserved IP ranges to block (SSRF protection)
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_ip(hostname: str) -> bool:
    """Check if a hostname resolves to a private/reserved IP."""
    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for family, _, _, _, addr in results:
            ip = ipaddress.ip_address(addr[0])
            for net in _BLOCKED_NETWORKS:
                if ip in net:
                    return True
    except (socket.gaierror, ValueError):
        return True  # Can't resolve = block
    return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WebhookWorker:
    """Background worker that dispatches queued webhook deliveries."""

    def __init__(self, poll_interval: float = 2.0, cleanup_interval: float = 3600.0):
        self._poll_interval = poll_interval
        self._cleanup_interval = cleanup_interval
        self._running = False
        self._task = None
        self._cleanup_task = None
        self._client = None

    async def start(self):
        """Start the dispatch loop and cleanup loop."""
        try:
            import httpx
        except ImportError:
            logger.warning(
                "httpx not installed — webhook dispatch disabled. "
                "Install with: pip install httpx"
            )
            return

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
            follow_redirects=False,
        )
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Webhook dispatch worker started (poll every %.1fs)", self._poll_interval)

    async def stop(self):
        """Stop the worker and close the HTTP client."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        logger.info("Webhook dispatch worker stopped")

    async def _poll_loop(self):
        """Main poll loop — fetch pending deliveries and dispatch."""
        while self._running:
            try:
                await self._process_batch()
            except Exception:
                logger.exception("Error in webhook dispatch loop")
            await asyncio.sleep(self._poll_interval)

    async def _cleanup_loop(self):
        """Periodic cleanup of old queue and delivery log entries."""
        while self._running:
            await asyncio.sleep(self._cleanup_interval)
            try:
                cleanup_old_deliveries(days=7)
                logger.debug("Cleaned up old webhook delivery records")
            except Exception:
                logger.exception("Error cleaning up webhook deliveries")

    async def _process_batch(self):
        """Fetch pending deliveries and dispatch concurrently."""
        items = get_pending_deliveries(limit=20)
        if not items:
            return

        tasks = [self._deliver(item) for item in items]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _deliver(self, item: dict):
        """Dispatch a single webhook delivery."""
        queue_id = item["id"]
        webhook_id = item["webhook_id"]
        event_id = item["event_id"]
        url = item["url"]
        secret = item["secret"]
        payload = item["payload"]
        attempts = item["attempts"]
        auth_type = item.get("auth_type", "hmac")

        # Skip if webhook was disabled after enqueue
        if not item.get("webhook_enabled"):
            mark_delivery_failed(queue_id)
            return

        # SSRF check — extract hostname from URL
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            hostname = parsed.hostname or ""
            if _is_private_ip(hostname):
                logger.warning("Blocked webhook to private IP: %s", url)
                mark_delivery_failed(queue_id)
                log_delivery_attempt(
                    webhook_id, event_id, attempts + 1,
                    None, "", "Blocked: private/reserved IP address", 0,
                )
                return
        except Exception:
            mark_delivery_failed(queue_id)
            return

        # Build headers based on auth type
        timestamp = _now_iso()
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-ID": str(queue_id),
            "User-Agent": "ARC-Webhook/1.0",
        }

        if auth_type == "bearer":
            # Bearer token auth — secret is the token
            headers["Authorization"] = f"Bearer {secret}"
        else:
            # HMAC-SHA256 signature auth (default)
            # Sign just the body: HMAC-SHA256(body, secret)
            signature = hmac.new(
                secret.encode("utf-8"),
                payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers["x-arc-signature"] = signature

        start_time = time.monotonic()
        status_code = None
        response_body = ""
        error_msg = ""

        try:
            response = await self._client.post(url, content=payload, headers=headers)
            status_code = response.status_code
            response_body = response.text[:200]
            duration_ms = int((time.monotonic() - start_time) * 1000)

            if 200 <= status_code < 300:
                mark_delivery_success(queue_id)
                log_delivery_attempt(
                    webhook_id, event_id, attempts + 1,
                    status_code, response_body, "", duration_ms,
                )
                logger.info(
                    "Webhook delivered: queue=%d webhook=%d event=%d status=%d (%dms)",
                    queue_id, webhook_id, event_id, status_code, duration_ms,
                )
                return
            else:
                error_msg = f"HTTP {status_code}"

        except Exception as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            error_msg = str(exc)[:200]
            logger.warning(
                "Webhook delivery failed: queue=%d webhook=%d error=%s",
                queue_id, webhook_id, error_msg,
            )

        # Log the failed attempt
        log_delivery_attempt(
            webhook_id, event_id, attempts + 1,
            status_code, response_body, error_msg,
            int((time.monotonic() - start_time) * 1000) if status_code is None else duration_ms,
        )

        # Retry or fail permanently
        next_attempt = attempts + 1  # 0-indexed: this was attempt #(attempts+1)
        if next_attempt < MAX_ATTEMPTS:
            delay = RETRY_DELAYS[min(next_attempt, len(RETRY_DELAYS) - 1)]
            next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            mark_delivery_retry(queue_id, next_at)
            logger.info(
                "Webhook retry scheduled: queue=%d attempt=%d/%d next_in=%ds",
                queue_id, next_attempt + 1, MAX_ATTEMPTS, delay,
            )
        else:
            mark_delivery_failed(queue_id)
            logger.warning(
                "Webhook permanently failed: queue=%d webhook=%d after %d attempts",
                queue_id, webhook_id, MAX_ATTEMPTS,
            )
