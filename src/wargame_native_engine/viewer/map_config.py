"""Self-contained, read-only map presentation configuration for the viewer.

The accepted JSON shape is the bounded subset used by the mature benchmark
PyQt renderer: an odd-r ``map_data`` grid with optional elevation, terrain,
road and river facts. The viewer never imports the benchmark project or SDK
resources at runtime. Native scenario terrain is an explicit, separately
validated input for GREEN replay presentation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wargame_native_engine.io.scenario import NativeScenario

from wargame_native_engine.compat.source_frame.models import SourceFrame
from wargame_native_engine.viewer.terrain_display import terrain_display_semantic


DEFAULT_HEX_SIZE = 18.0
MAX_MAP_CELLS = 100_000


class ViewerMapConfigError(ValueError):
    """Raised when an explicit viewer map configuration is malformed."""


@dataclass(frozen=True, slots=True)
class ViewerMapCell:
    row: int
    column: int
    position_id: str
    elevation: float | None
    terrain_level: int | None
    terrain_code: int | None
    terrain_semantic: str
    neighbors: tuple[int, int, int, int, int, int] | None
    roads: tuple[int, int, int, int, int, int]
    rivers: tuple[int, int, int, int, int, int]

    def to_record(self) -> dict[str, object]:
        return {
            "row": self.row,
            "column": self.column,
            "position_id": self.position_id,
            "elevation": self.elevation,
            "terrain_level": self.terrain_level,
            "terrain_code": self.terrain_code,
            "terrain_semantic": self.terrain_semantic,
            "neighbors": (
                None if self.neighbors is None else list(self.neighbors)
            ),
            "roads": list(self.roads),
            "rivers": list(self.rivers),
        }


@dataclass(frozen=True, slots=True)
class ViewerMapConfig:
    map_id: str | None
    rows: int
    columns: int
    hex_size: float
    cells: tuple[ViewerMapCell, ...]
    source_name: str
    source_sha256: str
    coordinate_system_source: str
    elevation_kind: str | None = None
    elevation_unit_m: float | None = None

    def to_record(self) -> dict[str, object]:
        return {
            "mode": "configured-hex-map",
            "map_id": self.map_id,
            "coordinate_system": "odd-r",
            "orientation": "pointy-top",
            "position_encoding": "row_times_100_plus_col",
            "rows": self.rows,
            "columns": self.columns,
            "bounds": {
                "min_row": 0,
                "max_row": self.rows - 1,
                "min_column": 0,
                "max_column": self.columns - 1,
            },
            "hex_size": self.hex_size,
            "elevation_kind": self.elevation_kind,
            "elevation_unit_m": self.elevation_unit_m,
            "elevation": _elevation_record(self.cells),
            "cells": [cell.to_record() for cell in self.cells],
            "source": {
                "name": self.source_name,
                "sha256": self.source_sha256,
                "coordinate_system": self.coordinate_system_source,
            },
        }


def load_native_scenario_map_record(
    path: str | Path,
    *,
    expected_scenario_id: str,
    expected_map_id: str,
    expected_scenario_hash: str | None = None,
    expected_map_hash: str | None = None,
    loaded_scenario: NativeScenario | None = None,
) -> dict[str, object]:
    """Load Native static terrain for a GREEN replay, never policy observations.

    Use the shared Native projection: physical map width/height are metres,
    not grid dimensions. Bind both identities before exposing static features.
    Archived content hashes are checked when available; historical ID-only
    bindings are explicitly marked unavailable rather than verified.
    """
    from wargame_native_engine.io.scenario import (
        load_scenario,
        native_map_identity_hash,
    )
    from wargame_native_engine.viewer.live_control import native_scenario_map_record

    source = Path(path)
    scenario = loaded_scenario if loaded_scenario is not None else load_scenario(source)
    if str(scenario.scenario_id) != expected_scenario_id:
        raise ViewerMapConfigError(
            "native scenario_id does not match replay scenario_id: "
            f"config={scenario.scenario_id!r}, replay={expected_scenario_id!r}"
        )
    if str(scenario.map.map_id) != expected_map_id:
        raise ViewerMapConfigError(
            "native map_id does not match replay terrain_id: "
            f"config={scenario.map.map_id!r}, replay={expected_map_id!r}"
        )
    if len(scenario.map.hexes) > MAX_MAP_CELLS:
        raise ViewerMapConfigError("native map exceeds viewer cell limit")
    content_binding = {}
    for field, expected, actual in (
        ("scenario_hash", expected_scenario_hash, scenario.scenario_hash),
        ("map_hash", expected_map_hash, native_map_identity_hash(scenario.map)),
    ):
        if expected is not None and expected != actual:
            raise ViewerMapConfigError(
                f"native {field} does not match replay manifest: "
                f"config={actual!r}, replay={expected!r}"
            )
        content_binding[field] = "matched" if expected is not None else "unavailable"
    record = native_scenario_map_record(scenario)
    record["source"]["name"] = source.name
    record["source"]["replay_content_binding"] = content_binding
    record["scenario"]["dynamic_units_source"] = "green_replay_frames"
    return record


def load_viewer_map_config(
    path: str | Path,
    *,
    coordinate_system_override: str | None = None,
) -> ViewerMapConfig:
    """Load one explicit odd-r basic-map JSON without SDK imports."""

    source = Path(path)
    if not source.is_file():
        raise ViewerMapConfigError(f"map config is not a file: {source}")
    payload = source.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ViewerMapConfigError(f"invalid map config JSON: {source}") from exc
    if not isinstance(value, Mapping):
        raise ViewerMapConfigError("map config root must be an object")

    coordinate_system = str(value.get("coordinate_system") or "").lower()
    override = (
        None
        if coordinate_system_override is None
        else str(coordinate_system_override).lower()
    )
    if override not in {None, "odd-r", "oddr"}:
        raise ViewerMapConfigError(
            "map coordinate-system override must be odd-r"
        )
    coordinate_system_source = "map_config"
    if not coordinate_system and override is not None:
        coordinate_system = override
        coordinate_system_source = "explicit_override"
    if coordinate_system not in {"odd-r", "oddr"}:
        raise ViewerMapConfigError(
            "map config coordinate_system must be odd-r"
        )
    grid = value.get("map_data")
    if not _is_array(grid):
        raise ViewerMapConfigError("map config map_data must be an array")

    inferred_rows = len(grid)
    inferred_columns = max(
        (len(row) for row in grid if _is_array(row)),
        default=0,
    )
    rows = _positive_exact_int(value.get("max_row", inferred_rows), "max_row")
    columns = _positive_exact_int(
        value.get("max_column", inferred_columns),
        "max_column",
    )
    if rows * columns > MAX_MAP_CELLS:
        raise ViewerMapConfigError(
            f"map config exceeds {MAX_MAP_CELLS} cells"
        )
    hex_size = _finite_number(value.get("hex_size", DEFAULT_HEX_SIZE), "hex_size")
    if hex_size <= 0:
        raise ViewerMapConfigError("map config hex_size must be positive")

    cells: list[ViewerMapCell] = []
    seen: set[tuple[int, int]] = set()
    for row_index, row in enumerate(grid):
        if not _is_array(row):
            continue
        for column_index, raw_cell in enumerate(row):
            if not isinstance(raw_cell, Mapping):
                continue
            cell_row = _cell_index(raw_cell.get("r", row_index), "r")
            cell_column = _cell_index(raw_cell.get("c", column_index), "c")
            if not (0 <= cell_row < rows and 0 <= cell_column < columns):
                raise ViewerMapConfigError(
                    f"map cell ({cell_row}, {cell_column}) is outside bounds"
                )
            coordinate = (cell_row, cell_column)
            if coordinate in seen:
                raise ViewerMapConfigError(
                    f"duplicate map cell ({cell_row}, {cell_column})"
                )
            seen.add(coordinate)
            terrain_code = _optional_exact_int(raw_cell.get("cond"), "cond")
            position_id = _position_id(
                raw_cell.get("pos"),
                row=cell_row,
                column=cell_column,
            )
            if (
                "neighbors" in raw_cell
                and "nb" in raw_cell
                and raw_cell["neighbors"] != raw_cell["nb"]
            ):
                raise ViewerMapConfigError(
                    f"map cell {position_id} has conflicting neighbors and nb"
                )
            cells.append(ViewerMapCell(
                row=cell_row,
                column=cell_column,
                position_id=position_id,
                elevation=_optional_finite_number(raw_cell.get("elev"), "elev"),
                terrain_level=_optional_exact_int(
                    raw_cell.get("terrain_level"),
                    "terrain_level",
                ),
                terrain_code=terrain_code,
                terrain_semantic=terrain_display_semantic(terrain_code),
                neighbors=_optional_neighbor_values(
                    raw_cell.get("neighbors", raw_cell.get("nb")),
                ),
                roads=_edge_values(raw_cell.get("roads")),
                rivers=_edge_values(raw_cell.get("rivers")),
            ))
    if not cells:
        raise ViewerMapConfigError("map config contains no valid cells")

    cells.sort(key=lambda cell: (cell.row, cell.column))
    raw_map_id = value.get("map_id")
    map_id = None if raw_map_id is None else str(raw_map_id)
    # Legacy basic.json declares its metre step in ele_grade. Do not derive
    # a physical step from the palette count or observed cell differences.
    raw_step = value.get("elevation_unit_m", value.get("ele_grade"))
    elevation_unit_m = (
        float(raw_step) if type(raw_step) in (int, float)
        and math.isfinite(raw_step) and raw_step > 0 else None
    )
    elevation_kind = value.get("elevation_kind")
    if elevation_kind not in ("METERS", "LEVEL"):
        elevation_kind = (
            "METERS" if elevation_kind is None
            and "ele_grade" in value and elevation_unit_m is not None else None
        )
    return ViewerMapConfig(
        map_id=map_id,
        rows=rows,
        columns=columns,
        hex_size=hex_size,
        cells=tuple(cells),
        source_name=source.name,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        coordinate_system_source=coordinate_system_source,
        elevation_kind=elevation_kind,
        elevation_unit_m=elevation_unit_m,
    )


def coordinate_fallback_record(
    frames: Sequence[SourceFrame],
) -> dict[str, object]:
    """Describe replay-coordinate bounds without inventing terrain facts."""

    positions = [
        unit.row_col
        for frame in frames
        for unit in (*frame.operators, *frame.passengers)
    ]
    if positions:
        row_values = [row for row, _ in positions]
        column_values = [column for _, column in positions]
        min_row, max_row = min(row_values), max(row_values)
        min_column, max_column = min(column_values), max(column_values)
    else:
        min_row = max_row = min_column = max_column = 0
    return {
        "mode": "coordinate-fallback",
        "map_id": None,
        "coordinate_system": "odd-r",
        "orientation": "pointy-top",
        "position_encoding": "row_times_100_plus_col",
        "rows": max_row - min_row + 1,
        "columns": max_column - min_column + 1,
        "bounds": {
            "min_row": min_row,
            "max_row": max_row,
            "min_column": min_column,
            "max_column": max_column,
        },
        "hex_size": DEFAULT_HEX_SIZE,
        "elevation_kind": None,
        "elevation_unit_m": None,
        "elevation": {
            "mode": "unavailable",
            "minimum": None,
            "maximum": None,
            "levels": 7,
        },
        "cells": [],
        "source": None,
    }


def _is_array(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _positive_exact_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ViewerMapConfigError(f"map config {field} must be a positive int")
    return value


def _cell_index(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ViewerMapConfigError(
            f"map cell {field} must be a non-negative int"
        )
    return value


def _optional_exact_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ViewerMapConfigError(f"map cell {field} must be an int or null")
    return value


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ViewerMapConfigError(f"map config {field} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ViewerMapConfigError(f"map config {field} must be finite")
    return result


def _optional_finite_number(value: object, field: str) -> float | None:
    return None if value is None else _finite_number(value, field)


def _edge_values(value: object) -> tuple[int, int, int, int, int, int]:
    if value is None:
        return (0, 0, 0, 0, 0, 0)
    if not _is_array(value) or len(value) != 6:
        raise ViewerMapConfigError("map cell roads/rivers must contain six values")
    result: list[int] = []
    for item in value:
        if type(item) is not int or item < 0:
            raise ViewerMapConfigError(
                "map cell roads/rivers values must be non-negative ints"
            )
        result.append(item)
    return tuple(result)  # type: ignore[return-value]


def _optional_neighbor_values(
    value: object,
) -> tuple[int, int, int, int, int, int] | None:
    if value is None:
        return None
    if not _is_array(value) or len(value) != 6:
        raise ViewerMapConfigError(
            "map cell neighbors must contain six values"
        )
    result: list[int] = []
    for item in value:
        if type(item) is not int or item < -1:
            raise ViewerMapConfigError(
                "map cell neighbors values must be ints greater than or equal to -1"
            )
        result.append(item)
    return tuple(result)  # type: ignore[return-value]


def _position_id(
    value: object,
    *,
    row: int,
    column: int,
) -> str:
    expected = row * 100 + column
    if value is None:
        return str(expected)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ViewerMapConfigError("map cell pos must be an int or digit string")
    text = str(value)
    if not text.isdigit() or int(text) != expected:
        raise ViewerMapConfigError(
            f"map cell pos {text!r} does not match row/column {expected}"
        )
    return text


def _elevation_record(
    cells: Sequence[ViewerMapCell],
) -> dict[str, object]:
    values = [
        cell.elevation for cell in cells
        if cell.elevation is not None
    ]
    if not values:
        return {
            "mode": "unavailable",
            "minimum": None,
            "maximum": None,
            "levels": 7,
        }
    return {
        "mode": "episode",
        "minimum": min(values),
        "maximum": max(values),
        "levels": 7,
    }
