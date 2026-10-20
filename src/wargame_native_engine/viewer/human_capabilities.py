"""Side-safe presentation model for the Human command capability UI.

This module only projects an already-frozen :class:`ControllerView`.  Complete
OWN weapon inventory comes from its authoritative side-bound read model;
current legal targets remain a separate annotation from ``valid_actions``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from wargame_native_engine.gateway.models import ControllerView
from wargame_native_engine.operations.commands import (
    _FrozenActionRecords,
    frozen_action_records,
)
from wargame_native_engine.viewer.operator_registry import (
    OperatorRegistry,
    WeaponDescriptor,
    load_operator_registry,
)


HUMAN_CAPABILITY_SCHEMA = "human-command-capabilities/1.1"

_CAPABILITY_ORDER = (
    ("MOVE", "MOVE"),
    ("DIRECT_FIRE", "SHOOT"),
    ("GUIDED_FIRE", "GUIDED_FIRE"),
    ("INDIRECT_FIRE", "INDIRECT_FIRE"),
    ("BOARD", "BOARD"),
    ("UNLOAD", "UNLOAD"),
    ("CAPTURE", "CAPTURE"),
    ("ENTER_CONCEALMENT", "ENTER_CONCEALMENT"),
    ("STOP_MOVE", "STOP_MOVE"),
    ("WEAPON_UNFOLD", "WEAPON_UNFOLD"),
    ("WEAPON_LOCK", "WEAPON_LOCK"),
    ("WAIT", "WAIT"),
)


def source_weapon_profiles(
    scenario_metadata: Mapping[str, object],
) -> Mapping[str, object]:
    """Return a detached Source-compatible weapon-profile mapping.

    Missing presentation metadata is valid; legality still comes exclusively
    from frozen Engine actions.
    """

    if not isinstance(scenario_metadata, Mapping):
        raise TypeError("scenario_metadata must be a mapping")
    source = scenario_metadata.get("source_compatible")
    if not isinstance(source, Mapping):
        return {}
    weapons = source.get("weapons")
    if not isinstance(weapons, Mapping):
        return {}
    return deepcopy(dict(weapons))


def build_human_capabilities(
    summary: Mapping[str, object],
    view: ControllerView,
    actor_ids: Sequence[int],
    *,
    weapon_profiles: Mapping[str | int, object] | None = None,
    operator_registry: OperatorRegistry | None = None,
) -> dict[str, object]:
    """Enrich a product summary with a capability-oriented UI projection.

    No action ID is returned.  Carried weapons never disappear merely because
    no attack is currently legal.
    """

    if not isinstance(summary, Mapping):
        raise TypeError("summary must be a mapping")
    actors = _actor_ids(actor_ids)
    profiles = weapon_profiles or {}
    if not isinstance(profiles, Mapping):
        raise TypeError("weapon_profiles must be a mapping")
    registry = operator_registry or load_operator_registry()
    friendlies = _friendly_units(view)
    requested = set(actors)
    controlled = requested.intersection(friendlies)
    visible_enemy_ids = _visible_enemy_ids(view)
    source_units = _source_units(view, controlled)
    inventories = _own_weapon_inventories(view, controlled)
    action_records = frozen_action_records(view)
    operations = summary.get("operations")
    if not isinstance(operations, Mapping):
        raise ValueError("capability summary operations must be an object")

    direct_fire_units = []
    for actor_id in actors:
        if actor_id not in controlled:
            continue
        weapon_targets = _legal_direct_fire_targets(
            action_records, actor_id, visible_enemy_ids,
        )
        source_unit = source_units.get(actor_id, {})
        identity = registry.resolve_viewer_unit(source_unit)
        summary_unit = next(
            (
                value for value in summary.get("selected_units", [])
                if isinstance(value, Mapping) and value.get("unit_id") == actor_id
            ),
            {},
        )
        inventory = inventories.get(actor_id)
        weapon_resources = (
            inventory.get("weapons", [])
            if isinstance(inventory, Mapping)
            else [{"weapon_id": value} for value in sorted(weapon_targets)]
        )
        weapons = [
            _weapon_record(
                int(resource["weapon_id"]),
                sorted(weapon_targets.get(int(resource["weapon_id"]), set())),
                source_unit,
                profiles,
                registry.weapon_profiles.get(int(resource["weapon_id"])),
                resource,
                (
                    inventory.get("weapon_cooldown_remaining_ms")
                    if isinstance(inventory, Mapping) else None
                ),
            )
            for resource in weapon_resources
            if isinstance(resource, Mapping)
            and type(resource.get("weapon_id")) is int
        ]
        direct_fire_units.append({
            "unit_id": actor_id,
            "display_name": str(
                summary_unit.get("display_name")
                or f"{identity.display_name} {actor_id}"
            ),
            "operator_key": identity.key,
            "weapons": weapons,
        })

    guided_fire_options = _legal_guided_fire_options(
        action_records,
        view,
        controlled,
        friendlies,
        visible_enemy_ids,
        profiles,
        registry,
    )
    indirect_fire_options = _legal_indirect_fire_options(
        summary, controlled, source_units, profiles, registry,
    )
    boarding_options = _legal_boarding_options(action_records, controlled)
    capture_options = _legal_capture_options(
        action_records,
        controlled,
        view.observation.get("objectives"),
    )

    capabilities = []
    for capability_type, operation_type in _CAPABILITY_ORDER:
        operation = operations.get(operation_type)
        if not isinstance(operation, Mapping):
            continue
        applicable = operation.get("applicable_count")
        if type(applicable) is not int or applicable <= 0:
            continue
        if capability_type == "DIRECT_FIRE" and not direct_fire_units:
            continue
        if capability_type == "GUIDED_FIRE" and not guided_fire_options:
            continue
        if capability_type == "INDIRECT_FIRE" and not indirect_fire_options:
            continue
        metadata: dict[str, object] = {"applicable_count": applicable}
        if capability_type == "DIRECT_FIRE":
            metadata.update({
                "unit_count": len(direct_fire_units),
                "weapon_count": sum(
                    len(value["weapons"]) for value in direct_fire_units
                ),
            })
        elif capability_type == "GUIDED_FIRE":
            metadata.update({
                "option_count": len(guided_fire_options),
                "shooter_count": len({
                    value["shooter_id"] for value in guided_fire_options
                }),
                "target_count": len({
                    value["target_id"] for value in guided_fire_options
                }),
            })
        elif capability_type == "INDIRECT_FIRE":
            metadata.update({
                "option_count": len(indirect_fire_options),
                "shooter_count": len({
                    value["shooter_id"] for value in indirect_fire_options
                }),
            })
        elif capability_type in {"BOARD", "UNLOAD"}:
            matching = [
                value for value in boarding_options
                if value["operation_type"] == operation_type
            ]
            if not matching:
                continue
            metadata["option_count"] = len(matching)
        elif capability_type == "CAPTURE":
            if not capture_options:
                continue
            metadata["option_count"] = len(capture_options)
        capabilities.append({
            "capability_type": capability_type,
            "operation_type": operation_type,
            "status": "AVAILABLE",
            "actor_ids": list(actors),
            "metadata": metadata,
        })

    direct_intent_available = (
        bool(actors)
        and bool(direct_fire_units)
        and len(direct_fire_units) == len(controlled)
        and all(bool(value["weapons"]) for value in direct_fire_units)
        and bool(visible_enemy_ids)
    )
    if direct_intent_available and not any(
        value["capability_type"] == "DIRECT_FIRE" for value in capabilities
    ):
        capabilities.append({
            "capability_type": "DIRECT_FIRE",
            "operation_type": "SHOOT",
            "status": "AVAILABLE",
            "actor_ids": list(actors),
            "metadata": {
                "applicable_count": 0,
                "unit_count": len(direct_fire_units),
                "weapon_count": sum(
                    len(value["weapons"]) for value in direct_fire_units
                ),
                "intent_only": True,
            },
        })
        order = {
            name: index for index, (name, _operation) in enumerate(
                _CAPABILITY_ORDER
            )
        }
        capabilities.sort(key=lambda value: order[value["capability_type"]])

    result = deepcopy(dict(summary))
    result.update({
        "schema": HUMAN_CAPABILITY_SCHEMA,
        "state_binding": {
            "decision_window_index": view.step_index,
            "frame_index": view.frame_index,
            "sim_time_ms": view.sim_time_ms,
            "observation_hash": view.observation.get("observation_hash"),
        },
        "capabilities": capabilities,
        "direct_fire": {
            "semantics": "complete_carried_weapons_plus_legal_annotations",
            "selectable_target_ids": sorted(visible_enemy_ids),
            "units": direct_fire_units,
        },
        "weapon_inventory": {
            "semantics": "authoritative_own_inventory_at_controller_view_version",
            "units": direct_fire_units,
        },
        "guided_fire": {
            "semantics": "current_frozen_engine_guided_fire_actions",
            "options": guided_fire_options,
        },
        "indirect_fire": {
            "semantics": "server_validated_global_hex_target",
            "options": indirect_fire_options,
        },
        "boarding": {
            "semantics": "current_frozen_engine_boarding_actions",
            "options": boarding_options,
        },
        "capture": {
            "semantics": "current_frozen_engine_capture_actions",
            "options": capture_options,
        },
    })
    return result


def _legal_capture_options(
    action_records: _FrozenActionRecords,
    controlled: set[int],
    objectives: object,
) -> list[dict[str, object]]:
    objective_index: dict[int, Mapping[str, Any]] = {}
    if isinstance(objectives, Sequence) and not isinstance(
        objectives, (str, bytes)
    ):
        for value in objectives:
            if not isinstance(value, Mapping):
                continue
            objective_id = value.get("objective_id")
            if type(objective_id) is int:
                objective_index[objective_id] = value
    result: list[dict[str, object]] = []
    for actor_id in controlled:
        for value in action_records.query_records(
            actor_reference=actor_id,
            action_type="CAPTURE",
        ):
            action = value.get("action")
            if not isinstance(action, Mapping):
                continue
            objective_id = action.get("target_id")
            objective = objective_index.get(objective_id)
            if type(objective_id) is not int or objective is None:
                continue
            x, y = objective.get("x"), objective.get("y")
            if (
                isinstance(x, bool)
                or not isinstance(x, (int, float))
                or isinstance(y, bool)
                or not isinstance(y, (int, float))
                or float(x) != int(x)
                or float(y) != int(y)
            ):
                continue
            result.append({
                "actor_id": actor_id,
                "objective_id": objective_id,
                "coord": int(y) * 100 + int(x),
                "controlling_side": objective.get("controlling_side"),
                "value": objective.get("value"),
            })
    return sorted(
        result,
        key=lambda value: (
            int(value["actor_id"]), int(value["objective_id"])
        ),
    )


def _legal_boarding_options(
    action_records: _FrozenActionRecords,
    controlled: set[int],
) -> list[dict[str, object]]:
    """Project only explicit Engine-issued BOARD/UNLOAD action tuples."""

    result: list[dict[str, object]] = []
    for actor_id in controlled:
        for operation in ("BOARD", "UNLOAD"):
            for value in action_records.query_records(
                actor_reference=actor_id,
                action_type=operation,
            ):
                action = value.get("action")
                if not isinstance(action, Mapping):
                    continue
                target_id = action.get("target_id")
                parameters = action.get("parameters")
                if type(target_id) is not int or not isinstance(
                    parameters, Mapping,
                ):
                    continue
                result.append({
                    "operation_type": operation,
                    "actor_id": actor_id,
                    "target_id": target_id,
                    "profile_id": parameters.get("profile_id"),
                })
    return sorted(
        result,
        key=lambda value: (
            str(value["operation_type"]),
            int(value["actor_id"]),
            int(value["target_id"]),
            str(value.get("profile_id")),
        ),
    )


def _legal_indirect_fire_options(
    summary: Mapping[str, object],
    controlled: set[int],
    source_units: Mapping[int, Mapping[str, Any]],
    profiles: Mapping[str | int, object],
    registry: OperatorRegistry,
) -> list[dict[str, object]]:
    indirect = summary.get("indirect_fire")
    values = indirect.get("capabilities", []) if isinstance(
        indirect, Mapping
    ) else []
    result: list[dict[str, object]] = []
    for value in values:
        if not isinstance(value, Mapping) or value.get("status") != "AVAILABLE":
            continue
        shooter_id = value.get("shooter_id")
        weapon_id = value.get("weapon_id")
        if (
            type(shooter_id) is not int
            or shooter_id not in controlled
            or type(weapon_id) is not int
        ):
            continue
        source_unit = source_units.get(shooter_id, {})
        identity = registry.resolve_viewer_unit(source_unit)
        weapon = _weapon_record(
            weapon_id, [], source_unit, profiles,
            registry.weapon_profiles.get(weapon_id),
        )
        result.append({
            "shooter_id": shooter_id,
            "shooter_display_name": str(
                source_unit.get("name") or f"{identity.display_name} {shooter_id}"
            ),
            "weapon_id": weapon_id,
            "weapon_display_name": weapon["display_name"],
            "artillery_class": value.get("artillery_class"),
            "profile_id": value.get("profile_id"),
            "range_policy": value.get("range_policy"),
            "target_policy": value.get("target_policy"),
        })
    return sorted(
        result,
        key=lambda item: (
            int(item["shooter_id"]), int(item["weapon_id"]),
            str(item.get("profile_id")),
        ),
    )


def _actor_ids(values: Sequence[int]) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("actor_ids must be a sequence")
    result = []
    for value in values:
        if type(value) is not int or value < 0:
            raise ValueError("actor_ids must contain non-negative exact ints")
        result.append(value)
    if len(result) != len(set(result)):
        raise ValueError("actor_ids must be unique")
    return tuple(sorted(result))


def _friendly_units(view: ControllerView) -> dict[int, Mapping[str, Any]]:
    values = view.observation.get("friendly_operators")
    if not isinstance(values, list):
        raise ValueError("side observation lacks friendly_operators")
    result = {}
    for value in values:
        if not isinstance(value, Mapping):
            continue
        actor_id = value.get("operator_id")
        if type(actor_id) is int and actor_id >= 0:
            result[actor_id] = value
    return result


def _visible_enemy_ids(view: ControllerView) -> set[int]:
    values = view.observation.get("visible_enemies")
    if not isinstance(values, list):
        raise ValueError("side observation lacks visible_enemies")
    return {
        value["operator_id"]
        for value in values
        if isinstance(value, Mapping)
        and type(value.get("operator_id")) is int
        and value["operator_id"] >= 0
    }


def _source_units(
    view: ControllerView, actor_ids: set[int],
) -> dict[int, Mapping[str, Any]]:
    result = {}
    for field in ("operators", "passengers"):
        values = view.source_frame.get(field, [])
        if not isinstance(values, list):
            raise ValueError(f"side source projection {field} must be an array")
        for value in values:
            if not isinstance(value, Mapping):
                continue
            actor_id = value.get("obj_id")
            if type(actor_id) is int and actor_id in actor_ids:
                result[actor_id] = value
    if actor_ids - set(result):
        raise ValueError("side source projection lacks selected friendly unit")
    return result


def _own_weapon_inventories(
    view: ControllerView, actor_ids: set[int],
) -> dict[int, Mapping[str, Any]]:
    result: dict[int, Mapping[str, Any]] = {}
    for value in view.own_weapon_inventory:
        if not isinstance(value, Mapping):
            continue
        actor_id = value.get("operator_id")
        if type(actor_id) is int and actor_id in actor_ids:
            result[actor_id] = value
    return result


def _legal_direct_fire_targets(
    action_records: _FrozenActionRecords,
    actor_id: int,
    visible_enemy_ids: set[int],
) -> dict[int, set[int]]:
    result: dict[int, set[int]] = {}
    for value in action_records.query_records(
        actor_reference=actor_id,
        action_type="SHOOT",
    ):
        action = value.get("action")
        if not isinstance(action, Mapping):
            continue
        target_id = action.get("target_id")
        parameters = action.get("parameters")
        weapon_id = (
            parameters.get("weapon_id") if isinstance(parameters, Mapping) else None
        )
        if type(target_id) is not int or target_id not in visible_enemy_ids:
            raise ValueError("SHOOT valid action targets a non-visible enemy")
        if type(weapon_id) is not int or weapon_id < 0:
            raise ValueError("SHOOT valid action lacks an exact weapon_id")
        result.setdefault(weapon_id, set()).add(target_id)
    return result


def _weapon_record(
    weapon_id: int,
    target_ids: list[int],
    source_unit: Mapping[str, Any],
    profiles: Mapping[str | int, object],
    descriptor: WeaponDescriptor | None,
    resource: Mapping[str, Any] | None = None,
    authoritative_cooldown_ms: object = None,
) -> dict[str, object]:
    raw_profile = profiles.get(str(weapon_id), profiles.get(weapon_id, {}))
    profile = raw_profile if isinstance(raw_profile, Mapping) else {}
    maximum_range = profile.get("maximum_range")
    if not _nonnegative_number(maximum_range):
        maximum_range = descriptor.maximum_range if descriptor is not None else None
    raw_domains = profile.get("target_domains")
    target_domains = (
        [str(value).upper() for value in raw_domains]
        if isinstance(raw_domains, Sequence)
        and not isinstance(raw_domains, (str, bytes))
        else list(descriptor.target_domains) if descriptor is not None else []
    )
    authoritative_resource = resource or {}
    ammo_key = authoritative_resource.get(
        "ammunition_key", profile.get("ammunition_key")
    )
    if "ammunition" in authoritative_resource:
        raw_ammunition = authoritative_resource.get("ammunition")
        ammunition = (
            raw_ammunition
            if type(raw_ammunition) is int and raw_ammunition >= 0 else None
        )
    else:
        ammunition = _ammunition(source_unit.get("remain_bullet_nums"), ammo_key)
    if type(authoritative_cooldown_ms) is int and authoritative_cooldown_ms >= 0:
        cooldown = authoritative_cooldown_ms / 1000.0
    else:
        cooldown = source_unit.get("weapon_cool_time")
        if not _nonnegative_number(cooldown):
            cooldown = None
    shared = authoritative_resource.get("shared_with_weapon_ids", [])
    shared_ids = (
        sorted(int(value) for value in shared if type(value) is int)
        if isinstance(shared, Sequence) and not isinstance(shared, (str, bytes))
        else []
    )
    raw_modes = authoritative_resource.get("attack_modes", [])
    attack_modes = (
        sorted(str(value).upper() for value in raw_modes)
        if isinstance(raw_modes, Sequence)
        and not isinstance(raw_modes, (str, bytes))
        else []
    )
    return {
        "weapon_id": weapon_id,
        "display_name": (
            descriptor.name
            if descriptor is not None and descriptor.name != "UNKNOWN"
            else f"武器 {weapon_id}"
        ),
        "weapon_class": (
            descriptor.weapon_class if descriptor is not None else "UNKNOWN"
        ),
        "maximum_range": maximum_range,
        "target_domains": target_domains,
        "ammunition": ammunition,
        "ammunition_status": (
            "SELF_AS_MUNITION"
            if authoritative_resource.get("resource_policy")
            == "SELF_AS_MUNITION"
            else "KNOWN" if ammunition is not None else "UNKNOWN"
        ),
        "ammunition_pool": ammo_key,
        "shared_ammunition": bool(shared_ids),
        "shared_with_weapon_ids": shared_ids,
        "ammunition_cost": authoritative_resource.get("ammunition_cost"),
        "attack_modes": attack_modes,
        "cooldown_remaining_seconds": cooldown,
        "ready_state": (
            "COOLDOWN" if cooldown is not None and cooldown > 0
            else "STATE_REPORTED"
        ),
        "legal_target_ids": target_ids,
        "profile_source": (
            "scenario_weapon_profile" if profile else "operator_registry"
        ),
    }


def _legal_guided_fire_options(
    action_records: _FrozenActionRecords,
    view: ControllerView,
    controlled: set[int],
    friendlies: Mapping[int, Mapping[str, Any]],
    visible_enemy_ids: set[int],
    profiles: Mapping[str | int, object],
    registry: OperatorRegistry,
) -> list[dict[str, object]]:
    """Project explicit Engine FIRE actions without inferring authority."""

    source_units = _source_units(view, set(friendlies))
    values: list[dict[str, object]] = []
    for guider_id in controlled:
        for raw in action_records.query_records(
            actor_reference=guider_id,
            action_type="FIRE",
            parameters={"fire_mode": "GUIDED"},
        ):
            action = raw.get("action")
            if not isinstance(action, Mapping):
                continue
            parameters = action.get("parameters")
            if not isinstance(parameters, Mapping):
                continue
            shooter_id = parameters.get("shooter_id")
            weapon_id = parameters.get("weapon_id")
            target_id = action.get("target_id")
            if (
                parameters.get("guider_id") != guider_id
                or type(shooter_id) is not int
                or shooter_id not in friendlies
                or type(weapon_id) is not int
                or weapon_id < 0
                or type(target_id) is not int
                or target_id not in visible_enemy_ids
            ):
                raise ValueError("GUIDED FIRE valid action violates side projection")
            descriptor = registry.weapon_profiles.get(weapon_id)
            profile = profiles.get(str(weapon_id), profiles.get(weapon_id, {}))
            profile = profile if isinstance(profile, Mapping) else {}
            values.append({
                "capability": "GUIDED_FIRE",
                "guider_id": guider_id,
                "guider_display_name": _source_display_name(
                    source_units[guider_id], guider_id, registry,
                ),
                "shooter_id": shooter_id,
                "shooter_display_name": _source_display_name(
                    source_units[shooter_id], shooter_id, registry,
                ),
                "weapon_id": weapon_id,
                "weapon_display_name": (
                    descriptor.name
                    if descriptor is not None and descriptor.name != "UNKNOWN"
                    else f"武器 {weapon_id}"
                ),
                "maximum_range": profile.get(
                    "maximum_range",
                    None if descriptor is None else descriptor.maximum_range,
                ),
                "target_id": target_id,
                # The executable FIRE action currently does not expose profile ID.
                # Preserve that boundary instead of inferring it from subtype/name.
                "profile_id": parameters.get("guidance_profile_id"),
            })
    return sorted(values, key=lambda value: (
        int(value["shooter_id"]),
        int(value["guider_id"]),
        int(value["weapon_id"]),
        int(value["target_id"]),
    ))


def _source_display_name(
    source_unit: Mapping[str, Any],
    unit_id: int,
    registry: OperatorRegistry,
) -> str:
    name = source_unit.get("name")
    if isinstance(name, str) and name.strip():
        return f"{name.strip()} {unit_id}"
    descriptor = registry.resolve_viewer_unit(source_unit)
    return f"{descriptor.display_name} {unit_id}"


def _ammunition(value: object, ammunition_key: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if not isinstance(value, Mapping) or ammunition_key is None:
        return None
    result = value.get(str(ammunition_key), value.get(ammunition_key))
    return result if type(result) is int and result >= 0 else None


def _nonnegative_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    )
