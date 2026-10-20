from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from wargame_native_engine.io.serialization import canonical_hash, json_clone


class ActionType(str, Enum):
    DEPLOY = "DEPLOY"
    END_DEPLOYMENT = "END_DEPLOYMENT"
    MOVE = "MOVE"
    CHANGE_STATE = "CHANGE_STATE"
    ATTACK = "ATTACK"
    SHOOT = "SHOOT"
    FIRE = "FIRE"
    STOP_MOVE = "STOP_MOVE"
    BOARD = "BOARD"
    UNLOAD = "UNLOAD"
    CAPTURE = "CAPTURE"
    ENTER_CONCEALMENT = "ENTER_CONCEALMENT"
    WEAPON_LOCK = "WEAPON_LOCK"
    WEAPON_UNFOLD = "WEAPON_UNFOLD"
    WAIT = "WAIT"


@dataclass(frozen=True, slots=True)
class EngineAction:
    side: str
    actor_id: int | None
    action_type: ActionType
    target_id: int | None = None
    target_position: tuple[float, float] | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)

    @property
    def action_id(self) -> str:
        return "action-" + canonical_hash(self.to_dict())[:24]

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side, "actor_id": self.actor_id,
            "action_type": self.action_type.value, "target_id": self.target_id,
            "target_position": (None if self.target_position is None
                                else list(self.target_position)),
            "parameters": json_clone(self.parameters),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EngineAction":
        position = value.get("target_position")
        return cls(
            side=str(value["side"]),
            actor_id=None if value.get("actor_id") is None else int(value["actor_id"]),
            action_type=ActionType(str(value["action_type"]).upper()),
            target_id=None if value.get("target_id") is None else int(value["target_id"]),
            target_position=(None if position is None
                             else (float(position[0]), float(position[1]))),
            parameters=json_clone(value.get("parameters", {})),
        )


@dataclass(frozen=True, slots=True)
class ValidAction:
    action: EngineAction
    priority: int
    source: str = "native_engine.valid_actions"

    @property
    def action_id(self) -> str:
        return self.action.action_id

    def to_dict(self) -> dict[str, Any]:
        return {"action_id": self.action_id, "priority": self.priority,
                "source": self.source, "action": self.action.to_dict()}


@dataclass(frozen=True, slots=True)
class ActionDecision:
    action_id: str
    accepted: bool
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"action_id": self.action_id, "accepted": self.accepted,
                "code": self.code, "message": self.message}


ACTION_PRIORITY = {
    ActionType.DEPLOY: 0,
    ActionType.END_DEPLOYMENT: 1,
    ActionType.ATTACK: 0,
    ActionType.SHOOT: 0,
    ActionType.FIRE: 0,
    ActionType.STOP_MOVE: 1,
    ActionType.BOARD: 2,
    ActionType.UNLOAD: 2,
    ActionType.CAPTURE: 2,
    ActionType.ENTER_CONCEALMENT: 2,
    ActionType.WEAPON_LOCK: 2,
    ActionType.WEAPON_UNFOLD: 2,
    ActionType.MOVE: 1,
    ActionType.CHANGE_STATE: 2,
    ActionType.WAIT: 99,
}


def valid_action_sort_key(value: ValidAction) -> tuple[Any, ...]:
    action = value.action
    return (value.priority, action.action_type.value,
            -1 if action.actor_id is None else action.actor_id,
            -1 if action.target_id is None else action.target_id,
            () if action.target_position is None else action.target_position,
            value.action_id)
