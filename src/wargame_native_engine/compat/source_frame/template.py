"""Build the stable Source-frame shell used by Native product sessions."""

from __future__ import annotations

from collections.abc import Mapping

from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
from wargame_native_engine.compat.source_frame.models import SourceFrame
from wargame_native_engine.io.scenario import NativeScenario
from wargame_native_engine.io.serialization import json_clone


def source_frame_template_for_scenario(scenario: NativeScenario) -> SourceFrame:
    """Return a detached GREEN-compatible template from canonical facts."""

    units = []
    for value in scenario.operators:
        source = value.raw_attributes.get("source_frame")
        unit = json_clone(source) if isinstance(source, Mapping) else {
            "obj_id": value.operator_id,
            "color": 0 if value.side == "red" else 1,
            "type": 2 if value.operator_type.upper() == "VEHICLE" else 1,
            "sub_type": 0,
            "name": f"fixture-{value.operator_id}",
            "owner": value.side,
            "armor": 0,
            "cur_hex": value.current_hex,
            "cur_pos": 0,
            "basic_speed": value.basic_speed,
            "speed": 0,
            "move_state": 0,
            "stop": 1,
            "move_path": [],
            "move_to_stop_remain_time": 0,
            "blood": value.blood,
            "max_blood": value.max_blood,
            "carry_weapon_ids": list(value.weapon_ids),
            "remain_bullet_nums": {"0": 20},
            "observe_distance": [],
            "passenger_ids": [],
            "car": None,
            "launcher": None,
            "launch_ids": [],
            "on_board": 0,
            "close_combat": 0,
            "altitude": value.altitude,
            "weapon_unfold_state": 1,
            "weapon_cool_time": 0,
        }
        # Dynamic Native facts override the detached Source template while
        # identity, display/type metadata and ammunition remain source-backed.
        unit.update({
            "obj_id": value.operator_id,
            "color": 0 if value.side == "red" else 1,
            "cur_hex": value.current_hex,
            "cur_pos": value.cur_pos,
            "blood": value.blood,
            "max_blood": value.max_blood,
            "basic_speed": value.basic_speed,
            "speed": value.speed,
            "move_path": list(value.move_path),
            "move_state": 1 if value.movement_mode == "MARCHING" else 0,
            "stop": int(value.stopped),
            "alive": value.alive,
            "carry_weapon_ids": list(value.weapon_ids),
            "weapon_unfold_state": int(value.weapon_unfolded),
            "weapon_cool_time": value.weapon_cooldown_remaining_ms / 1000.0,
        })
        if "remain_bullet_nums" not in unit:
            ammunition = value.raw_attributes.get("ammunition", {})
            unit["remain_bullet_nums"] = (
                json_clone(ammunition) if isinstance(ammunition, Mapping) else {}
            )
        units.append(unit)
    document = {
        "operators": units,
        "passengers": [],
        "time": {
            "cur_step": 0,
            "tick": 1,
            "max_time": scenario.terminal_rules.max_ticks,
            "max_step": scenario.terminal_rules.max_ticks,
            "stage": 2,
        },
        "jm_points": [],
        "cities": [],
        "scores": {},
        "landmarks": {},
        "terrain_id": scenario.map.map_id,
        "judge_info": [],
        "role_and_grouping_info": {
            "red": {"side": "red"},
            "blue": {"side": "blue"},
        },
        "communication": [],
        "scenario_id": scenario.scenario_id,
        "valid_actions": {},
        "actions": [],
    }
    return SourceFrameImporter().from_dict(
        document,
        source_hash="synthetic-template",
    )
