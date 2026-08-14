"""Deterministic conversion of one audited benchmark ``basic.json`` map.

The converter is deliberately an offline boundary.  It never imports the old
benchmark project and a viewer map does not become adjudication input merely
because its identifier matches a scenario.  Callers must explicitly select the
source file and supply any source metadata that is absent from that file.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping, Sequence

from wargame_native_engine.io.scenario import NativeHex, NativeMap
from wargame_native_engine.io.serialization import canonical_json
from wargame_native_engine.viewer.map_config import (
    ViewerMapConfig,
    ViewerMapConfigError,
    load_viewer_map_config,
)


NATIVE_MAP_CONVERSION_SCHEMA = "native-map-conversion/1.0"
NATIVE_MAP_CONVERTER_VERSION = "1.0.0"
_OPPOSITE_DIRECTIONS = (3, 4, 5, 0, 1, 2)
_KNOWN_TERRAIN_CODES = frozenset({0, 1, 2, 3, 4, 5})
_KNOWN_ROAD_CODES = frozenset({0, 1, 2, 3})
_KNOWN_RIVER_CODES = frozenset({0, 1})


class NativeMapConversionError(ValueError):
    """Raised when source map evidence is insufficient or inconsistent."""


@dataclass(frozen=True, slots=True)
class NativeMapConversion:
    native_map: NativeMap
    native_map_payload: bytes
    provenance: Mapping[str, Any]
    provenance_payload: bytes

    @property
    def native_map_sha256(self) -> str:
        return hashlib.sha256(self.native_map_payload).hexdigest()

    @property
    def provenance_sha256(self) -> str:
        return hashlib.sha256(self.provenance_payload).hexdigest()


@dataclass(frozen=True, slots=True)
class PublishedNativeMapConversion:
    output_dir: Path
    native_map_path: Path
    provenance_path: Path
    native_map_sha256: str
    provenance_sha256: str


def convert_benchmark_basic_map(
    source_path: str | Path,
    *,
    coordinate_system_override: str | None = None,
    hex_size_m_override: int | None = None,
    elevation_unit_m_override: float | None = None,
) -> NativeMapConversion:
    """Convert one explicit old ``basic.json`` into canonical NativeMap facts.

    ``elev`` is preserved as an elevation in metres.  The source ``ele_grade``
    (or an explicit override) is the map's minimum elevation step and becomes
    ``elevation_unit_m`` for Native simplified slope adjudication.
    """

    source = Path(source_path)
    raw = source.read_bytes()
    value = _read_mapping(raw, source)
    try:
        viewer_map = load_viewer_map_config(
            source,
            coordinate_system_override=coordinate_system_override,
        )
    except ViewerMapConfigError as exc:
        raise NativeMapConversionError(str(exc)) from exc

    map_id = _required_identity(value.get("map_id"), "map_id")
    if viewer_map.map_id is not None and viewer_map.map_id != map_id:
        raise NativeMapConversionError("map_id changed during map validation")
    if len(viewer_map.cells) != viewer_map.rows * viewer_map.columns:
        raise NativeMapConversionError(
            "native adjudication map requires a complete rectangular grid"
        )
    if viewer_map.columns > 100:
        raise NativeMapConversionError(
            "row_times_100_plus_col cannot represent more than 100 columns"
        )

    hex_size_m, hex_size_source = _resolve_positive_int(
        value.get("scale"),
        hex_size_m_override,
        "scale",
        "hex_size_m_override",
    )
    elevation_unit_m, elevation_unit_source = _resolve_positive_number(
        value.get("ele_grade"),
        elevation_unit_m_override,
        "ele_grade",
        "elevation_unit_m_override",
    )

    by_hex_id = {int(cell.position_id): cell for cell in viewer_map.cells}
    if len(by_hex_id) != len(viewer_map.cells):
        raise NativeMapConversionError(
            "source position strings do not map to unique numeric hex ids"
        )
    _validate_native_facts(viewer_map, by_hex_id)

    native_hexes = tuple(
        NativeHex(
            hex_id=int(cell.position_id),
            elevation=cell.elevation,
            terrain=(
                "UNKNOWN" if cell.terrain_code is None else cell.terrain_code
            ),
            neighbors=cell.neighbors or (),
            roads=cell.roads,
            rivers=cell.rivers,
        )
        for cell in viewer_map.cells
    )
    native_map = NativeMap(
        map_id=map_id,
        width=float(viewer_map.columns * hex_size_m),
        height=float(viewer_map.rows * hex_size_m),
        distance_unit="m",
        hexes=native_hexes,
        hex_size_m=hex_size_m,
        elevation_unit_m=elevation_unit_m,
        elevation_kind="METERS",
    )
    native_map_payload = _payload(native_map.to_dict())
    native_map_sha256 = hashlib.sha256(native_map_payload).hexdigest()
    warnings, edge_counts = _edge_diagnostics(viewer_map, by_hex_id)
    position_identities = [
        {
            "row": cell.row,
            "column": cell.column,
            "source_position_id": cell.position_id,
            "native_hex_id": int(cell.position_id),
        }
        for cell in viewer_map.cells
    ]
    provenance: dict[str, Any] = {
        "schema": NATIVE_MAP_CONVERSION_SCHEMA,
        "converter_version": NATIVE_MAP_CONVERTER_VERSION,
        "source": {
            "path_identity": str(source.resolve()),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        "map_id": map_id,
        "coordinate_system": {
            "value": "odd-r",
            "source": viewer_map.coordinate_system_source,
            "orientation": "pointy-top",
            "direction_order": ["E", "NE", "NW", "W", "SW", "SE"],
        },
        "conversion_options": {
            "hex_size_m": hex_size_m,
            "hex_size_source": hex_size_source,
            "elevation_kind": "METERS",
            "elevation_unit_m": elevation_unit_m,
            "elevation_unit_source": elevation_unit_source,
        },
        "output": {
            "sha256": native_map_sha256,
            "cell_count": len(native_hexes),
            "rows": viewer_map.rows,
            "columns": viewer_map.columns,
        },
        "coverage": {
            "terrain_cells": sum(
                cell.terrain_code is not None for cell in viewer_map.cells
            ),
            "elevation_cells": sum(
                cell.elevation is not None for cell in viewer_map.cells
            ),
            **edge_counts,
        },
        "position_identities": position_identities,
        "warnings": list(warnings),
    }
    provenance_payload = _payload(provenance)
    return NativeMapConversion(
        native_map=native_map,
        native_map_payload=native_map_payload,
        provenance=provenance,
        provenance_payload=provenance_payload,
    )


def publish_native_map_conversion(
    conversion: NativeMapConversion,
    output_dir: str | Path,
) -> PublishedNativeMapConversion:
    """Publish both deterministic conversion documents as one new directory."""

    target = Path(output_dir)
    if target.exists():
        raise FileExistsError(f"native map output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f".{target.name}.staging-",
        dir=target.parent,
    ))
    map_name = "native_map.native.json"
    provenance_name = "native_map_conversion.native.json"
    try:
        (staging / map_name).write_bytes(conversion.native_map_payload)
        (staging / provenance_name).write_bytes(conversion.provenance_payload)
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return PublishedNativeMapConversion(
        output_dir=target,
        native_map_path=target / map_name,
        provenance_path=target / provenance_name,
        native_map_sha256=conversion.native_map_sha256,
        provenance_sha256=conversion.provenance_sha256,
    )


def _read_mapping(raw: bytes, source: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeMapConversionError(
            f"invalid map config JSON: {source}"
        ) from exc
    if not isinstance(value, Mapping):
        raise NativeMapConversionError("map config root must be an object")
    return value


def _required_identity(value: object, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise NativeMapConversionError(f"{field} must be a string or int")
    result = str(value)
    if not result:
        raise NativeMapConversionError(f"{field} must be non-empty")
    return result


def _resolve_positive_int(
    source_value: object,
    override: int | None,
    source_field: str,
    override_field: str,
) -> tuple[int, str]:
    if source_value is not None:
        if type(source_value) is not int or source_value <= 0:
            raise NativeMapConversionError(
                f"{source_field} must be a positive exact int"
            )
        if override is not None and override != source_value:
            raise NativeMapConversionError(
                f"{override_field} conflicts with source {source_field}"
            )
        return source_value, "source"
    if type(override) is not int or override <= 0:
        raise NativeMapConversionError(
            f"missing {source_field}; {override_field} is required"
        )
    return override, "explicit_override"


def _resolve_positive_number(
    source_value: object,
    override: float | None,
    source_field: str,
    override_field: str,
) -> tuple[float, str]:
    if source_value is not None:
        result = _positive_number(source_value, source_field)
        if override is not None and _positive_number(
            override, override_field
        ) != result:
            raise NativeMapConversionError(
                f"{override_field} conflicts with source {source_field}"
            )
        return result, "source"
    if override is None:
        raise NativeMapConversionError(
            f"missing {source_field}; {override_field} is required"
        )
    return _positive_number(override, override_field), "explicit_override"


def _positive_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NativeMapConversionError(f"{field} must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise NativeMapConversionError(f"{field} must be a positive number")
    return result


def _validate_native_facts(
    viewer_map: ViewerMapConfig,
    by_hex_id: Mapping[int, object],
) -> None:
    for cell in viewer_map.cells:
        if cell.neighbors is None:
            raise NativeMapConversionError(
                f"map cell {cell.position_id} is missing neighbors"
            )
        if cell.terrain_code not in _KNOWN_TERRAIN_CODES:
            raise NativeMapConversionError(
                f"map cell {cell.position_id} has unknown terrain code"
            )
        if any(value not in _KNOWN_ROAD_CODES for value in cell.roads):
            raise NativeMapConversionError(
                f"map cell {cell.position_id} has unknown road code"
            )
        if any(value not in _KNOWN_RIVER_CODES for value in cell.rivers):
            raise NativeMapConversionError(
                f"map cell {cell.position_id} has unknown river code"
            )
        source_id = int(cell.position_id)
        for direction, neighbor in enumerate(cell.neighbors):
            if neighbor == -1:
                continue
            other = by_hex_id.get(neighbor)
            if other is None:
                raise NativeMapConversionError(
                    f"hex {source_id} direction {direction} references unknown neighbor {neighbor}"
                )
            reverse = other.neighbors[_OPPOSITE_DIRECTIONS[direction]]  # type: ignore[attr-defined]
            if reverse != source_id:
                raise NativeMapConversionError(
                    f"hex {source_id} and neighbor {neighbor} are not reciprocal"
                )


def _edge_diagnostics(
    viewer_map: ViewerMapConfig,
    by_hex_id: Mapping[int, object],
) -> tuple[tuple[str, ...], dict[str, int]]:
    road_directed = river_directed = 0
    asymmetric_roads: set[tuple[int, int]] = set()
    asymmetric_rivers: set[tuple[int, int]] = set()
    for cell in viewer_map.cells:
        source_id = int(cell.position_id)
        for direction, neighbor in enumerate(cell.neighbors or ()):
            if cell.roads[direction] > 0:
                road_directed += 1
            if cell.rivers[direction] > 0:
                river_directed += 1
            if neighbor < 0:
                continue
            other = by_hex_id[neighbor]
            reverse = _OPPOSITE_DIRECTIONS[direction]
            edge = tuple(sorted((source_id, neighbor)))
            if cell.roads[direction] != other.roads[reverse]:  # type: ignore[attr-defined]
                asymmetric_roads.add(edge)
            if cell.rivers[direction] != other.rivers[reverse]:  # type: ignore[attr-defined]
                asymmetric_rivers.add(edge)
    warnings = []
    if asymmetric_roads:
        warnings.append(
            f"asymmetric_road_edges_preserved:{len(asymmetric_roads)}"
        )
    if asymmetric_rivers:
        warnings.append(
            f"asymmetric_river_edges_preserved:{len(asymmetric_rivers)}"
        )
    return tuple(warnings), {
        "road_directed_edge_facts": road_directed,
        "river_directed_edge_facts": river_directed,
        "asymmetric_road_edges": len(asymmetric_roads),
        "asymmetric_river_edges": len(asymmetric_rivers),
    }


def _payload(value: object) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")
