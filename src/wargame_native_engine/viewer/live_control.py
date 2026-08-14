"""Presentation-only records for the local side-control page."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from wargame_native_engine.gateway import ControllerView
from wargame_native_engine.io.scenario import NativeScenario


_TERRAIN_NAMES = {
    0: "open",
    1: "forest",
    2: "settlement",
    3: "soft_ground",
    4: "water",
}


def native_scenario_map_record(scenario: NativeScenario) -> dict[str, object]:
    """Project immutable Native map facts into the existing map renderer."""

    cells = []
    for cell in scenario.map.hexes:
        row, column = divmod(cell.hex_id, 100)
        terrain_code = cell.terrain if type(cell.terrain) is int else None
        cells.append({
            "row": row,
            "column": column,
            "position_id": str(cell.hex_id),
            "elevation": cell.elevation,
            "terrain_level": (
                int(cell.elevation)
                if scenario.map.elevation_kind == "LEVEL"
                and cell.elevation is not None
                else None
            ),
            "terrain_code": terrain_code,
            "terrain_semantic": _TERRAIN_NAMES.get(terrain_code, "unknown"),
            "roads": list(cell.roads),
            "rivers": list(cell.rivers),
        })
    coordinates = [(item["row"], item["column"]) for item in cells]
    if not coordinates:
        coordinates = [
            divmod(operator.current_hex, 100)
            for operator in scenario.operators
            if operator.current_hex is not None
        ]
    if coordinates:
        rows = [item[0] for item in coordinates]
        columns = [item[1] for item in coordinates]
        min_row, max_row = min(rows), max(rows)
        min_column, max_column = min(columns), max(columns)
    else:
        min_row = max_row = min_column = max_column = 0
    elevations = [
        item["elevation"] for item in cells
        if item["elevation"] is not None
    ]
    features = []
    for objective in scenario.objectives:
        row = int(objective.y)
        column = int(objective.x)
        if objective.x != column or objective.y != row:
            continue
        features.append({
            "feature_id": f"objective:{objective.objective_id}",
            "kind": "city",
            "row": row,
            "column": column,
            "position_id": str(row * 100 + column),
            "name": f"objective {objective.objective_id}",
            "side": objective.controlling_side or "neutral",
            "value": objective.value,
        })
    return {
        "mode": "configured-hex-map" if cells else "coordinate-fallback",
        "map_id": scenario.map.map_id,
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
        "hex_size": 18.0,
        "elevation": {
            "mode": "episode" if elevations else "unavailable",
            "minimum": min(elevations) if elevations else None,
            "maximum": max(elevations) if elevations else None,
            "levels": 7,
        },
        "cells": sorted(cells, key=lambda item: (item["row"], item["column"])),
        "scenario": {
            "scenario_id": scenario.scenario_id,
            "bound_map_id": scenario.map.map_id,
            "binding_source": "native_scenario",
            "features": features,
            "initial_unit_count": len(scenario.operators),
            "dynamic_units_source": "side_observation",
            "source": {
                "name": "NativeScenario",
                "sha256": scenario.scenario_hash,
            },
        },
        "source": {
            "name": "NativeScenario.map",
            "sha256": scenario.scenario_hash,
            "coordinate_system": "native_hex_contract",
        },
    }


def controller_view_record(view: ControllerView) -> dict[str, object]:
    """Return one detached side frame plus current valid action intents."""

    frame = view.source_frame
    extra = frame.get("extra")
    native = extra.get("native") if isinstance(extra, Mapping) else None
    explicit = (
        native.get("perspective") if isinstance(native, Mapping) else None
    )
    if explicit != view.perspective:
        raise ValueError("controller Source frame perspective mismatch")
    viewer_units = [
        _viewer_unit(item, "operator")
        for item in _mapping_array(frame.get("operators"), "operators")
    ] + [
        _viewer_unit(item, "passenger")
        for item in _mapping_array(frame.get("passengers"), "passengers")
    ]
    return {
        **view.to_record(),
        "frame": {
            "frame_index": view.frame_index,
            "time": frame.get("time", {}),
            "actions": frame.get("actions", []),
            "judge_info": frame.get("judge_info", []),
            "cities": frame.get("cities", []),
            "viewer_units": viewer_units,
        },
    }


def _mapping_array(value: object, field: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"controller Source frame {field} must be an array")
    if not all(isinstance(item, Mapping) for item in value):
        raise ValueError(
            f"controller Source frame {field} items must be objects"
        )
    return tuple(value)  # type: ignore[return-value]


def _viewer_unit(value: Mapping[str, Any], category: str) -> dict[str, object]:
    current_hex = value.get("cur_hex")
    if type(current_hex) is not int or current_hex < 0:
        raise ValueError("controller unit cur_hex must be a non-negative int")
    blood = value.get("blood")
    max_blood = value.get("max_blood")
    health_ratio = (
        float(blood) / float(max_blood)
        if isinstance(blood, (int, float))
        and not isinstance(blood, bool)
        and isinstance(max_blood, (int, float))
        and not isinstance(max_blood, bool)
        and max_blood > 0
        else None
    )
    side = "red" if value.get("color") == 0 else (
        "blue" if value.get("color") == 1 else "unknown"
    )
    operator_id = value.get("obj_id")
    return {
        "operator_id": operator_id,
        "category": category,
        "side": side,
        "name": value.get("name"),
        "type_code": value.get("type"),
        "sub_type_code": value.get("sub_type"),
        "icon_key": _icon_key(value.get("type"), value.get("sub_type")),
        "current_hex": current_hex,
        "row": current_hex // 100,
        "column": current_hex % 100,
        "blood": blood,
        "max_blood": max_blood,
        "health_ratio": health_ratio,
        "alive": bool(value.get("alive", blood != 0)),
        "destroyed": value.get("alive") is False or blood == 0,
        "state": value.get("state"),
        "stopped": bool(value.get("stop", False)),
        "marching": value.get("move_state") not in {None, 0},
        "suppressed": bool(value.get("suppressed", False)),
        "move_state": value.get("move_state"),
        "direction": value.get("direction"),
        "lose_control": bool(value.get("lose_control", False)),
        "can_to_move": value.get("can_to_move"),
        "on_board": bool(value.get("on_board", False)),
        "carrier_id": value.get("car"),
        "launcher_id": value.get("launcher"),
    }


def _icon_key(raw_type: object, raw_sub_type: object) -> str:
    if type(raw_sub_type) is int:
        return {
            0: "tank", 1: "armored_vehicle", 2: "infantry",
            3: "artillery", 4: "unmanned_ground_vehicle", 5: "drone",
            6: "helicopter", 7: "loitering_munition",
            8: "helicopter", 9: "armored_vehicle",
            10: "armored_vehicle",
        }.get(raw_sub_type, "unit_fallback")
    return {
        1: "infantry", 2: "armored_vehicle",
        3: "drone", 4: "facility",
    }.get(raw_type, "unit_fallback")
