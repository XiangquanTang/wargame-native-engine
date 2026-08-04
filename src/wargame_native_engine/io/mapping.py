from __future__ import annotations

from typing import Any, Mapping

from wargame_native_engine.core.action import ActionType, EngineAction


EXTERNAL_FORMAT_ACTIONS = {
    1: ActionType.MOVE,
    2: ActionType.ATTACK,
    6: ActionType.CHANGE_STATE,
    333: ActionType.END_DEPLOYMENT,
}


class UnsupportedActionError(ValueError):
    code = "unsupported_action"


def action_from_compatible_payload(side: str,
                                   payload: Mapping[str, Any]) -> EngineAction:
    """Map a small public-format subset; this does not emulate SDK behavior."""
    action_number = int(payload.get("type", -1))
    action_type = EXTERNAL_FORMAT_ACTIONS.get(action_number)
    if action_type is None:
        raise UnsupportedActionError(f"unsupported_action:{action_number}")
    actor = payload.get("actor", payload.get("obj_id"))
    target_id = payload.get("target_obj_id")
    position = payload.get("target_position", payload.get("target_pos"))
    if action_type is ActionType.MOVE and position is None:
        path = payload.get("move_path") or []
        position = path[-1] if path else None
    return EngineAction(
        side=side,
        actor_id=None if actor is None else int(actor),
        action_type=action_type,
        target_id=None if target_id is None else int(target_id),
        target_position=(None if position is None
                         else (float(position[0]), float(position[1]))),
        parameters={"compatible_payload": dict(payload)},
    )
