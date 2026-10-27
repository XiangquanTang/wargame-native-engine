"""Provider-neutral static-map function over host-registered NativeMap snapshots.

Conversion is lazy and cached for the lifetime of this standalone adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
from threading import RLock
from typing import Any

from wargame_native_engine.io.scenario import NativeMap
from wargame_native_engine.io.structured_map import structure_map


MAX_MAP_PAGE_CELLS = 256


class MapToolError(ValueError):
    """Stable function-boundary error; report ``code`` as a tool error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _map_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise MapToolError("invalid_arguments", "map_id must contain 1–128 characters")
    return value


class MapToolAdapter:
    """Snapshot an allowlist, then return bounded pages of one canonical export.

    Registry keys must equal each map's native map_id. To replace a map, create a
    new adapter; response map_hash lets the host detect mixed source versions.
    """

    def __init__(
        self,
        maps: Mapping[str, NativeMap],
        *,
        max_response_bytes: int = 512 * 1024,
    ) -> None:
        if not isinstance(maps, Mapping):
            raise TypeError("maps must be a map_id to NativeMap mapping")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        self._maps: dict[str, NativeMap] = {}
        for map_id, native_map in maps.items():
            _map_id(map_id)
            if not isinstance(native_map, NativeMap):
                raise TypeError("host must load each approved source into a NativeMap")
            if map_id != native_map.map_id:
                raise ValueError("registry key must equal NativeMap.map_id")
            # Snapshot even manually constructed NativeMaps whose tuple-typed
            # fields may actually contain caller-owned mutable lists.
            self._maps[map_id] = deepcopy(native_map)
        self._cache: dict[str, dict[str, Any]] = {}
        self._max_response_bytes = max_response_bytes
        self._lock = RLock()

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate model arguments and return a detached JSON-compatible page."""
        if name != "structured_map":
            raise MapToolError("unknown_tool", "tool name is not allowlisted")
        if type(arguments) is not dict or arguments.keys() != {"map_id", "offset", "limit"}:
            raise MapToolError("invalid_arguments", "missing or unsupported argument fields")
        map_id = _map_id(arguments["map_id"])
        offset, limit = arguments["offset"], arguments["limit"]
        if type(offset) is not int or offset < 0:
            raise MapToolError("invalid_arguments", "offset must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= MAX_MAP_PAGE_CELLS:
            raise MapToolError("invalid_arguments", f"limit must be an integer from 1 to {MAX_MAP_PAGE_CELLS}")
        with self._lock:
            if map_id not in self._maps:
                raise MapToolError("unknown_map", "map_id is not registered by the host")
            if map_id not in self._cache:
                try:
                    self._cache[map_id] = structure_map(self._maps[map_id])
                except ValueError as exc:
                    raise MapToolError("invalid_map", str(exc)) from exc
            document = self._cache[map_id]
            total = document["cell_count"]
            if offset > total:
                raise MapToolError("invalid_offset", "offset exceeds the map cell count")
            cells = document["cells"][offset:offset + limit]
            end = offset + len(cells)
            response = {
                **document,
                "cells": cells,
                "pagination": {
                    "offset": offset,
                    "limit": limit,
                    "returned": len(cells),
                    "total": total,
                    "next_offset": end if end < total else None,
                    "has_more": end < total,
                },
            }
            encoded = json.dumps(
                response, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
            if len(encoded.encode("utf-8")) > self._max_response_bytes:
                raise MapToolError(
                    "response_too_large",
                    "response exceeds host byte limit; retry with a smaller limit",
                )
            return json.loads(encoded)


def function_schemas() -> list[dict[str, Any]]:
    """Return a fresh closed JSON Schema; wrap it for the chosen LLM provider."""
    return deepcopy([{
        "name": "structured_map",
        "description": (
            "Convert a host-approved static map to canonical terrain, elevation, "
            "and directional-edge rows. Start at offset 0 and follow next_offset "
            "until has_more is false."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "map_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_MAP_PAGE_CELLS},
            },
            "required": ["map_id", "offset", "limit"],
            "additionalProperties": False,
        },
    }])
