from pydantic_settings import BaseSettings
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8080

    # Cloud API  (env: UGBRIDGE_API_URL)
    api_url: str = "https://olzhvzijbmaeqxbmemgt.supabase.co/functions/v1/bridge-api"
    tunnel_token: str = ""
    state_file: str = str(BASE_DIR / "state.json")
    config_poll_interval: int = 60
    heartbeat_interval: int = 30

    # HLS output
    hls_dir: str = "/tmp/hls"

    # Stream lifecycle
    stream_timeout_seconds: int = 300       # 5 minutes idle -> auto-stop
    stream_start_wait_seconds: float = 10.0 # Max wait for first .ts segment

    # FFmpeg
    ffmpeg_path: str = "ffmpeg"
    hls_segment_time: int = 1               # Seconds per HLS segment (lower = less latency)
    hls_list_size: int = 3                  # Segments kept in playlist

    class Config:
        env_file = ".env"
        env_prefix = "UGBRIDGE_"


settings = Settings()
