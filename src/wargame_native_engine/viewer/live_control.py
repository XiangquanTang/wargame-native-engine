"""Presentation-only records for the local side-control page."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from wargame_native_engine.gateway import ControllerView
from wargame_native_engine.io.scenario import NativeScenario
from wargame_native_engine.viewer.terrain_display import terrain_display_semantic


def native_scenario_map_record(scenario: NativeScenario) -> dict[str, object]:
    """Project immutable Native map facts into the existing map renderer."""

    cells = []
    roadblocks = frozenset(scenario.map.roadblocks)
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
            "terrain_semantic": terrain_display_semantic(
                cell.terrain, allow_native_strings=True,
            ),
            # Preserve the legacy numeric payload; retain a raw value only
            # when terrain_code does not already represent it.
            **({"terrain_source": cell.terrain}
               if terrain_code is None and cell.terrain is not None else {}),
            # A static roadblock is an overlay, not a replacement for forest,
            # settlement or any other underlying terrain fact.
            **({"roadblock": True} if cell.hex_id in roadblocks else {}),
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
        "elevation_kind": scenario.map.elevation_kind,
        "elevation_unit_m": scenario.map.elevation_unit_m,
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
    own_lifecycles = _own_lifecycle_index(view)
    source_units = tuple(
        (item, "operator")
        for item in _mapping_array(frame.get("operators"), "operators")
    ) + tuple(
        (item, "passenger")
        for item in _mapping_array(frame.get("passengers"), "passengers")
    )
    viewer_units = [
        _viewer_unit(
            item,
            category,
            view.perspective,
            own_lifecycles.get(item.get("obj_id")),
        )
        for item, category in source_units
    ]
    _attach_own_transport_details(
        viewer_units,
        source_units=source_units,
        own_lifecycles=own_lifecycles,
    )
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


def _viewer_unit(
    value: Mapping[str, Any],
    category: str,
    perspective: str,
    own_lifecycle: object | None = None,
) -> dict[str, object]:
    source_current_hex = value.get("cur_hex")
    if type(source_current_hex) is not int or source_current_hex < 0:
        raise ValueError("controller unit cur_hex must be a non-negative int")
    current_hex: int | None = source_current_hex
    map_visible = True
    spatially_present = True
    spatial_status = "ON_MAP"
    boarding_role = None
    boarding_state = None
    if own_lifecycle is not None:
        deployment = own_lifecycle.deployment
        boarding = own_lifecycle.boarding
        spatially_present = deployment.spatially_present
        spatial_status = deployment.spatial_status.value
        boarding_role = boarding.role.value
        boarding_state = boarding.state.value
        # Native BOARD establishes the relationship immediately, but the
        # product keeps the passenger at its submitted position until the
        # 75-second transition has completed.  Once complete (and throughout
        # UNLOAD), the passenger has no independent map/hit-test presence.
        map_visible = not (
            boarding_role == "PASSENGER"
            and boarding_state in {"ON_BOARD", "UNLOADING"}
        )
        if map_visible:
            current_hex = (
                deployment.current_hex
                if deployment.current_hex is not None
                else source_current_hex
            )
        else:
            current_hex = None
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
    movement_target_hex = None
    movement_progress = 0.0
    # A friendly unit's submitted route is already known to its own seat.
    # Do not expose the next cell of an opponent route merely to animate it.
    if side == perspective.lower() and map_visible:
        move_path = value.get("move_path")
        if (
            isinstance(move_path, Sequence)
            and not isinstance(move_path, (str, bytes))
            and move_path
            and type(move_path[0]) is int
            and move_path[0] >= 0
        ):
            movement_target_hex = move_path[0]
            raw_progress = value.get("cur_pos", 0.0)
            if isinstance(raw_progress, (int, float)) and not isinstance(
                raw_progress, bool
            ):
                movement_progress = max(0.0, min(1.0, float(raw_progress)))
    extra = value.get("extra")
    native = extra.get("native", {}) if isinstance(extra, Mapping) else {}
    native = native if isinstance(native, Mapping) else {}
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
        "row": None if current_hex is None else current_hex // 100,
        "column": None if current_hex is None else current_hex % 100,
        "map_visible": map_visible,
        "spatially_present": spatially_present,
        "spatial_status": spatial_status,
        "movement_target_hex": movement_target_hex,
        "movement_progress": movement_progress,
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
        "lose_control": (native["command_lost"]
                         if type(native.get("command_lost")) is bool
                         else bool(value.get("lose_control", False))),
        "can_to_move": value.get("can_to_move"),
        "on_board": (
            bool(value.get("on_board", False))
            if own_lifecycle is None
            else own_lifecycle.deployment.on_board
        ),
        "boarding_role": boarding_role,
        "boarding_state": boarding_state,
        "carrier_id": (
            value.get("car")
            if own_lifecycle is None or boarding_role == "PASSENGER"
            else None
        ),
        "launcher_id": value.get("launcher"),
    }


def _own_lifecycle_index(view: ControllerView) -> dict[int, object]:
    """Index only frame-bound OWN lifecycle payloads; never inspect GREEN."""

    decision = view.decision_semantic
    if decision is None:
        # Legacy/replay ControllerViews predate the machine Decision core.
        # Their historical Source projection stays byte-compatible.
        return {}
    values: dict[int, object] = {}
    for lifecycle in decision.own_operators:
        operator_id = lifecycle.operator_reference
        if operator_id in values:
            raise ValueError("duplicate OWN lifecycle operator reference")
        if lifecycle.identity.side.lower() != view.perspective.lower():
            raise ValueError("OWN lifecycle perspective mismatch")
        values[operator_id] = lifecycle
    return values


def _attach_own_transport_details(
    viewer_units: list[dict[str, object]],
    *,
    source_units: tuple[tuple[Mapping[str, Any], str], ...],
    own_lifecycles: Mapping[int, object],
) -> None:
    """Attach same-side completed occupancy without exposing enemy links."""

    projected = {item["operator_id"]: item for item in viewer_units}
    source_by_id = {item.get("obj_id"): item for item, _category in source_units}
    for operator_id, lifecycle in sorted(own_lifecycles.items()):
        if lifecycle.boarding.role.value != "CARRIER":
            continue
        if lifecycle.boarding.state.value not in {"CARRYING", "UNLOADING"}:
            continue
        carrier = projected.get(operator_id)
        source = source_by_id.get(operator_id)
        if carrier is None or source is None:
            raise ValueError("OWN carrier is absent from controller Source frame")
        raw_ids = source.get("passenger_ids", [])
        if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, (str, bytes)):
            raise ValueError("OWN carrier passenger_ids must be an array")
        passenger_ids = tuple(sorted(
            value for value in raw_ids if type(value) is int and value >= 0
        ))
        if len(passenger_ids) != 1 or len(passenger_ids) != len(raw_ids):
            raise ValueError("OWN carrying relation must contain one passenger")
        passenger_id = passenger_ids[0]
        passenger = projected.get(passenger_id)
        passenger_lifecycle = own_lifecycles.get(passenger_id)
        if passenger is None or passenger_lifecycle is None:
            raise ValueError("OWN passenger is absent from the bound side frame")
        if passenger_lifecycle.boarding.role.value != "PASSENGER":
            raise ValueError("OWN transport relationship is not reciprocal")
        carrier["transport_display_count"] = 2
        carrier["carried_units"] = [{
            "operator_id": passenger_id,
            "name": passenger.get("name"),
            "icon_key": passenger.get("icon_key"),
            "alive": passenger.get("alive"),
            "destroyed": passenger.get("destroyed"),
            "boarding_state": passenger.get("boarding_state"),
        }]


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
