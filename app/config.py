from pydantic_settings import BaseSettings
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8080

    hls_dir: str = str(BASE_DIR / "hls")
    db_path: str = str(BASE_DIR / "uniguard.db")

    # Stream lifecycle
    stream_timeout_seconds: int = 300       # 5 minutes idle → auto-stop
    stream_start_wait_seconds: float = 10.0 # Max wait for first .ts segment

    # FFmpeg
    ffmpeg_path: str = "ffmpeg"
    hls_segment_time: int = 2               # Seconds per HLS segment
    hls_list_size: int = 6                  # Segments kept in playlist

    # LAN discovery
    scan_timeout_seconds: float = 1.5       # Per-host TCP connect timeout
    unifi_api_port: int = 443

    class Config:
        env_file = ".env"
        env_prefix = "UGBRIDGE_"


settings = Settings()
