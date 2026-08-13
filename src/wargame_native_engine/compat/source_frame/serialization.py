"""Shared deterministic serialization for Source-compatible replay facts."""

from __future__ import annotations

import json
from typing import Any, Mapping

from wargame_native_engine.io.serialization import json_clone


def canonical_source_json(value: Any) -> str:
    """Serialize a Source-compatible value exactly as the Native recorder."""

    return json.dumps(
        json_clone(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_green_frame_payload(frame: Mapping[str, Any]) -> bytes:
    """Return the exact canonical payload used for a numbered GREEN frame."""

    if not isinstance(frame, Mapping):
        raise TypeError("GREEN frame must be a Mapping")
    return (canonical_source_json(frame) + "\n").encode("utf-8")
