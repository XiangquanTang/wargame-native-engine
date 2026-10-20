"""Build the stable Source-frame shell used by Native product sessions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
from wargame_native_engine.compat.source_frame.mapping import source_identity_codes
from wargame_native_engine.compat.source_frame.models import SourceFrame
from wargame_native_engine.io.scenario import NativeScenario
from wargame_native_engine.io.serialization import json_clone


def source_frame_template_for_scenario(scenario: NativeScenario) -> SourceFrame:
    """Return a detached GREEN-compatible template from canonical facts."""

    units = []
    for value in scenario.operators:
        source = value.raw_attributes.get("source_frame")
        source_codes = source_identity_codes(value.subtype)
        unit = json_clone(source) if isinstance(source, Mapping) else {
            "obj_id": value.operator_id,
            "color": 0 if value.side == "red" else 1,
            "type": (
                source_codes[0]
                if source_codes is not None
                else 2 if value.operator_type.upper() == "VEHICLE" else 1
            ),
            "sub_type": 0 if source_codes is None else source_codes[1],
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
    source_cities = scenario.metadata.get("source_cities", [])
    city_templates = (
        [json_clone(value) for value in source_cities]
        if isinstance(source_cities, Sequence)
        and not isinstance(source_cities, (str, bytes))
        and all(isinstance(value, Mapping) for value in source_cities)
        else []
    )
    cities = []
    for objective in sorted(
        scenario.objectives, key=lambda value: value.objective_id
    ):
        column, row = int(objective.x), int(objective.y)
        if objective.x != column or objective.y != row:
            continue
        coord = row * 100 + column
        matches = [
            value for value in city_templates if value.get("coord") == coord
        ]
        city = json_clone(matches[0]) if len(matches) == 1 else {
            "coord": coord,
            "name": f"objective-{objective.objective_id}",
            "type": "native_objective",
            "value": objective.value,
        }
        city["flag"] = (
            0 if objective.controlling_side == "red"
            else 1 if objective.controlling_side == "blue" else -1
        )
        extra = city.setdefault("extra", {})
        if not isinstance(extra, dict):
            extra = {}
            city["extra"] = extra
        native = extra.setdefault("native", {})
        if not isinstance(native, dict):
            native = {}
            extra["native"] = native
        native["objective_id"] = objective.objective_id
        cities.append(city)
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
        "cities": cities,
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
