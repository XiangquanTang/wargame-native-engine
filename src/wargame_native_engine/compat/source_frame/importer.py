from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.compat.source_frame.mapping import SourceMappings
from wargame_native_engine.compat.source_frame.models import (
    SourceControlPoint, SourceFrame, SourceIndirectPoint, SourceLandmarks,
    SourceScoreState, SourceTimeState, SourceUnitState,
)
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import SideState, Stage, WorldState
from wargame_native_engine.io.serialization import file_sha256, json_clone


REQUIRED_TOP_LEVEL = {
    "operators", "passengers", "time", "jm_points", "cities", "scores",
    "landmarks", "terrain_id", "judge_info", "role_and_grouping_info",
    "communication", "scenario_id", "valid_actions", "actions",
}


class SourceFrameImporter:
    """Read a documented standard frame without treating it as rule truth."""

    importer_version = "source-frame-importer/1.0"

    def __init__(self, mappings: SourceMappings | None = None) -> None:
        self.mappings = mappings or SourceMappings()

    def load(self, path: str | Path) -> SourceFrame:
        source = Path(path)
        before_hash = file_sha256(source)
        value = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("source frame root must be an object")
        missing = sorted(REQUIRED_TOP_LEVEL - set(value))
        if missing:
            raise ValueError(f"source frame missing required fields: {missing}")
        frame = self.from_dict(value, source_hash=before_hash)
        if file_sha256(source) != before_hash:
            raise RuntimeError("source frame changed during import")
        return frame

    def from_dict(self, value: Mapping[str, Any], *, source_hash: str) -> SourceFrame:
        time = _mapping(value["time"], "time")
        warnings: list[str] = []
        operators = tuple(self._unit(item, False, warnings)
                          for item in _sequence(value["operators"], "operators"))
        passengers = tuple(self._unit(item, True, warnings)
                           for item in _sequence(value["passengers"], "passengers"))
        ids = [item.obj_id for item in operators + passengers]
        if len(ids) != len(set(ids)):
            raise ValueError("source unit obj_id values must be unique")
        known = REQUIRED_TOP_LEVEL
        raw_top = {key: json_clone(item) for key, item in value.items()
                   if key not in known}
        return SourceFrame(
            source_hash=source_hash, operators=operators, passengers=passengers,
            time=SourceTimeState(int(time["cur_step"]), int(time["tick"]),
                                 _optional_time_int(time, "max_time"),
                                 _optional_time_int(time, "max_step"),
                                 _optional_time_int(time, "stage"),
                                 json_clone(time)),
            jm_points=tuple(SourceIndirectPoint(json_clone(_mapping(item, "jm_point")))
                            for item in _sequence(value["jm_points"], "jm_points")),
            cities=tuple(SourceControlPoint(json_clone(_mapping(item, "city")))
                         for item in _sequence(value["cities"], "cities")),
            scores=SourceScoreState(json_clone(_mapping(value["scores"], "scores"))),
            landmarks=SourceLandmarks(json_clone(_mapping(value["landmarks"], "landmarks"))),
            terrain_id=value["terrain_id"], judge_info=json_clone(value["judge_info"]),
            role_and_grouping_info=json_clone(value["role_and_grouping_info"]),
            communication=json_clone(value["communication"]),
            scenario_id=str(value["scenario_id"]),
            valid_actions=json_clone(_mapping(value["valid_actions"], "valid_actions")),
            actions=tuple(json_clone(_sequence(value["actions"], "actions"))),
            raw=raw_top, warnings=tuple(sorted(set(warnings))),
        )

    def to_world(self, frame: SourceFrame) -> tuple[WorldState, Mapping[str, Any]]:
        operators: dict[int, OperatorState] = {}
        records: list[dict[str, Any]] = []
        for source in sorted(frame.all_units, key=lambda item: item.obj_id):
            side = self.mappings.side(source.color)
            domain = self.mappings.domain(source.type_code)
            armor = self.mappings.armor(source.armor_code)
            row, col = source.row_col
            unit_state = self.mappings.unit_state(source)
            health = 100.0 * source.blood / source.max_blood
            raw = {
                "source_frame": json_clone(source.raw),
                "source_value": {"cur_hex": source.current_hex,
                                 "blood": source.blood,
                                 "max_blood": source.max_blood},
                "normalized_value": {"row": row, "column": col,
                                     "domain": domain.normalized_value,
                                     "armor_class": armor.normalized_value},
                "native_effective_value": {"x": float(col), "y": float(row),
                                           "health": health,
                                           "health_status": "DERIVED_FROM_BLOOD"},
                "source_is_passenger": source.is_passenger,
            }
            operators[source.obj_id] = OperatorState(
                operator_id=source.obj_id, side=str(side.native_effective_value),
                operator_type=str(domain.native_effective_value),
                subtype=f"source-subtype-{source.sub_type_code}",
                x=float(col), y=float(row), altitude=source.altitude, heading=0.0,
                health=health, max_health=100.0, speed=source.speed,
                max_speed=max(source.basic_speed, source.speed), move_range=0.0,
                sensor_range=0.0, state=unit_state.value,
                alive=source.alive, weapon_ids=list(source.carry_weapon_ids),
                raw_attributes=raw, deployed=not source.on_board,
                blood=source.blood, max_blood=source.max_blood,
                basic_speed=source.basic_speed,
                current_hex=source.current_hex,
                cur_pos_millionths=int(round(source.current_progress * 1_000_000)),
                move_path=[int(item) for item in source.raw.get("move_path", [])],
                movement_mode="MARCHING" if source.marching else "NORMAL",
                stopped=source.stopped,
                move_to_stop_remaining_ms=int(round(float(
                    source.raw.get("move_to_stop_remain_time", 0)) * 1000)),
                weapon_unfolded=bool(source.raw.get("weapon_unfold_state", 1)),
                weapon_cooldown_remaining_ms=int(round(float(
                    source.raw.get("weapon_cool_time", 0)) * 1000)),
            )
            records.append({
                "operator_id": source.obj_id,
                "side": side.to_dict(), "domain": domain.to_dict(),
                "armor": armor.to_dict(),
                "weapons": [self.mappings.weapon(weapon_id).to_dict()
                            for weapon_id in source.carry_weapon_ids],
                "position": {"source_value": source.current_hex,
                             "normalized_value": [row, col],
                             "native_effective_value": [float(col), float(row)],
                             "status": "NORMALIZED",
                             "source": "row_times_100_plus_col"},
                "health": {"source_value": source.blood,
                           "normalized_value": [source.blood, source.max_blood],
                           "native_effective_value": health, "status": "DERIVED",
                           "source": "blood/max_blood percentage"},
            })
        sides = {item.side: SideState(item.side, True, 0)
                 for item in operators.values()}
        world = WorldState(frame.time.cur_step, Stage.COMBAT, operators, sides, {},
                           False, None, None, False)
        report = {
            "schema_version": "source-frame-mapping-report/1.0",
            "source_hash": frame.source_hash,
            "importer_version": self.importer_version,
            "scenario_id": frame.scenario_id, "terrain_id": frame.terrain_id,
            "operator_count": len(frame.operators),
            "passenger_count": len(frame.passengers),
            "valid_actions_rule_truth": False,
            "position_encoding": "row_times_100_plus_col",
            "unresolved_source_semantics": [
                "armor numeric code to ArmorClass (U01)",
                "complete type/sub_type enum (U01)",
                "ammunition slots 0/100/101/102 (U01)",
                "keep/keep_remain_time state semantics (U01)",
                "observe_distance target-type indices (U01)"
            ],
            "records": records, "warnings": list(frame.warnings),
        }
        return world, report

    @staticmethod
    def _unit(value: Any, passenger: bool,
              warnings: list[str]) -> SourceUnitState:
        item = _mapping(value, "unit")
        required = {"obj_id", "color", "type", "sub_type", "name", "armor",
                    "cur_hex", "blood", "max_blood"}
        missing = sorted(required - set(item))
        if missing:
            raise ValueError(f"source unit missing required fields: {missing}")
        blood, max_blood = int(item["blood"]), int(item["max_blood"])
        if max_blood <= 0 or not 0 <= blood <= max_blood:
            raise ValueError(f"invalid blood for source unit {item['obj_id']}")
        if not item.get("carry_weapon_ids"):
            warnings.append(f"unit {item['obj_id']} has no weapon ids")
        bullets = _mapping(item.get("remain_bullet_nums", {}), "remain_bullet_nums")
        observe_distance = item.get("observe_distance", [])
        if isinstance(observe_distance, Mapping):
            # Legacy dictionary indices are undocumented. Preserve the source
            # in raw, but do not misrepresent its keys as normalized distances.
            warnings.append(
                f"unit {item['obj_id']} observe_distance mapping has unsupported "
                "indices; preserved raw, normalized distances unknown")
            normalized_observe_distance = ()
        else:
            normalized_observe_distance = tuple(float(value) for value in observe_distance)
        return SourceUnitState(
            obj_id=int(item["obj_id"]), color=int(item["color"]),
            type_code=int(item["type"]), sub_type_code=int(item["sub_type"]),
            name=str(item["name"]), owner=json_clone(item.get("owner")),
            armor_code=int(item["armor"]), current_hex=int(item["cur_hex"]),
            current_progress=float(item.get("cur_pos", 0.0)),
            blood=blood, max_blood=max_blood,
            basic_speed=float(item.get("basic_speed", 0.0)),
            speed=float(item.get("speed", 0.0)), altitude=float(item.get("altitude", 0.0)),
            stopped=bool(item.get("stop", 0)), marching=int(item.get("move_state", 0)) == 1,
            suppressed=False, keep_code=int(item.get("keep", 0)),
            close_combat=bool(item.get("close_combat", 0)), alive=blood > 0,
            carry_weapon_ids=tuple(int(value) for value in item.get("carry_weapon_ids", [])),
            remain_bullet_nums={str(key): int(value) for key, value in bullets.items()},
            observe_distance=normalized_observe_distance,
            passenger_ids=tuple(int(value) for value in item.get("passenger_ids", [])),
            car_id=_optional_int(item.get("car")), launcher_id=_optional_int(item.get("launcher")),
            launch_ids=tuple(int(value) for value in item.get("launch_ids", [])),
            on_board=bool(item.get("on_board", 0)), raw=json_clone(item),
            is_passenger=passenger,
        )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_time_int(time: Mapping[str, Any], field_name: str) -> int | None:
    if field_name not in time:
        return None
    return int(time[field_name])


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _sequence(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value
