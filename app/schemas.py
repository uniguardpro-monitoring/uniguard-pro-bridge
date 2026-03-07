from pydantic import BaseModel
from typing import Optional


class StartStreamResponse(BaseModel):
    hls_url: str
    status: str


class StreamStatusResponse(BaseModel):
    status: str  # idle | starting | streaming | error
    hls_url: Optional[str] = None


class HealthResponse(BaseModel):
    version: str
    active_streams: int
