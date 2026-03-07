"""
Camera Registry — in-memory store of camera definitions from cloud config.

The cloud is the single source of truth. This registry is updated on each
config poll. The stream manager consults it for RTSP URLs when starting streams.
"""

import logging
from dataclasses import dataclass
from typing import Dict, Optional, List, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CameraConfig:
    camera_id: str
    name: str
    rtsp_url: str
    rtsp_url_low: str = ""
    segment_duration: int = 2


class CameraRegistry:
    def __init__(self) -> None:
        self._cameras: Dict[str, CameraConfig] = {}

    def get(self, camera_id: str) -> Optional[CameraConfig]:
        return self._cameras.get(camera_id)

    def all_ids(self) -> List[str]:
        return list(self._cameras.keys())

    def update_from_config(
        self, cameras: List[dict]
    ) -> Tuple[Set[str], Set[str], Set[str]]:
        """
        Reconcile in-memory registry with cloud config.
        Returns (added_ids, removed_ids, updated_ids) so the caller
        can stop streams for removed/changed cameras.
        """
        new_ids = {c["id"] for c in cameras}
        old_ids = set(self._cameras.keys())

        added = new_ids - old_ids
        removed = old_ids - new_ids
        retained = new_ids & old_ids

        updated: Set[str] = set()
        for cam_dict in cameras:
            cid = cam_dict["id"]
            new_config = CameraConfig(
                camera_id=cid,
                name=cam_dict.get("name", ""),
                rtsp_url=cam_dict["rtsp_url"],
                rtsp_url_low=cam_dict.get("rtsp_url_low", ""),
                segment_duration=cam_dict.get("segment_duration", 2),
            )
            if cid in retained:
                old = self._cameras[cid]
                if (old.rtsp_url != new_config.rtsp_url
                        or old.rtsp_url_low != new_config.rtsp_url_low):
                    updated.add(cid)
            self._cameras[cid] = new_config

        for cid in removed:
            del self._cameras[cid]

        return added, removed, updated


camera_registry = CameraRegistry()
