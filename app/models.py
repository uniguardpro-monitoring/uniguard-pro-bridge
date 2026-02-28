from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base


class NVR(Base):
    """Represents a UniFi Protect NVR (or any RTSP host) on the LAN."""

    __tablename__ = "nvrs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    ip_address = Column(String(45), nullable=False, unique=True)
    username = Column(String(128), nullable=True)
    password = Column(String(256), nullable=True)
    rtsp_port = Column(Integer, default=7441)
    api_verified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, nullable=True)

    cameras = relationship("Camera", back_populates="nvr", cascade="all, delete-orphan")


class Camera(Base):
    """
    Represents an RTSP camera stream.
    May be linked to an NVR (auto-discovered) or added manually.
    """

    __tablename__ = "cameras"

    id = Column(Integer, primary_key=True, index=True)
    nvr_id = Column(Integer, ForeignKey("nvrs.id", ondelete="SET NULL"), nullable=True)
    name = Column(String(128), nullable=False)
    rtsp_url = Column(Text, nullable=False)
    camera_uid = Column(String(64), nullable=True)  # UniFi camera unique ID
    channel_name = Column(String(64), nullable=True)  # e.g. "High", "Low"
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, nullable=True)

    nvr = relationship("NVR", back_populates="cameras")
