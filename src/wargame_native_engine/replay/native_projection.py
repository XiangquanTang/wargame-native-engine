"""Minimal Native-created playback facts, never applied to imported raw JSON.

Static provenance belongs in the header, integrity in one external package
hash. This projection does not claim equality with full debug GREEN metadata.
"""
from copy import deepcopy
from typing import Any, Mapping

NATIVE_PLAYBACK_PROJECTION = "native-source-playback-projection/1.0"
STATIC_NATIVE_FIELDS = frozenset({
    "engine_version", "adjudication_semantics_version", "ruleset_id", "ruleset_version", "ruleset_hash",
    "episode_seed", "session_id", "movement_policy_id", "movement_policy_version", "movement_policy_hash",
    "perspective_policy_id", "perspective_policy_version", "perspective_policy_hash",
    "adjudication_table_set_id", "adjudication_table_set_version", "adjudication_table_set_hash",
    "boarding_type_assignment_authority_version", "boarding_type_assignment_authority_hash",
    "boarding_type_assignment_policy", "operator_lifecycle_authority_version", "operator_lifecycle_authority_hash",
    "loitering_munition_attack_authority_version", "loitering_munition_attack_authority_hash",
    "objective_capture_policy", "map_runtime_data", "mounted_position_policy",
})


def native_playback_frame(frame: Mapping[str, Any], *, source_registry: list[dict] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return playback facts plus static header; preserve all unknown fields.

    Recognized Source messages retain Native-only parameters (notably weapon
    selection for lock, guided roles and charge modes). UNMAPPED actions keep
    their full Native payload. Admission is never changed into completion.
    """
    value = deepcopy(dict(frame))
    native = value.get("extra", {}).get("native", {})
    static = {key: native.pop(key) for key in STATIC_NATIVE_FIELDS if key in native}
    native.pop("state_hash", None)  # Native recorder audit field, not a game fact.
    for record in value.get("actions", []):
        if not isinstance(record, dict):
            continue
        detail = record.get("extra", {}).get("native", {})
        action = detail.get("action")
        message = record.get("message")
        if (not isinstance(action, dict) or not isinstance(message, dict)
                or detail.get("source_projection", {}).get("status") != "MAPPED"):
            continue
        detail.pop("action")
        detail["side"] = action["side"]
        detail["action_type"] = action["action_type"]
        parameters = deepcopy(action.get("parameters", {}))
        # Remove only exact redundancies already in the verified Source message.
        for parameter, field in (("path", "move_path"), ("weapon_id", "weapon_id"),
                                 ("target_hex", "jm_pos"), ("shooter_id", "guided_obj_id")):
            if field in message and parameters.get(parameter) == message[field]:
                parameters.pop(parameter, None)
        if parameters:
            detail["parameters"] = parameters
        if action.get("target_id") is not None and action["target_id"] != message.get("target_obj_id"):
            detail["target_id"] = action["target_id"]
        if action.get("target_position") is not None:
            detail["target_position"] = action["target_position"]
        # Unusual additional Native fields are facts, not presumed audit.
        for key, item in action.items():
            if key not in {"side", "actor_id", "action_type", "target_id", "target_position", "parameters"}:
                detail.setdefault("extensions", {})[key] = item
    if source_registry is not None:
        from wargame_native_engine.replay.compact import json_equal
        for judge in value.get("judge_info", []):
            if not isinstance(judge, dict):
                continue
            for event in judge.get("result", {}).get("events", []):
                if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
                    continue
                source = event["data"].get("source_rule_set")
                if not isinstance(source, dict):
                    continue
                matched = next((entry for entry in source_registry if json_equal(entry["record"], source)), None)
                if matched is None:
                    matched = {"id": f"adjudication-source-{len(source_registry)}", "record": deepcopy(source)}
                    source_registry.append(matched)
                event["data"]["source_rule_set"] = {"source_ref": matched["id"]}
    return value, static
