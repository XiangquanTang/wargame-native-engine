from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from wargame_native_engine.io.serialization import canonical_hash, json_clone


class EventType(str, Enum):
    ENGINE_RESET = "engine_reset"
    OPERATOR_DEPLOYED = "operator_deployed"
    DEPLOYMENT_ENDED = "deployment_ended"
    COMBAT_STARTED = "combat_started"
    OPERATOR_MOVED = "operator_moved"
    OPERATOR_STATE_CHANGED = "operator_state_changed"
    WAITED = "waited"
    ATTACK_DECLARED = "attack_declared"
    TARGET_HIT = "target_hit"
    OPERATOR_DAMAGED = "operator_damaged"
    OPERATOR_DESTROYED = "operator_destroyed"
    ACTION_REJECTED = "action_rejected"
    OBJECTIVE_CONTROLLED = "objective_controlled"
    GAME_TERMINATED = "game_terminated"
    GAME_TRUNCATED = "game_truncated"


@dataclass(frozen=True, slots=True)
class EngineEvent:
    tick: int
    event_type: EventType
    side: str | None = None
    actor_id: int | None = None
    target_id: int | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def event_id(self) -> str:
        return "event-" + canonical_hash(self.to_dict(include_id=False))[:24]

    def to_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        value = {"schema_version": "native-event/1.0", "tick": self.tick,
                 "event_type": self.event_type.value, "side": self.side,
                 "actor_id": self.actor_id, "target_id": self.target_id,
                 "data": json_clone(self.data)}
        if include_id:
            value["event_id"] = self.event_id
        return value
