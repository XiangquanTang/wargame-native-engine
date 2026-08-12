"""Self-contained, read-only map presentation configuration for the viewer.

The accepted JSON shape is the bounded subset used by the mature benchmark
PyQt renderer: an odd-r ``map_data`` grid with optional elevation, terrain,
road and river facts. The viewer never imports the benchmark project or SDK
resources at runtime.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

from wargame_native_engine.compat.source_frame.models import SourceFrame


DEFAULT_HEX_SIZE = 18.0
MAX_MAP_CELLS = 100_000

_TERRAIN_NAMES = {
    0: "open",
    1: "forest",
    2: "settlement",
    3: "soft_ground",
    4: "water",
}


class ViewerMapConfigError(ValueError):
    """Raised when an explicit viewer map configuration is malformed."""


@dataclass(frozen=True, slots=True)
class ViewerMapCell:
    row: int
    column: int
    elevation: float | None
    terrain_code: int | None
    terrain_semantic: str
    roads: tuple[int, int, int, int, int, int]
    rivers: tuple[int, int, int, int, int, int]

    def to_record(self) -> dict[str, object]:
        return {
            "row": self.row,
            "column": self.column,
            "elevation": self.elevation,
            "terrain_code": self.terrain_code,
            "terrain_semantic": self.terrain_semantic,
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
            "cells": [cell.to_record() for cell in self.cells],
            "source": {
                "name": self.source_name,
                "sha256": self.source_sha256,
            },
        }


def load_viewer_map_config(path: str | Path) -> ViewerMapConfig:
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
            cells.append(ViewerMapCell(
                row=cell_row,
                column=cell_column,
                elevation=_optional_finite_number(raw_cell.get("elev"), "elev"),
                terrain_code=terrain_code,
                terrain_semantic=_TERRAIN_NAMES.get(terrain_code, "unknown"),
                roads=_edge_values(raw_cell.get("roads")),
                rivers=_edge_values(raw_cell.get("rivers")),
            ))
    if not cells:
        raise ViewerMapConfigError("map config contains no valid cells")

    cells.sort(key=lambda cell: (cell.row, cell.column))
    raw_map_id = value.get("map_id")
    map_id = None if raw_map_id is None else str(raw_map_id)
    return ViewerMapConfig(
        map_id=map_id,
        rows=rows,
        columns=columns,
        hex_size=hex_size,
        cells=tuple(cells),
        source_name=source.name,
        source_sha256=hashlib.sha256(payload).hexdigest(),
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
