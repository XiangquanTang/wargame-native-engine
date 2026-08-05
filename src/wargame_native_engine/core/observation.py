from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import ObjectiveState, Stage
from wargame_native_engine.io.serialization import canonical_hash, json_clone


@dataclass(frozen=True, slots=True)
class OperatorObservation:
    operator_id: int
    side: str
    operator_type: str
    subtype: str | None
    position: tuple[float, float]
    altitude: float
    heading: float
    health: float
    max_health: float
    state: str
    alive: bool
    deployed: bool
    raw_attributes: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_state(cls, value: OperatorState, *,
                   include_raw_attributes: bool = True) -> "OperatorObservation":
        return cls(value.operator_id, value.side, value.operator_type,
                   value.subtype, value.position, value.altitude, value.heading,
                   value.health, value.max_health, value.state, value.alive,
                   value.deployed, (json_clone(value.raw_attributes)
                                    if include_raw_attributes else {}))

    def to_dict(self) -> dict[str, Any]:
        return {"operator_id": self.operator_id, "side": self.side,
                "operator_type": self.operator_type, "subtype": self.subtype,
                "position": list(self.position), "altitude": self.altitude,
                "heading": self.heading, "health": self.health,
                "max_health": self.max_health, "state": self.state,
                "alive": self.alive, "deployed": self.deployed,
                "raw_attributes": json_clone(self.raw_attributes)}


@dataclass(frozen=True, slots=True)
class SideObservation:
    side: str
    tick: int
    stage: Stage
    friendly_operators: tuple[OperatorObservation, ...]
    visible_enemies: tuple[OperatorObservation, ...]
    objectives: tuple[Mapping[str, Any], ...]
    legal_action_ids: tuple[str, ...]
    mission: Mapping[str, Any] = field(default_factory=dict)

    @property
    def observation_hash(self) -> str:
        return str(self.to_dict()["observation_hash"])

    def to_dict(self) -> dict[str, Any]:
        return {"side": self.side, "tick": self.tick,
                "stage": self.stage.value,
                "friendly_operators": [item.to_dict()
                                       for item in self.friendly_operators],
                "visible_enemies": [item.to_dict() for item in self.visible_enemies],
                "objectives": json_clone(self.objectives),
                "legal_action_ids": list(self.legal_action_ids),
                "mission": json_clone(self.mission),
                "observation_hash": canonical_hash({
                    "side": self.side, "tick": self.tick,
                    "stage": self.stage.value,
                    "friendly": [item.to_dict() for item in self.friendly_operators],
                    "enemies": [item.to_dict() for item in self.visible_enemies],
                    "objectives": self.objectives,
                    "legal_action_ids": self.legal_action_ids,
                    "mission": self.mission,
                })}


def objective_view(value: ObjectiveState) -> Mapping[str, Any]:
    return json_clone(value.to_dict())
