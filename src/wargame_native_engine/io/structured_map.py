"""Deterministic static hex-map facts for direct input and function calling.

This projection is not an observation, movement planner, or game-state query.
Only an explicitly supplied NativeMap or basic-map file is read. Cell rows are
sorted by (y, x); directional facts retain their source order and asymmetry.
"""

from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.io.scenario import NativeMap, native_map_from_dict
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.viewer.map_config import MAX_MAP_CELLS, load_viewer_map_config


STRUCTURED_MAP_SCHEMA = "native-structured-map/1.0"
CELL_COLUMNS = (
    "x", "y", "elevation_m", "elevation_level", "terrain_code", "position_id",
    "elevation_raw", "terrain_raw", "terrain_name", "roadblock",
    "neighbors", "roads", "rivers",
)
DIRECTION_ORDER = ("E", "NE", "NW", "W", "SW", "SE")
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_EXACT_INTEGER = 2**53 - 1
MAX_TEXT_LENGTH = 256
_TERRAIN_NAMES = ("NORMAL", "FOREST", "SETTLEMENT", "SOFT_GROUND", "LARGE_RIVER", "ROADBLOCK")
_INPUT_FORMATS = ("native-scenario", "native-map", "basic")


class StructuredMapError(ValueError):
    """The explicit source cannot be projected without guessing or data loss."""


def structure_map(native_map: NativeMap) -> dict[str, Any]:
    """Return detached, JSON-safe static facts from a Native hex map.

    Coordinates are odd-r indices, not metres: x is column, y is row, and
    position_id is y * 100 + x. A metric elevation becomes an integer grade
    only when its quotient by the declared step is exactly integral; otherwise
    the grade remains null. No rounding, palette bins, or minimum subtraction.
    """
    if not isinstance(native_map, NativeMap):
        raise StructuredMapError("structure_map requires a NativeMap")
    raw = native_map.to_dict()
    _validate_native(raw)
    # Normalize direct construction exactly as the canonical Native reader does.
    native_map = native_map_from_dict(raw)
    roadblocks = set(native_map.roadblocks)
    rows = [
        _cell_row(
            cell.hex_id, cell.elevation, cell.terrain,
            native_map.elevation_kind, native_map.elevation_unit_m,
            cell.hex_id in roadblocks, cell.neighbors, cell.roads, cell.rivers,
        )
        for cell in native_map.hexes
    ]
    return _document(
        rows, map_id=native_map.map_id,
        kind=native_map.elevation_kind, unit=native_map.elevation_unit_m,
        physical={"width": native_map.width, "height": native_map.height,
                  "distance_unit": native_map.distance_unit,
                  "hex_size_m": native_map.hex_size_m},
        source={"input_format": "native-map", "name": None, "sha256": None},
    )


def load_structured_map(
    path: str | Path,
    *,
    input_format: str,
    coordinate_system_override: str | None = None,
) -> dict[str, Any]:
    """Read an explicitly named format; never infer one from a file name.

    Native scenarios contribute only their map object, not operators, objectives,
    deployment, scenario metadata, or a scenario identity hash. For basic maps,
    missing directional arrays stay null instead of becoming invented edges.
    """
    if input_format not in _INPUT_FORMATS:
        raise StructuredMapError(f"input_format must be one of {_INPUT_FORMATS}")
    if coordinate_system_override is not None and input_format != "basic":
        raise StructuredMapError("coordinate_system_override is only valid for basic maps")
    source = Path(path)
    with source.open("rb") as stream:
        payload = stream.read(MAX_SOURCE_BYTES + 1)
    if len(payload) > MAX_SOURCE_BYTES:
        raise StructuredMapError(f"source exceeds {MAX_SOURCE_BYTES} byte limit")
    try:
        value = json.loads(
            payload.decode("utf-8-sig"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant, parse_float=_json_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StructuredMapError("source must contain valid UTF-8 JSON") from exc
    value = _mapping(value, "source")
    if input_format == "basic":
        result = _basic_document(source, value, coordinate_system_override, hashlib.sha256(payload).hexdigest())
    else:
        raw_map = _mapping(value.get("map"), "map") if input_format == "native-scenario" else value
        _validate_native(raw_map)
        result = structure_map(native_map_from_dict(raw_map))
    result["source"] = {
        "input_format": input_format, "name": source.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    return result


def _basic_document(
    path: Path, raw: Mapping[str, Any], override: str | None, expected_hash: str,
) -> dict[str, Any]:
    _elevation_metadata(raw.get("elevation_kind"), raw.get("elevation_unit_m"))
    if _array(raw.get("blocked_positions", []), "blocked_positions"):
        raise StructuredMapError("continuous blocked_positions are unsupported by the static hex-map export")
    if raw.get("map_id") is not None:
        _identity(raw["map_id"])
    if raw.get("ele_grade") is not None:
        _number(raw["ele_grade"], "ele_grade", positive=True)
        if raw.get("elevation_unit_m") is not None and raw["ele_grade"] != raw["elevation_unit_m"]:
            raise StructuredMapError("ele_grade conflicts with elevation_unit_m")
    if raw.get("scale") is not None:
        _integer(raw["scale"], "scale", minimum=1)
    grid = _array(raw.get("map_data"), "map_data")
    source_cells: dict[int, Mapping[str, Any]] = {}
    for row_index, raw_row in enumerate(grid):
        if raw_row is None:
            continue
        for column_index, cell in enumerate(_array(raw_row, "map_data[]")):
            if cell is None:
                continue
            cell = _mapping(cell, "map_data[][]")
            y = _integer(cell.get("r", row_index), "r")
            x = _integer(cell.get("c", column_index), "c")
            if x >= 100:
                raise StructuredMapError("row_times_100_plus_col requires x < 100")
            _integer(y * 100 + x, "position_id")
            source_cells[y * 100 + x] = cell
    viewer = load_viewer_map_config(path, coordinate_system_override=override)
    if viewer.source_sha256 != expected_hash:
        raise StructuredMapError("basic map source changed during loading")
    if viewer.columns > 100:
        raise StructuredMapError("row_times_100_plus_col requires at most 100 columns")
    roadblocks = _roadblocks(raw.get("roadblocks", []), set(source_cells))
    rows = []
    for cell in viewer.cells:
        position_id = int(cell.position_id)
        original = source_cells[position_id]
        neighbors = original.get("neighbors", original.get("nb"))
        rows.append(_cell_row(
            position_id, cell.elevation, cell.terrain_code,
            viewer.elevation_kind, viewer.elevation_unit_m, position_id in roadblocks,
            neighbors, original.get("roads"), original.get("rivers"),
        ))
    scale = raw.get("scale")
    return _document(
        rows, map_id=viewer.map_id, kind=viewer.elevation_kind, unit=viewer.elevation_unit_m,
        physical={"width": None if scale is None else viewer.columns * scale,
                  "height": None if scale is None else viewer.rows * scale,
                  "distance_unit": None if scale is None else "m", "hex_size_m": scale},
        source={"input_format": "basic", "name": None, "sha256": None},
    )


def _document(
    rows: list[list[Any]], *, map_id: str | None, kind: str | None,
    unit: float | None, physical: dict[str, Any], source: dict[str, Any],
) -> dict[str, Any]:
    if not rows or len(rows) > MAX_MAP_CELLS:
        raise StructuredMapError(f"map must contain 1..{MAX_MAP_CELLS} hex cells")
    rows.sort(key=lambda row: (row[1], row[0]))
    xs, ys = [row[0] for row in rows], [row[1] for row in rows]
    meters = [row[2] for row in rows if row[2] is not None]
    levels = {row[3] for row in rows if row[3] is not None}
    result = {
        "schema_version": STRUCTURED_MAP_SCHEMA, "map_id": map_id,
        "coordinates": {
            "system": "odd-r", "orientation": "pointy-top", "order": ["x", "y"],
            "x": "column", "y": "row", "position_encoding": "y * 100 + x",
            "direction_order": list(DIRECTION_ORDER), "origin": [min(xs), min(ys)],
        },
        "bounds": {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)},
        "dimensions": {"rows": max(ys) - min(ys) + 1, "columns": max(xs) - min(xs) + 1},
        "physical": physical,
        "elevation": {
            "kind": kind, "unit_m": unit,
            "normalization": "zero_based_exact_integral_grade_no_rounding",
            "minimum_m": min(meters) if meters else None,
            "maximum_m": max(meters) if meters else None,
            "minimum_level": min(levels) if levels else None,
            "maximum_level": max(levels) if levels else None,
            "level_count": len(levels),
        },
        "terrain_legend": {str(code): name for code, name in enumerate(_TERRAIN_NAMES)},
        "cell_count": len(rows), "cell_columns": list(CELL_COLUMNS), "cells": rows,
    }
    result["map_hash"] = canonical_hash(result)
    result["source"] = source
    return result


def _cell_row(
    position_id: int, raw_elevation: float | None, raw_terrain: int | str | None,
    kind: str | None, unit: float | None, roadblock: bool,
    neighbors: Any, roads: Any, rivers: Any,
) -> list[Any]:
    position_id = _integer(position_id, "position_id")
    elevation = None if raw_elevation is None else _number(raw_elevation, "elevation")
    meters = elevation if kind == "METERS" else None
    grade = Fraction(str(elevation)) if kind == "LEVEL" and elevation is not None else None
    if elevation is not None and unit is not None:
        if kind == "METERS":
            grade = Fraction(str(elevation)) / Fraction(str(unit))
        elif kind == "LEVEL":
            meters = _number(elevation * unit, "converted elevation_m")
    level = None
    if grade is not None and grade.denominator == 1:
        level = _integer(int(grade), "elevation_level", minimum=-MAX_EXACT_INTEGER)
    code, name = _terrain(raw_terrain)
    return [position_id % 100, position_id // 100, meters, level, code, position_id,
            elevation, raw_terrain, name, roadblock,
            _edges(neighbors, "neighbors", minimum=-1),
            _edges(roads, "roads"), _edges(rivers, "rivers")]


def _terrain(raw: Any) -> tuple[int | None, str | None]:
    if raw is not None and type(raw) not in (int, str):
        raise StructuredMapError("terrain must be an integer, string, or null")
    if type(raw) is int:
        _integer(raw, "terrain", minimum=-MAX_EXACT_INTEGER)
    if isinstance(raw, str) and len(raw) > MAX_TEXT_LENGTH:
        raise StructuredMapError(f"terrain exceeds {MAX_TEXT_LENGTH} characters")
    code = raw
    if isinstance(raw, str):
        digits = raw[1:] if raw.startswith("-") else raw
        if digits.isdigit():
            try:
                code = int(raw)
            except ValueError:
                return None, None
        else:
            name = raw.upper()
            if name in _TERRAIN_NAMES:
                return _TERRAIN_NAMES.index(name), name
            return (None, "SMALL_RIVER") if name == "SMALL_RIVER" else (None, None)
    if type(code) is int and 0 <= code < len(_TERRAIN_NAMES):
        return code, _TERRAIN_NAMES[code]
    return None, None


def _validate_native(raw: Mapping[str, Any]) -> None:
    _identity(raw.get("map_id"))
    _number(raw.get("width"), "width", positive=True)
    _number(raw.get("height"), "height", positive=True)
    distance_unit = raw.get("distance_unit", "m")
    if not isinstance(distance_unit, str) or not 0 < len(distance_unit) <= MAX_TEXT_LENGTH:
        raise StructuredMapError("distance_unit must be a bounded nonempty string")
    _integer(raw.get("hex_size_m"), "hex_size_m", minimum=1)
    _elevation_metadata(raw.get("elevation_kind"), raw.get("elevation_unit_m"))
    cells = _array(raw.get("hexes"), "hexes")
    if not cells or len(cells) > MAX_MAP_CELLS:
        raise StructuredMapError(f"map must contain 1..{MAX_MAP_CELLS} hex cells")
    seen = set()
    for raw_cell in cells:
        cell = _mapping(raw_cell, "hexes[]")
        position_id = _integer(cell.get("pos"), "pos")
        if position_id in seen:
            raise StructuredMapError(f"duplicate position_id: {position_id}")
        seen.add(position_id)
        if cell.get("elev") is not None:
            _number(cell["elev"], "elev")
        _terrain(cell.get("cond"))
        if cell.get("neighbors") is None:
            raise StructuredMapError("native hex neighbors must contain six integers")
        _edges(cell["neighbors"], "neighbors", minimum=-1)
        _edges(cell.get("roads", [0] * 6), "roads", required=True)
        _edges(cell.get("rivers", [0] * 6), "rivers", required=True)
    _roadblocks(raw.get("roadblocks", []), seen)
    if _array(raw.get("blocked_positions", []), "blocked_positions"):
        raise StructuredMapError("continuous blocked_positions are unsupported by the static hex-map export")



def _identity(value: Any) -> None:
    if type(value) not in (str, int) or not 0 < len(str(value)) <= MAX_TEXT_LENGTH:
        raise StructuredMapError("map_id must be a bounded nonempty string or integer")
    if type(value) is int:
        _integer(value, "map_id", minimum=-MAX_EXACT_INTEGER)

def _elevation_metadata(kind: Any, unit: Any) -> None:
    if kind not in (None, "METERS", "LEVEL"):
        raise StructuredMapError("elevation_kind must be METERS, LEVEL, or null")
    if unit is not None:
        _number(unit, "elevation_unit_m", positive=True)


def _roadblocks(raw: Any, ids: set[int]) -> set[int]:
    result = {_integer(value, "roadblocks[]") for value in _array(raw, "roadblocks")}
    if result - ids:
        raise StructuredMapError("roadblocks reference a position not in the map")
    return result


def _edges(value: Any, field: str, *, minimum: int = 0, required: bool = False) -> list[int] | None:
    if value is None and not required:
        return None
    values = _array(value, field)
    if len(values) != 6:
        raise StructuredMapError(f"{field} must contain six integers")
    return [_integer(item, field, minimum=minimum) for item in values]


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float):
        raise StructuredMapError(f"{field} must be a finite number")
    if type(value) is int and abs(value) > MAX_EXACT_INTEGER:
        raise StructuredMapError(f"{field} integer exceeds JSON exact-integer range")
    try:
        number = float(value)
    except OverflowError as exc:
        raise StructuredMapError(f"{field} must be a finite number") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise StructuredMapError(f"{field} must be finite" + (" and positive" if positive else ""))
    return number


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_EXACT_INTEGER:
        raise StructuredMapError(f"{field} must be an exact integer in [{minimum}, {MAX_EXACT_INTEGER}]")
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StructuredMapError(f"{field} must be an object")
    return value


def _array(value: Any, field: str) -> list[Any] | tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise StructuredMapError(f"{field} must be an array")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StructuredMapError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise StructuredMapError(f"non-finite JSON number: {value}")


def _json_float(value: str) -> float:
    return _number(float(value), "JSON number")
