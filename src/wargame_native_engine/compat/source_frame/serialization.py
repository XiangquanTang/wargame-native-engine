"""Shared deterministic serialization for Source-compatible replay facts."""

from __future__ import annotations

import json
from typing import Any, Mapping

from wargame_native_engine.io.serialization import json_clone


def canonical_source_json(value: Any) -> str:
    """Serialize a Source-compatible value exactly as the Native recorder."""

    return _canonical_normalized_source_json(json_clone(value))


def _canonical_normalized_source_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_green_frame_payload(frame: Mapping[str, Any]) -> bytes:
    """Return the exact canonical payload used for a numbered GREEN frame."""

    if not isinstance(frame, Mapping):
        raise TypeError("GREEN frame must be a Mapping")
    normalized = json_clone(frame)
    return (_canonical_normalized_source_json(normalized) + "\n").encode("utf-8")


def canonical_validated_green_frame_payload(frame: Mapping[str, Any]) -> bytes:
    """Validate and serialize one normalized snapshot of a GREEN frame."""

    if not isinstance(frame, Mapping):
        raise TypeError("GREEN frame must be a Mapping")
    normalized = json_clone(frame)
    perspective = normalized.get("extra", {}).get("native", {}).get(
        "perspective"
    )
    if perspective != "GREEN":
        raise ValueError("recorder accepts GREEN frames only")
    return (_canonical_normalized_source_json(normalized) + "\n").encode("utf-8")
