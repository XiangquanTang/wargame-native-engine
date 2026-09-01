from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from wargame_native_engine.io.serialization import json_clone

from wargame_native_engine.core.indirect_fire import (
    FireMission,
    fire_mission_sort_key,
    normalize_fire_missions,
)
from wargame_native_engine.core.boarding import (
    BoardingTransition,
    boarding_transition_sort_key,
    normalize_boarding_transitions,
)
from wargame_native_engine.core.operator import OperatorState


class Stage(str, Enum):
    DEPLOYMENT = "deployment"
    COMBAT = "combat"
    TERMINAL = "terminal"


@dataclass(slots=True)
class SideState:
    side: str
    deployment_complete: bool = False
    objective_score: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"side": self.side,
                "deployment_complete": self.deployment_complete,
                "objective_score": self.objective_score}


@dataclass(slots=True)
class ObjectiveState:
    objective_id: int
    x: float
    y: float
    radius: float = 1.0
    value: int = 1
    controlling_side: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"objective_id": self.objective_id, "x": self.x, "y": self.y,
                "radius": self.radius, "value": self.value,
                "controlling_side": self.controlling_side}


@dataclass(slots=True)
class WorldState:
    tick: int
    stage: Stage
    operators: dict[int, OperatorState]
    sides: dict[str, SideState]
    objectives: dict[int, ObjectiveState]
    terminal: bool
    winner: str | None
    terminal_reason: str | None
    truncated: bool = False
    elapsed_time_ms: int = 0
    next_action_sequence: int = 0
    next_event_sequence: int = 0
    movement_evidence: list[dict[str, Any]] = field(default_factory=list)
    last_actions: list[dict[str, Any]] = field(default_factory=list)
    last_judge_info: list[dict[str, Any]] = field(default_factory=list)
    indirect_fire_missions: tuple[FireMission, ...] = ()
    boarding_transitions: tuple[BoardingTransition, ...] = ()

    def __post_init__(self) -> None:
        self.indirect_fire_missions = normalize_fire_missions(
            self.indirect_fire_missions
        )
        self.boarding_transitions = normalize_boarding_transitions(
            self.boarding_transitions
        )

    @property
    def done(self) -> bool:
        return self.terminal or self.truncated

    def to_dict(self) -> dict[str, Any]:
        value = {
            "tick": self.tick,
            "stage": self.stage.value,
            "operators": {str(key): item.to_dict()
                          for key, item in sorted(self.operators.items())},
            "sides": {key: item.to_dict() for key, item in sorted(self.sides.items())},
            "objectives": {str(key): item.to_dict()
                           for key, item in sorted(self.objectives.items())},
            "terminal": self.terminal,
            "winner": self.winner,
            "terminal_reason": self.terminal_reason,
            "truncated": self.truncated,
        }
        if any(item.current_hex is not None for item in self.operators.values()):
            value.update({
                "elapsed_time_ms": self.elapsed_time_ms,
                "next_action_sequence": self.next_action_sequence,
                "next_event_sequence": self.next_event_sequence,
                "movement_evidence": json_clone(self.movement_evidence),
                "last_actions": json_clone(self.last_actions),
                "last_judge_info": json_clone(self.last_judge_info),
            })
        if self.indirect_fire_missions:
            value["indirect_fire_missions"] = [
                item.to_dict()
                for item in sorted(
                    self.indirect_fire_missions,
                    key=fire_mission_sort_key,
                )
            ]
        if self.boarding_transitions:
            value["boarding_transitions"] = [
                item.to_dict()
                for item in sorted(
                    self.boarding_transitions,
                    key=boarding_transition_sort_key,
                )
            ]
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorldState":
        operators = {int(key): OperatorState.from_dict(item)
                     for key, item in dict(value["operators"]).items()}
        sides = {str(key): SideState(
            side=str(item["side"]),
            deployment_complete=bool(item.get("deployment_complete", False)),
            objective_score=int(item.get("objective_score", 0)),
        ) for key, item in dict(value["sides"]).items()}
        objectives = {int(key): ObjectiveState(
            objective_id=int(item["objective_id"]), x=float(item["x"]),
            y=float(item["y"]), radius=float(item.get("radius", 1.0)),
            value=int(item.get("value", 1)),
            controlling_side=item.get("controlling_side"),
        ) for key, item in dict(value.get("objectives", {})).items()}
        return cls(int(value["tick"]), Stage(str(value["stage"])), operators,
                   sides, objectives, bool(value["terminal"]),
                   value.get("winner"), value.get("terminal_reason"),
                   bool(value.get("truncated", False)),
                   int(value.get("elapsed_time_ms", int(value["tick"]) * 1000)),
                   int(value.get("next_action_sequence", 0)),
                   int(value.get("next_event_sequence", 0)),
                   json_clone(value.get("movement_evidence", [])),
                   json_clone(value.get("last_actions", [])),
                   json_clone(value.get("last_judge_info", [])),
                   tuple(sorted(
                       (
                           FireMission.from_dict(item)
                           for item in value.get("indirect_fire_missions", [])
                       ),
                       key=fire_mission_sort_key,
                   )),
                   tuple(sorted(
                       (
                           BoardingTransition.from_dict(item)
                           for item in value.get("boarding_transitions", [])
                       ),
                       key=boarding_transition_sort_key,
                   )))
