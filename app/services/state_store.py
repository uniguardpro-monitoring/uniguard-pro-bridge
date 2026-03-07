"""
Local State Store — persists clientId and tunnelToken to a JSON file.

Atomic writes (write to .tmp, then os.replace) to survive power loss on Pi.
"""

import json
import os
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from ..config import settings

logger = logging.getLogger(__name__)


@dataclass
class BridgeState:
    client_id: Optional[str] = None
    tunnel_token: Optional[str] = None


class StateStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self._path = Path(path or settings.state_file)

    def load(self) -> BridgeState:
        if not self._path.exists():
            return BridgeState()
        try:
            data = json.loads(self._path.read_text())
            return BridgeState(
                client_id=data.get("clientId"),
                tunnel_token=data.get("tunnelToken"),
            )
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Corrupt state file %s: %s — starting fresh", self._path, exc)
            return BridgeState()

    def save(self, state: BridgeState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "clientId": state.client_id,
            "tunnelToken": state.tunnel_token,
        }, indent=2))
        os.replace(str(tmp), str(self._path))
        logger.info("State saved to %s", self._path)


state_store = StateStore()
