"""Side-only observation encoding with no GREEN label channel."""

from __future__ import annotations

from typing import Any, Mapping

from wargame_native_engine.gateway import ControllerView
from wargame_native_engine.io.serialization import json_clone


class SideObservationEncoder:
    """Encode the existing detached controller observation as JSON values."""

    def encode(self, view: ControllerView) -> Mapping[str, Any]:
        value = json_clone(view.observation)
        if value.get("side") != view.perspective.lower():
            raise ValueError("training observation perspective mismatch")
        if "green" in value or "frame_analysis" in value:
            raise ValueError("training observation contains referee-only data")
        return value
