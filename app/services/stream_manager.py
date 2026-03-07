"""
Stream Manager — on-demand FFmpeg RTSP->HLS lifecycle manager.

Design:
  - One FFmpeg subprocess per camera *channel* (high / low), launched on request.
  - FFmpeg remuxes RTSP to HLS using -c:v copy (no transcoding) -- very lightweight on Pi.
  - Each stream has a 5-minute idle timeout; a background asyncio task polices it.
  - HLS segments are written to  hls/<camera_id>/<channel>/  and served as static files.
  - Requesting /hls/<id>/<channel>/... automatically touches the stream's last_activity.
"""

import asyncio
import subprocess
import shutil
import logging
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

CHANNELS = ("high", "low")


@dataclass
class _StreamProcess:
    camera_id: str
    channel: str          # "high" or "low"
    rtsp_url: str
    process: subprocess.Popen
    hls_dir: Path
    started_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)
    status: str = "starting"   # starting | streaming | error


class StreamManager:

    def __init__(self) -> None:
        # Keyed by "camera_id/channel", e.g. "abc123/high"
        self._streams: Dict[str, _StreamProcess] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    # -- Key helpers --------------------------------------------------------

    @staticmethod
    def _key(camera_id: str, channel: str) -> str:
        return f"{camera_id}/{channel}"

    # -- Public lifecycle ---------------------------------------------------

    async def start_cleanup_task(self) -> None:
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Stream cleanup task started (interval=30 s, timeout=%d s)",
                    settings.stream_timeout_seconds)

    async def stop_all(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
        async with self._lock:
            for key in list(self._streams):
                await self._terminate(key)
        logger.info("All streams stopped")

    async def start_stream(
        self, camera_id: str, channel: str, rtsp_url: str,
    ) -> str:
        """
        Start (or refresh) a stream for *camera_id* at the given *channel*.
        Returns the HLS playlist path, e.g. /hls/abc123/high/stream.m3u8
        """
        key = self._key(camera_id, channel)

        async with self._lock:
            existing = self._streams.get(key)
            if existing:
                if existing.process.poll() is None:
                    # Still running -- just refresh the timeout
                    existing.last_activity = datetime.utcnow()
                    return self._hls_url(camera_id, channel)
                else:
                    # Dead process -- clean up before restarting
                    await self._terminate(key)

            hls_dir = Path(settings.hls_dir) / camera_id / channel
            hls_dir.mkdir(parents=True, exist_ok=True)

            m3u8 = str(hls_dir / "stream.m3u8")
            seg_pattern = str(hls_dir / "seg_%05d.ts")

            cmd = [
                settings.ffmpeg_path,
                "-loglevel", "error",
                # Low-latency input: reduce RTSP and demuxer buffering
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "-rtsp_transport", "tcp",
                "-rtsp_flags", "prefer_tcp",
            ]
            # Only add TLS flag for encrypted RTSPS streams
            if rtsp_url.lower().startswith("rtsps://"):
                cmd += ["-tls_verify", "0"]     # Accept self-signed certs (UniFi Protect)
            cmd += [
                "-i", rtsp_url,
                # Copy the H.264 stream as-is -- no CPU-intensive transcoding
                "-c:v", "copy",
                "-c:a", "aac",
                "-map", "0:v:0",
                "-map", "0:a:0?",       # Optional audio -- won't fail if no audio track
                "-f", "hls",
                "-hls_time", str(settings.hls_segment_time),
                "-hls_list_size", str(settings.hls_list_size),
                "-hls_flags", "delete_segments",
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
                channel=channel,
                rtsp_url=rtsp_url,
                process=process,
                hls_dir=hls_dir,
            )
            self._streams[key] = stream

        # Wait for the playlist to appear (outside the lock so other calls aren't blocked)
        await self._wait_ready(key)
        return self._hls_url(camera_id, channel)

    async def stop_stream(self, camera_id: str, channel: str) -> None:
        """Stop a single channel stream for a camera."""
        key = self._key(camera_id, channel)
        async with self._lock:
            await self._terminate(key)

    async def stop_camera_streams(self, camera_id: str) -> None:
        """Stop all channel streams (high + low) for a camera."""
        async with self._lock:
            for ch in CHANNELS:
                await self._terminate(self._key(camera_id, ch))

    def touch_activity(self, stream_key: str) -> None:
        """Called by the HLS route to extend the stream timeout."""
        stream = self._streams.get(stream_key)
        if stream:
            stream.last_activity = datetime.utcnow()

    def get_status(self, camera_id: str, channel: str) -> dict:
        """Get status for a single channel."""
        key = self._key(camera_id, channel)
        stream = self._streams.get(key)
        if not stream:
            return {"status": "idle"}
        if stream.process.poll() is not None:
            return {"status": "error"}
        return {
            "status": stream.status,
            "hls_url": self._hls_url(camera_id, channel),
            "started_at": stream.started_at.isoformat(),
            "last_activity": stream.last_activity.isoformat(),
            "pid": stream.process.pid,
        }

    def get_camera_status(self, camera_id: str) -> dict:
        """Get combined status for all channels of a camera."""
        return {
            ch: self.get_status(camera_id, ch)
            for ch in CHANNELS
        }

    def active_count(self) -> int:
        return len(self._streams)

    def active_streams_list(self) -> List[dict]:
        """Return summary of all active streams."""
        result = []
        for key, stream in self._streams.items():
            alive = stream.process.poll() is None
            result.append({
                "camera_id": stream.camera_id,
                "channel": stream.channel,
                "status": stream.status if alive else "error",
                "hls_url": self._hls_url(stream.camera_id, stream.channel),
                "started_at": stream.started_at.isoformat(),
            })
        return result

    # -- Internal helpers ---------------------------------------------------

    async def _wait_ready(
        self, key: str, timeout: float = None,
    ) -> None:
        timeout = timeout or settings.stream_start_wait_seconds
        stream = self._streams.get(key)
        if not stream:
            return
        m3u8 = stream.hls_dir / "stream.m3u8"
        deadline = asyncio.get_event_loop().time() + timeout
        interval = 0.4

        while asyncio.get_event_loop().time() < deadline:
            stream = self._streams.get(key)
            if not stream:
                return  # Stopped externally

            if stream.process.poll() is not None:
                stream.status = "error"
                stderr_bytes = stream.process.stderr.read(2048) if stream.process.stderr else b""
                stderr_text = stderr_bytes.decode(errors="replace").strip()
                logger.error(
                    "FFmpeg for %s exited early (code %d). stderr: %s",
                    key, stream.process.returncode, stderr_text,
                )
                # Clean up the dead stream
                async with self._lock:
                    await self._terminate(key)
                raise RuntimeError(
                    f"FFmpeg exited immediately (code {stream.process.returncode}): "
                    f"{stderr_text or 'no output'}"
                )

            if m3u8.exists() and m3u8.stat().st_size > 0:
                stream.status = "streaming"
                logger.info("Stream %s ready", key)
                return

            await asyncio.sleep(interval)

        # Timed out -- process might still connect; keep it alive but mark streaming
        stream = self._streams.get(key)
        if stream and stream.process.poll() is None:
            stream.status = "streaming"
            logger.warning(
                "Stream %s did not produce a playlist within %.1f s — "
                "check the RTSP URL and network connectivity.",
                key,
                timeout,
            )

    async def _terminate(self, key: str) -> None:
        """Kill FFmpeg and remove HLS files. Must be called while holding self._lock."""
        stream = self._streams.pop(key, None)
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

        logger.info("Stream %s stopped and HLS files cleaned up", key)

    @staticmethod
    def _hls_url(camera_id: str, channel: str) -> str:
        return f"/hls/{camera_id}/{channel}/stream.m3u8"

    # -- Cleanup loop -------------------------------------------------------

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

        for key, stream in self._streams.items():
            idle_seconds = (now - stream.last_activity).total_seconds()
            if idle_seconds > settings.stream_timeout_seconds:
                logger.info(
                    "Stream %s idle for %.0f s — stopping", key, idle_seconds,
                )
                to_stop.append(key)
            elif stream.process.poll() is not None:
                logger.warning("Stream %s process died unexpectedly", key)
                to_stop.append(key)

        for key in to_stop:
            async with self._lock:
                await self._terminate(key)


# Singleton used by all API routes
stream_manager = StreamManager()
