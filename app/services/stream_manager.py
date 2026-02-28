"""
Stream Manager — on-demand FFmpeg RTSP→HLS lifecycle manager.

Design:
  • One FFmpeg subprocess per camera, launched on request.
  • FFmpeg remuxes RTSP to HLS using -c:v copy (no transcoding) — very lightweight on Pi.
  • Each stream has a 5-minute idle timeout; a background asyncio task polices it.
  • HLS segments are written to  hls/<camera_id>/  and served as static files.
  • Requesting /hls/<id>/... automatically touches the stream's last_activity.
"""

import asyncio
import subprocess
import shutil
import logging
from datetime import datetime
from typing import Dict, Optional
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)


@dataclass
class _StreamProcess:
    camera_id: int
    rtsp_url: str
    process: subprocess.Popen
    hls_dir: Path
    started_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)
    status: str = "starting"   # starting | streaming | error


class StreamManager:

    def __init__(self) -> None:
        self._streams: Dict[int, _StreamProcess] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    # ── Public lifecycle ───────────────────────────────────────────────────

    async def start_cleanup_task(self) -> None:
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Stream cleanup task started (interval=30 s, timeout=%d s)",
                    settings.stream_timeout_seconds)

    async def stop_all(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
        async with self._lock:
            for cam_id in list(self._streams):
                await self._terminate(cam_id)
        logger.info("All streams stopped")

    async def start_stream(self, camera_id: int, rtsp_url: str) -> str:
        """
        Start (or refresh) a stream for *camera_id*.
        Returns the HLS playlist path, e.g. /hls/3/index.m3u8
        """
        async with self._lock:
            existing = self._streams.get(camera_id)
            if existing:
                if existing.process.poll() is None:
                    # Still running — just refresh the timeout
                    existing.last_activity = datetime.utcnow()
                    return self._hls_url(camera_id)
                else:
                    # Dead process — clean up before restarting
                    await self._terminate(camera_id)

            hls_dir = Path(settings.hls_dir) / str(camera_id)
            hls_dir.mkdir(parents=True, exist_ok=True)

            m3u8 = str(hls_dir / "index.m3u8")
            seg_pattern = str(hls_dir / "seg_%05d.ts")

            cmd = [
                settings.ffmpeg_path,
                "-loglevel", "error",
                "-rtsp_transport", "tcp",
                "-i", rtsp_url,
                # Copy the H.264 stream as-is — no CPU-intensive transcoding
                "-c:v", "copy",
                "-an",              # Drop audio (saves bandwidth; add -c:a aac if needed)
                "-f", "hls",
                "-hls_time", str(settings.hls_segment_time),
                "-hls_list_size", str(settings.hls_list_size),
                "-hls_flags", "delete_segments+append_list",
                "-hls_segment_type", "mpegts",
                "-hls_segment_filename", seg_pattern,
                m3u8,
            ]

            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                raise RuntimeError(
                    "ffmpeg not found. Install it with: sudo apt-get install ffmpeg"
                )

            stream = _StreamProcess(
                camera_id=camera_id,
                rtsp_url=rtsp_url,
                process=process,
                hls_dir=hls_dir,
            )
            self._streams[camera_id] = stream

        # Wait for the playlist to appear (outside the lock so other calls aren't blocked)
        await self._wait_ready(camera_id)
        return self._hls_url(camera_id)

    async def stop_stream(self, camera_id: int) -> None:
        async with self._lock:
            await self._terminate(camera_id)

    def touch_activity(self, camera_id: int) -> None:
        """Called by the HLS static-file middleware to extend the stream timeout."""
        stream = self._streams.get(camera_id)
        if stream:
            stream.last_activity = datetime.utcnow()

    def get_status(self, camera_id: int) -> dict:
        stream = self._streams.get(camera_id)
        if not stream:
            return {"status": "idle"}
        if stream.process.poll() is not None:
            return {"status": "error"}
        return {
            "status": stream.status,
            "hls_url": self._hls_url(camera_id),
            "started_at": stream.started_at,
            "last_activity": stream.last_activity,
            "pid": stream.process.pid,
        }

    def active_count(self) -> int:
        return len(self._streams)

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _wait_ready(
        self, camera_id: int, timeout: float = None
    ) -> None:
        timeout = timeout or settings.stream_start_wait_seconds
        m3u8 = Path(settings.hls_dir) / str(camera_id) / "index.m3u8"
        deadline = asyncio.get_event_loop().time() + timeout
        interval = 0.4

        while asyncio.get_event_loop().time() < deadline:
            stream = self._streams.get(camera_id)
            if not stream:
                return  # Stopped externally

            if stream.process.poll() is not None:
                stream.status = "error"
                stderr_bytes = stream.process.stderr.read(512) if stream.process.stderr else b""
                logger.error(
                    "FFmpeg for camera %d exited early. stderr: %s",
                    camera_id,
                    stderr_bytes.decode(errors="replace").strip(),
                )
                return

            if m3u8.exists() and m3u8.stat().st_size > 0:
                stream.status = "streaming"
                logger.info("Stream %d ready", camera_id)
                return

            await asyncio.sleep(interval)

        # Timed out — process might still connect; keep it alive but mark streaming
        stream = self._streams.get(camera_id)
        if stream and stream.process.poll() is None:
            stream.status = "streaming"
            logger.warning(
                "Stream %d did not produce a playlist within %.1f s — "
                "check the RTSP URL and network connectivity.",
                camera_id,
                timeout,
            )

    async def _terminate(self, camera_id: int) -> None:
        """Kill FFmpeg and remove HLS files. Must be called while holding self._lock."""
        stream = self._streams.pop(camera_id, None)
        if not stream:
            return

        if stream.process.poll() is None:
            stream.process.terminate()
            try:
                await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, stream.process.wait),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                stream.process.kill()

        if stream.hls_dir.exists():
            shutil.rmtree(str(stream.hls_dir), ignore_errors=True)

        logger.info("Stream %d stopped and HLS files cleaned up", camera_id)

    def _hls_url(self, camera_id: int) -> str:
        return f"/hls/{camera_id}/index.m3u8"

    # ── Cleanup loop ──────────────────────────────────────────────────────

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                await self._check_timeouts()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Cleanup loop error: %s", exc)

    async def _check_timeouts(self) -> None:
        now = datetime.utcnow()
        to_stop = []

        for cam_id, stream in self._streams.items():
            idle_seconds = (now - stream.last_activity).total_seconds()
            if idle_seconds > settings.stream_timeout_seconds:
                logger.info(
                    "Stream %d idle for %.0f s — stopping", cam_id, idle_seconds
                )
                to_stop.append(cam_id)
            elif stream.process.poll() is not None:
                logger.warning("Stream %d process died unexpectedly", cam_id)
                to_stop.append(cam_id)

        for cam_id in to_stop:
            await self.stop_stream(cam_id)


# Singleton used by all API routes
stream_manager = StreamManager()
