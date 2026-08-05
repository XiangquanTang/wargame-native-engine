from __future__ import annotations

from typing import Any, Mapping

from wargame_native_engine.compat.source_frame.models import SourceFrame
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.io.serialization import json_clone


class SourceFrameExporter:
    """Export a compatible semantic frame while preserving source-only fields."""

    def export(self, world: WorldState, *, template: SourceFrame,
               actions: list[Mapping[str, Any]] | None = None,
               valid_actions: Mapping[str, Any] | None = None,
               adjudication: Mapping[str, Any] | None = None) -> dict[str, Any]:
        root = json_clone(template.raw)
        source_units = {item.obj_id: item for item in template.all_units}
        operators: list[dict[str, Any]] = []
        passengers: list[dict[str, Any]] = []
        for operator_id, state in sorted(world.operators.items()):
            source = source_units.get(operator_id)
            raw = (json_clone(source.raw) if source is not None else
                   json_clone(state.raw_attributes.get("source_frame", {})))
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
            if source is not None and source.is_passenger:
                passengers.append(raw)
            else:
                operators.append(raw)
        root.update({
            "operators": operators, "passengers": passengers,
            "time": {**json_clone(template.time.raw), "cur_step": world.tick,
                     "stage": 2 if world.stage.value == "combat" else
                              1 if world.stage.value == "deployment" else 3},
            "jm_points": [json_clone(item.raw) for item in template.jm_points],
            "cities": [json_clone(item.raw) for item in template.cities],
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
        return root
