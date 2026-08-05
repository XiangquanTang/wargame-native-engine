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
    DIRECT_FIRE_REQUESTED = "direct_fire_requested"
    DIRECT_FIRE_VALIDATED = "direct_fire_validated"
    ATTACK_LEVEL_SELECTED = "attack_level_selected"
    ATTACK_LEVEL_MODIFIED = "attack_level_modified"
    DICE_ROLLED = "dice_rolled"
    ROLL_MODIFIERS_APPLIED = "roll_modifiers_applied"
    COMBAT_RESULT_LOOKED_UP = "combat_result_looked_up"
    LOSSES_APPLIED = "losses_applied"
    SUPPRESSION_APPLIED = "suppression_applied"
    UNIT_DESTROYED = "unit_destroyed"
    WEAPON_COOLDOWN_STARTED = "weapon_cooldown_started"
    AMMUNITION_CONSUMED = "ammunition_consumed"
    MOVEMENT_STARTED = "movement_started"
    ARRIVED_HEX = "arrived_hex"
    MOVEMENT_BLOCKED = "movement_blocked"
    STOP_MOVE_REQUESTED = "stop_move_requested"
    STOPPING_STARTED = "stopping_started"
    STOPPING_COMPLETED = "stopping_completed"
    FATIGUE_CHANGED = "fatigue_changed"
    WEAPON_LOCK_STARTED = "weapon_lock_started"
    WEAPON_LOCKED = "weapon_locked"
    WEAPON_UNFOLD_STARTED = "weapon_unfold_started"
    WEAPON_UNFOLDED = "weapon_unfolded"
    MOVEMENT_MODE_CHANGE_STARTED = "movement_mode_change_started"
    MOVEMENT_MODE_CHANGED = "movement_mode_changed"


EVENT_PRIORITY = {
    EventType.ACTION_REJECTED: 0,
    EventType.OPERATOR_DEPLOYED: 10,
    EventType.DEPLOYMENT_ENDED: 11,
    EventType.COMBAT_STARTED: 12,
    EventType.OPERATOR_MOVED: 20,
    EventType.OPERATOR_STATE_CHANGED: 21,
    EventType.WAITED: 22,
    EventType.ATTACK_DECLARED: 30,
    EventType.TARGET_HIT: 31,
    EventType.OPERATOR_DAMAGED: 32,
    EventType.OPERATOR_DESTROYED: 33,
    EventType.OBJECTIVE_CONTROLLED: 40,
    EventType.GAME_TERMINATED: 90,
    EventType.GAME_TRUNCATED: 91,
    EventType.ENGINE_RESET: 99,
    EventType.DIRECT_FIRE_REQUESTED: 30,
    EventType.DIRECT_FIRE_VALIDATED: 31,
    EventType.ATTACK_LEVEL_SELECTED: 32,
    EventType.ATTACK_LEVEL_MODIFIED: 33,
    EventType.DICE_ROLLED: 34,
    EventType.ROLL_MODIFIERS_APPLIED: 35,
    EventType.COMBAT_RESULT_LOOKED_UP: 36,
    EventType.LOSSES_APPLIED: 37,
    EventType.SUPPRESSION_APPLIED: 38,
    EventType.UNIT_DESTROYED: 39,
    EventType.WEAPON_COOLDOWN_STARTED: 40,
    EventType.AMMUNITION_CONSUMED: 41,
    EventType.MOVEMENT_STARTED: 13,
    EventType.ARRIVED_HEX: 14,
    EventType.MOVEMENT_BLOCKED: 15,
    EventType.STOP_MOVE_REQUESTED: 16,
    EventType.STOPPING_STARTED: 17,
    EventType.STOPPING_COMPLETED: 18,
    EventType.FATIGUE_CHANGED: 19,
    EventType.WEAPON_LOCK_STARTED: 23,
    EventType.WEAPON_LOCKED: 24,
    EventType.WEAPON_UNFOLD_STARTED: 25,
    EventType.WEAPON_UNFOLDED: 26,
    EventType.MOVEMENT_MODE_CHANGE_STARTED: 27,
    EventType.MOVEMENT_MODE_CHANGED: 28,
}


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


def event_sort_key(value: EngineEvent) -> tuple[Any, ...]:
    event_time = int(value.data.get("event_time_ms", value.tick * 1000))
    action_sequence = int(value.data.get("action_sequence", -1))
    event_sequence = int(value.data.get("event_sequence", -1))
    return (event_time, action_sequence, event_sequence,
            EVENT_PRIORITY[value.event_type],
            int(value.data.get("roll_index", -1)), value.side or "",
            -1 if value.actor_id is None else value.actor_id,
            -1 if value.target_id is None else value.target_id,
            value.event_id)
