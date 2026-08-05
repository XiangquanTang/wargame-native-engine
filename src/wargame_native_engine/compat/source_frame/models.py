from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from wargame_native_engine.io.serialization import json_clone


SOURCE_FRAME_SCHEMA_VERSION = "source-frame-compat/1.0"


class UnitDomain(str, Enum):
    PERSONNEL = "PERSONNEL"
    VEHICLE = "VEHICLE"
    AIR = "AIR"
    UNKNOWN = "UNKNOWN"


class ArmorClass(str, Enum):
    NONE = "NONE"
    LIGHT = "LIGHT"
    MEDIUM = "MEDIUM"
    HEAVY = "HEAVY"
    COMPOSITE = "COMPOSITE"
    UNKNOWN = "UNKNOWN"


class UnitState(str, Enum):
    STOPPED = "STOPPED"
    MOVING = "MOVING"
    MARCHING = "MARCHING"
    CONCEALED = "CONCEALED"
    SUPPRESSED = "SUPPRESSED"
    CLOSE_COMBAT = "CLOSE_COMBAT"
    TRANSITIONING = "TRANSITIONING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SourceTimeState:
    cur_step: int
    tick: int
    max_time: int
    max_step: int
    stage: int
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"cur_step": self.cur_step, "tick": self.tick,
                "max_time": self.max_time, "max_step": self.max_step,
                "stage": self.stage, "raw": json_clone(self.raw)}


@dataclass(frozen=True, slots=True)
class SourceScoreState:
    values: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return json_clone(self.values)


@dataclass(frozen=True, slots=True)
class SourceIndirectPoint:
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SourceControlPoint:
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SourceLandmarks:
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SourceUnitState:
    obj_id: int
    color: int
    type_code: int
    sub_type_code: int
    name: str
    owner: Any
    armor_code: int
    current_hex: int
    current_progress: float
    blood: int
    max_blood: int
    basic_speed: float
    speed: float
    altitude: float
    stopped: bool
    marching: bool
    suppressed: bool
    keep_code: int
    close_combat: bool
    alive: bool
    carry_weapon_ids: tuple[int, ...]
    remain_bullet_nums: Mapping[str, int]
    observe_distance: tuple[float, ...]
    passenger_ids: tuple[int, ...]
    car_id: int | None
    launcher_id: int | None
    launch_ids: tuple[int, ...]
    on_board: bool
    raw: Mapping[str, Any]
    is_passenger: bool = False

    @property
    def row_col(self) -> tuple[int, int]:
        return divmod(self.current_hex, 100)

    def to_dict(self) -> dict[str, Any]:
        return {
            "obj_id": self.obj_id, "color": self.color,
            "type_code": self.type_code, "sub_type_code": self.sub_type_code,
            "name": self.name, "owner": json_clone(self.owner),
            "armor_code": self.armor_code, "current_hex": self.current_hex,
            "current_progress": self.current_progress,
            "blood": self.blood, "max_blood": self.max_blood,
            "basic_speed": self.basic_speed, "speed": self.speed,
            "altitude": self.altitude, "stopped": self.stopped,
            "marching": self.marching, "suppressed": self.suppressed,
            "keep_code": self.keep_code,
            "close_combat": self.close_combat, "alive": self.alive,
            "carry_weapon_ids": list(self.carry_weapon_ids),
            "remain_bullet_nums": dict(self.remain_bullet_nums),
            "observe_distance": list(self.observe_distance),
            "passenger_ids": list(self.passenger_ids), "car_id": self.car_id,
            "launcher_id": self.launcher_id, "launch_ids": list(self.launch_ids),
            "on_board": self.on_board, "is_passenger": self.is_passenger,
            "raw": json_clone(self.raw),
        }


@dataclass(frozen=True, slots=True)
class SourceFrame:
    source_hash: str
    operators: tuple[SourceUnitState, ...]
    passengers: tuple[SourceUnitState, ...]
    time: SourceTimeState
    jm_points: tuple[SourceIndirectPoint, ...]
    cities: tuple[SourceControlPoint, ...]
    scores: SourceScoreState
    landmarks: SourceLandmarks
    terrain_id: int | str
    judge_info: Any
    role_and_grouping_info: Any
    communication: Any
    scenario_id: str
    valid_actions: Mapping[str, Any]
    actions: tuple[Any, ...]
    raw: Mapping[str, Any]
    warnings: tuple[str, ...] = ()
    schema_version: str = SOURCE_FRAME_SCHEMA_VERSION

    @property
    def all_units(self) -> tuple[SourceUnitState, ...]:
        return self.operators + self.passengers

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "source_hash": self.source_hash,
            "operators": [item.to_dict() for item in self.operators],
            "passengers": [item.to_dict() for item in self.passengers],
            "time": self.time.to_dict(),
            "jm_points": [json_clone(item.raw) for item in self.jm_points],
            "cities": [json_clone(item.raw) for item in self.cities],
            "scores": self.scores.to_dict(), "landmarks": json_clone(self.landmarks.raw),
            "terrain_id": self.terrain_id, "judge_info": json_clone(self.judge_info),
            "role_and_grouping_info": json_clone(self.role_and_grouping_info),
            "communication": json_clone(self.communication),
            "scenario_id": self.scenario_id,
            "valid_actions": json_clone(self.valid_actions),
            "actions": json_clone(self.actions), "warnings": list(self.warnings),
            "raw": json_clone(self.raw),
        }
