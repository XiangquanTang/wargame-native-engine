from __future__ import annotations

from typing import Any, Mapping

from wargame_native_engine.compat.source_frame.models import SourceFrame
from wargame_native_engine.compat.source_frame.contract import (
    CURRENT_SOURCE_CONTRACT, LEGACY_SOURCE_CONTRACT, SOURCE_CONTRACT_KEY,
    require_source_contract,
)
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.io.serialization import json_clone


class SourceFrameExporter:
    """Export a compatible semantic frame while preserving source-only fields."""

    def export(self, world: WorldState, *, template: SourceFrame,
               actions: list[Mapping[str, Any]] | None = None,
               valid_actions: Mapping[str, Any] | None = None,
               adjudication: Mapping[str, Any] | None = None,
               project_objective_control: bool = True,
               source_contract: str = LEGACY_SOURCE_CONTRACT,
               weapon_inventory: tuple[Mapping[str, Any], ...] = ()) -> dict[str, Any]:
        if type(project_objective_control) is not bool:
            raise TypeError("project_objective_control must be an exact bool")
        require_source_contract(source_contract)
        dynamic = source_contract == CURRENT_SOURCE_CONTRACT
        inventories = {value["operator_id"]: value for value in weapon_inventory}
        root = json_clone(template.raw)
        source_units = {item.obj_id: item for item in template.all_units}
        lifecycle_projection = any(
            state.lifecycle_profile_id is not None
            for state in world.operators.values()
        )
        operators: list[dict[str, Any]] = []
        passengers: list[dict[str, Any]] = []
        for operator_id, state in sorted(world.operators.items()):
            source = source_units.get(operator_id)
            raw = (json_clone(source.raw) if source is not None else
                   json_clone(state.raw_attributes.get("source_frame", {})))
            previous_extra = raw.get("extra") if dynamic else None
            previous_native = (previous_extra.get("native")
                               if isinstance(previous_extra, Mapping) else None)
            previous_ammo = (previous_native.get("ammunition_projection", {})
                             if isinstance(previous_native, Mapping) else {})
            raw.update({
                "obj_id": operator_id,
                "color": 0 if state.side == "red" else 1 if state.side == "blue"
                         else raw.get("color"),
                "cur_hex": (state.current_hex if state.current_hex is not None else
                            int(round(state.y)) * 100 + int(round(state.x))),
                "cur_pos": state.cur_pos,
                "blood": int(state.blood), "max_blood": int(state.max_blood),
                "basic_speed": state.basic_speed,
                "speed": state.speed, "altitude": state.altitude,
                "move_path": list(state.move_path),
                "move_state": 1 if state.movement_mode == "MARCHING" else 0,
                "stop": int(state.stopped),
                "move_to_stop_remain_time": (
                    state.move_to_stop_remaining_ms / 1000.0),
                "close_combat": int(state.state == "CLOSE_COMBAT"),
                "carry_weapon_ids": list(state.weapon_ids),
                "weapon_cool_time": state.weapon_cooldown_remaining_ms / 1000.0,
                "weapon_unfold_state": int(state.weapon_unfolded),
                "see_enemy_bop_ids": list(state.raw_attributes.get(
                    "see_enemy_bop_ids", [])),
            })
            raw["alive"] = state.alive
            if lifecycle_projection and not dynamic:
                # Ammunition projection remains version-gated by lifecycle
                # authority; alive/blood above are always authoritative dynamic
                # fields rather than values retained from the input template.
                raw["remain_bullet_nums"] = json_clone(
                    state.raw_attributes.get("ammunition", {})
                )
            if state.passenger_id is not None:
                raw["passenger_ids"] = [state.passenger_id]
            if state.mounted_on is not None:
                raw["car"] = state.mounted_on
                raw["on_board"] = True
                raw["is_passenger"] = True
            extra = raw.setdefault("extra", {})
            if not isinstance(extra, dict):
                extra = {}
                raw["extra"] = extra
            extra["native"] = {
                "movement_mode": state.movement_mode,
                "fatigue_level": state.fatigue_level,
                "movement_blocked_reason": state.movement_blocked_reason,
                "movement_action_id": state.movement_action_id,
                "transition_kind": state.transition_kind,
                "transition_remaining_ms": state.transition_remaining_ms,
                "cur_pos_millionths": state.cur_pos_millionths,
            }
            if dynamic:
                raw["passenger_ids"] = ([] if state.passenger_id is None
                                        else [state.passenger_id])
                raw["car"] = state.mounted_on
                raw["on_board"] = state.mounted_on is not None
                raw["is_passenger"] = state.mounted_on is not None
                inventory = inventories.get(operator_id)
                pools = {} if inventory is None else inventory["ammunition_pools"]
                bound_keys = set() if inventory is None else {
                    item["ammunition_key"] for item in inventory["weapons"]
                    if item["resource_policy"] == "AMMUNITION_KEY"
                    and isinstance(item["ammunition_key"], str)
                }
                original = json_clone(
                    previous_ammo["source_input"]
                    if isinstance(previous_ammo, Mapping) and previous_ammo.get(
                        "source_input_semantics") == "INITIAL_INPUT_NOT_CURRENT_STOCK"
                    else raw.get("remain_bullet_nums", {})
                )
                # Source slots and weapon IDs are distinct namespaces. Only
                # exact pool keys bound by the existing authority are projected.
                slots = original if isinstance(original, Mapping) else {}
                unknown = sorted(key for key in slots
                                 if key not in bound_keys or key not in pools)
                missing_pools = sorted(bound_keys - pools.keys())
                unrepresented = sorted(bound_keys - slots.keys())
                complete = inventory is not None and not (
                    unknown or missing_pools or unrepresented)
                raw["remain_bullet_nums"] = {
                    key: pools[key] for key in sorted(slots)
                    if key in bound_keys and key in pools
                }
                if not complete:
                    # Absence is UNKNOWN to legacy field consumers; an empty
                    # dict would falsely report known zero ammunition.
                    raw.pop("remain_bullet_nums", None)
                extra["native"].update({
                    "weapon_inventory": json_clone(inventory),
                    "ammunition_projection": {
                        "status": "CURRENT" if complete else "UNKNOWN",
                        "missing_pools": missing_pools,
                        "unrepresented_pools": unrepresented,
                        "unmapped_source_slots": unknown,
                        "source_input": original,
                        "source_input_semantics": "INITIAL_INPUT_NOT_CURRENT_STOCK",
                    },
                    "mounted_on": state.mounted_on,
                    "passenger_id": state.passenger_id,
                })
            if (
                state.boarding_profile_id is not None
                or state.mounted_on is not None
                or state.passenger_id is not None
                or state.movement_locked
            ):
                extra["native"].update({
                    "boarding_profile_id": state.boarding_profile_id,
                    "mounted_on": state.mounted_on,
                    "passenger_id": state.passenger_id,
                    "movement_locked": state.movement_locked,
                })
            if state.lifecycle_profile_id is not None:
                extra["native"].update({
                    "lifecycle_profile_id": state.lifecycle_profile_id,
                    "lifecycle_remaining_ms": state.lifecycle_remaining_ms,
                })
            if state.mounted_on is not None or (
                not dynamic and source is not None and source.is_passenger
            ):
                passengers.append(raw)
            else:
                operators.append(raw)
        cities: list[dict[str, Any]] = []
        objectives_by_id = world.objectives
        for index, source_city in enumerate(template.cities):
            raw = json_clone(source_city.raw)
            native = raw.get("extra", {}).get("native", {}) if isinstance(
                raw.get("extra"), Mapping
            ) else {}
            objective_id = native.get("objective_id") if isinstance(
                native, Mapping
            ) else None
            objective = (
                objectives_by_id.get(objective_id)
                if type(objective_id) is int
                else None
            )
            if objective is None and index < len(objectives_by_id):
                candidate = sorted(
                    objectives_by_id.values(),
                    key=lambda value: value.objective_id,
                )[index]
                column, row = int(candidate.x), int(candidate.y)
                if (
                    candidate.x == column
                    and candidate.y == row
                    and raw.get("coord") == row * 100 + column
                ):
                    objective = candidate
            if project_objective_control and objective is not None:
                raw["flag"] = (
                    0 if objective.controlling_side == "red"
                    else 1 if objective.controlling_side == "blue" else -1
                )
                raw["value"] = objective.value
            cities.append(raw)
        root.update({
            "operators": operators, "passengers": passengers,
            "time": {**json_clone(template.time.raw), "cur_step": world.tick,
                     "stage": 2 if world.stage.value == "combat" else
                              1 if world.stage.value == "deployment" else 3},
            "jm_points": [json_clone(item.raw) for item in template.jm_points],
            "cities": cities,
            "scores": template.scores.to_dict(),
            "landmarks": json_clone(template.landmarks.raw),
            "terrain_id": template.terrain_id,
            "role_and_grouping_info": json_clone(template.role_and_grouping_info),
            "communication": json_clone(template.communication),
            "scenario_id": template.scenario_id,
            "valid_actions": json_clone(valid_actions or {}),
            "actions": json_clone(actions or []),
            "judge_info": ([] if adjudication is None else [{
                "source": "NATIVE_TABLE_ADJUDICATION",
                "schema_version": "native-judge-info/1.0",
                "adjudication_id": None if adjudication is None
                                     else adjudication.get("adjudication_id"),
                "result": json_clone(adjudication or {}),
            }]),
        })
        if dynamic:
            extra = root.get("extra")
            if not isinstance(extra, dict):
                extra = {}
                root["extra"] = extra
            native = extra.get("native")
            if not isinstance(native, dict):
                native = {}
                extra["native"] = native
            native[SOURCE_CONTRACT_KEY] = source_contract
        return root
