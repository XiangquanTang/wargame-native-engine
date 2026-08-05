from __future__ import annotations

from typing import Any, Mapping

from wargame_native_engine.compat.source_frame.importer import REQUIRED_TOP_LEVEL


def validate_source_frame_document(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("source frame root must be an object")
    missing = sorted(REQUIRED_TOP_LEVEL - set(value))
    if missing:
        raise ValueError(f"source frame missing required fields: {missing}")
    for name in ("operators", "passengers", "jm_points", "cities", "actions"):
        if not isinstance(value[name], list):
            raise ValueError(f"{name} must be an array")
    for name in ("time", "scores", "landmarks", "valid_actions"):
        if not isinstance(value[name], Mapping):
            raise ValueError(f"{name} must be an object")
