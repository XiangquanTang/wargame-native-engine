"""Native intent -> original Source action envelope, never execution evidence.

Only verified original fields are projected. The complete Native input and its
admission decision remain in an explicit namespace, including unknown actions.
This adapter is for newly produced frames; it never rewrites historical JSON.
"""
from __future__ import annotations

from typing import Any, Mapping

from wargame_native_engine.compat.source_frame.models import SourceFrame
from wargame_native_engine.core.action import ActionDecision, ActionType, EngineAction
from wargame_native_engine.io.serialization import json_clone

SOURCE_ACTION_EXPORT_SCHEMA = "native-source-action-export/1.0"
_SIMPLE_TYPES = {
    ActionType.CAPTURE: 5,
    ActionType.STOP_MOVE: 10,
    ActionType.WEAPON_LOCK: 11,
    ActionType.WEAPON_UNFOLD: 12,
    ActionType.END_DEPLOYMENT: 333,
}


def _identifier(value: object) -> bool:
    return type(value) is int and value >= 0


def _owner(value: object) -> bool:
    # Source owner is an opaque identity, not a color, actor unit, or fake seat.
    return ((isinstance(value, str) and bool(value.strip()))
            or (type(value) is int and value >= 0))


def _actor_owner(action: EngineAction, template: SourceFrame) -> tuple[Any, str | None]:
    color = {"red": 0, "blue": 1}.get(action.side)
    if color is None:
        return None, "SOURCE_ACTOR_OWNER_UNAVAILABLE"
    if action.actor_id is not None:
        matches = [unit for unit in template.all_units
                   if unit.obj_id == action.actor_id and unit.color == color]
        if len(matches) == 1 and _owner(matches[0].owner):
            return json_clone(matches[0].owner), None
        return None, "SOURCE_ACTOR_OWNER_UNAVAILABLE"
    if action.action_type is ActionType.END_DEPLOYMENT:
        # A side-wide command has no unit. Only a unique declared owner is safe.
        units = [unit for unit in template.all_units if unit.color == color]
        if units and all(_owner(unit.owner) for unit in units):
            first = units[0].owner
            if all(type(unit.owner) is type(first) and unit.owner == first for unit in units):
                return json_clone(first), None
        return None, "SOURCE_ACTOR_OWNER_AMBIGUOUS"
    return None, "SOURCE_ACTOR_OWNER_UNAVAILABLE"


def _message_fields(action: EngineAction) -> tuple[dict[str, Any] | None, str | None]:
    kind, params = action.action_type, action.parameters
    if kind in _SIMPLE_TYPES:
        return {"type": _SIMPLE_TYPES[kind]}, None
    if kind is ActionType.MOVE:
        path = params.get("path")
        if (isinstance(path, (list, tuple)) and path
                and all(_identifier(value) for value in path)):
            return {"type": 1, "move_path": list(path)}, None
        return None, "SOURCE_MOVE_PATH_UNAVAILABLE"
    if kind in (ActionType.BOARD, ActionType.UNLOAD):
        if _identifier(action.target_id):
            return {"type": 3 if kind is ActionType.BOARD else 4,
                    "target_obj_id": action.target_id}, None
        return None, "SOURCE_TARGET_OBJECT_UNAVAILABLE"
    if kind is ActionType.CHANGE_STATE:
        # The original agent forwards candidate target_state without defining
        # that enum. MoveType is a route-cost enum, NOT proof of target_state.
        return None, "SOURCE_TARGET_STATE_ENUM_UNVERIFIED"
    if kind in (ActionType.ATTACK, ActionType.SHOOT, ActionType.FIRE):
        mode = params.get("fire_mode", "DIRECT")
        weapon = params.get("weapon_id")
        if not _identifier(weapon):
            return None, "SOURCE_WEAPON_ID_UNAVAILABLE"
        if mode == "INDIRECT" and kind is ActionType.FIRE:
            target_hex = params.get("target_hex")
            if _identifier(target_hex) and action.target_id is None:
                return {"type": 8, "jm_pos": target_hex, "weapon_id": weapon}, None
            return None, "SOURCE_INDIRECT_TARGET_UNAVAILABLE"
        if mode == "GUIDED" and kind is ActionType.FIRE:
            shooter = params.get("shooter_id")
            if _identifier(action.target_id) and _identifier(shooter):
                # Native actor is the guider; guided_obj_id is the shooter.
                return {"type": 9, "target_obj_id": action.target_id,
                        "weapon_id": weapon, "guided_obj_id": shooter}, None
            return None, "SOURCE_GUIDED_PARTICIPANTS_UNAVAILABLE"
        if mode == "DIRECT" and _identifier(action.target_id):
            return {"type": 2, "target_obj_id": action.target_id,
                    "weapon_id": weapon}, None
        # Native direct empty-cell fire is not original JMPlan / indirect fire.
        return None, "SOURCE_FIRE_SHAPE_UNMAPPED"
    return None, "SOURCE_ACTION_TYPE_UNMAPPED"


def export_source_action(action: EngineAction, *, cur_step: int,
                         template: SourceFrame,
                         decision: ActionDecision | None = None) -> dict[str, Any]:
    """Return a detached intent record; cur_step is the pre-adjudication tick.

    No message is invented if the original identity/shape is unverified. Native
    extensions (e.g. weapon-specific lock, charge modes, unlock, wait, deploy)
    are retained losslessly under extra.native.action instead of numeric codes.
    """
    if type(cur_step) is not int or cur_step < 0:
        raise ValueError("source action cur_step must be a non-negative integer")
    owner, owner_reason = _actor_owner(action, template)
    fields, mapping_reason = _message_fields(action)
    reasons = [reason for reason in (owner_reason, mapping_reason) if reason]
    if action.action_type is not ActionType.END_DEPLOYMENT and not _identifier(action.actor_id):
        reasons.append("SOURCE_ACTOR_OBJECT_UNAVAILABLE")
    native: dict[str, Any] = {
        "schema_version": SOURCE_ACTION_EXPORT_SCHEMA,
        "semantics": "ACTION_INTENT_NOT_EXECUTION",
        "action_id": action.action_id,
        "action": action.to_dict(),
        "source_projection": {
            "status": "UNMAPPED" if reasons else "MAPPED",
            "reasons": reasons,
        },
    }
    if action.action_type is ActionType.WEAPON_LOCK and "weapon_id" in action.parameters:
        native["source_projection"]["native_extensions"] = ["WEAPON_SPECIFIC_LOCK"]
    if decision is not None:
        native["admission"] = {"accepted": decision.accepted, "code": decision.code,
                               "semantics": "ACCEPTANCE_NOT_COMPLETION"}
    record: dict[str, Any] = {"cur_step": cur_step}
    if not reasons:
        message = {"actor": owner}
        if action.action_type is not ActionType.END_DEPLOYMENT:
            message["obj_id"] = action.actor_id
        message.update(fields or {})
        record["message"] = message
    record["extra"] = {"native": native}
    return record


def owned_source_action(record: Mapping[str, Any], side: str,
                        friendly_ids: set[int]) -> bool:
    """Filter a server-produced 1.3 record; never infer authority from actor text."""
    extra = record.get("extra")
    native = extra.get("native") if isinstance(extra, Mapping) else None
    if not isinstance(native, Mapping) or native.get("schema_version") != SOURCE_ACTION_EXPORT_SCHEMA:
        return False
    action, admission = native.get("action"), native.get("admission")
    if not isinstance(action, Mapping) or action.get("side") != side:
        return False
    if not isinstance(admission, Mapping) or admission.get("accepted") is not True:
        return False
    actor_id = action.get("actor_id")
    return ((_identifier(actor_id) and actor_id in friendly_ids)
            or (actor_id is None and action.get("action_type") == "END_DEPLOYMENT"))
