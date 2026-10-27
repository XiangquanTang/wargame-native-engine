"""Strict, detached Native inputs for the shared eight-factor arithmetic.

This is an analysis convention, not Source SDK semantics or MOVE legality.
Unknown facts remain absent; the adapter never borrows initial template state.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any

from wargame_native_engine.compat.source_frame.contract import (
    NATIVE_ANALYSIS_FACTS_SCHEMA_VERSION as NATIVE_FACTS_SCHEMA,
)

from .eight_factor_contract import FactorEvidence, FactorStatus
from .semantic_identity import ANALYZER_MODEL_HASH, FACTOR_ORDER, FACTOR_SOURCE_FIELDS

NATIVE_MODEL_ID = "native-state-eight-factor"
NATIVE_MODEL_VERSION = "1.0.0"
NATIVE_POLICY = {
    "schema": "native-eight-factor-input-policy/1.0",
    "arithmetic_model_hash": ANALYZER_MODEL_HASH,
    "ammo": "unique_bound_current_pools;unbound_nonzero_unknown;self_munition_unknown",
    "mobility": "not_movement_locked;not_full_move_permission;boarding_separate",
    "status": "current_suppression;actual_native_fatigue_level_regardless_applicability;close_combat;lose_control_is_suppression_0.2_once;keep_excluded",
    "unknown": "no_imputation;strict_types;no_initial_template_fallback",
}
NATIVE_POLICY_HASH = hashlib.sha256(json.dumps(
    NATIVE_POLICY, sort_keys=True, separators=(",", ":")
).encode()).hexdigest()
NATIVE_MODEL_HASH = hashlib.sha256(json.dumps({
    "id": NATIVE_MODEL_ID, "version": NATIVE_MODEL_VERSION,
    "policy_hash": NATIVE_POLICY_HASH,
}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class NativeUnitInputs:
    """Detached numeric inputs and independently traceable evidence."""

    unit: dict[str, Any]
    evidence: tuple[FactorEvidence, ...]


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _flag(value: object) -> bool:
    return type(value) is bool or (type(value) is int and value in (0, 1))


def _valid(key: str, value: object) -> bool:
    if key in {"carry_weapon_ids", "see_enemy_bop_ids"}:
        return isinstance(value, (list, tuple)) and all(type(x) is int for x in value)
    if key in {"stop", "on_board", "close_combat"}:
        return _flag(value)
    if key == "move_state":
        return type(value) is int and value in range(6)
    if key == "weapon_unfold_state":
        return type(value) is int and value in (0, 1)
    return _number(value)


def adapt_native_unit(unit: Mapping[str, Any]) -> NativeUnitInputs:
    """Convert one Native-exported unit without mutating or aliasing input.

    OBSERVED is the legacy schema's availability flag. Evidence explicitly
    labels these inputs DERIVED_NATIVE and names their real Native sources.
    """
    native = _mapping(_mapping(unit.get("extra")).get("native"))
    has_facts = "analysis_facts" in native
    facts = _mapping(native.get("analysis_facts"))
    facts_valid = facts.get("schema_version") == NATIVE_FACTS_SCHEMA
    root = "unit.extra.native.analysis_facts"
    converted = {key: deepcopy(unit[key]) for key in ("obj_id", "color") if key in unit}
    evidence: list[FactorEvidence] = []

    for name in FACTOR_ORDER:
        inputs: dict[str, Any] = {}
        values: dict[str, Any] = {}
        missing: list[str] = []
        if has_facts and name in {"mobility", "status"}:
            inputs[f"{root}.schema_version"] = deepcopy(facts.get("schema_version"))

        def take(key: str, value: Any, path: str, valid: bool) -> None:
            inputs[path] = deepcopy(value)
            if valid:
                values[key] = inputs[path]
            else:
                missing.append(path)

        if name == "ammo":
            inventory_path = "unit.extra.native.weapon_inventory"
            inventory = _mapping(native.get("weapon_inventory"))
            pools = inventory.get("ammunition_pools")
            weapons = inventory.get("weapons")
            inputs[f"{inventory_path}.operator_id"] = inventory.get("operator_id")
            inputs[f"{inventory_path}.ammunition_pools"] = deepcopy(pools)
            binding_fields = ("weapon_id", "resource_policy", "ammunition_key")
            inputs[f"{inventory_path}.weapons"] = (
                [{key: deepcopy(weapon.get(key)) for key in binding_fields}
                 if isinstance(weapon, Mapping) else deepcopy(weapon)
                 for weapon in weapons]
                if isinstance(weapons, (list, tuple)) else deepcopy(weapons)
            )
            bound: set[str] = set()
            weapon_ids: list[int] = []
            valid = (
                inventory.get("operator_id") == unit.get("obj_id")
                and type(inventory.get("operator_id")) is int
                and isinstance(pools, Mapping) and isinstance(weapons, (list, tuple))
            )
            if valid:
                valid = all(type(k) is str and type(v) is int and v >= 0
                            for k, v in pools.items())
                for weapon in weapons:
                    if not isinstance(weapon, Mapping):
                        valid = False
                        break
                    key = weapon.get("ammunition_key")
                    wid = weapon.get("weapon_id")
                    if (weapon.get("resource_policy") != "AMMUNITION_KEY"
                            or type(key) is not str or key not in pools
                            or type(wid) is not int):
                        valid = False
                        break
                    bound.add(key)
                    weapon_ids.append(wid)
                carried = unit.get("carry_weapon_ids")
                inputs["unit.carry_weapon_ids"] = deepcopy(carried)
                valid = valid and _valid("carry_weapon_ids", carried)
                if valid:
                    valid = (len(set(weapon_ids)) == len(weapon_ids)
                             and len(set(carried)) == len(carried)
                             and set(weapon_ids) == set(carried)
                             and all(v == 0 for k, v in pools.items() if k not in bound))
            if valid:
                values["remain_bullet_nums"] = {k: pools[k] for k in sorted(bound)}
            else:
                missing.append(inventory_path)
        elif name == "mobility":
            for key in ("move_state", "stop"):
                take(key, unit.get(key), f"unit.{key}", _valid(key, unit.get(key)))
            lock_path = f"{root}.movement_locked" if has_facts else "unit.extra.native.movement_locked"
            lock = facts.get("movement_locked") if has_facts else native.get("movement_locked")
            inputs[lock_path] = deepcopy(lock)
            if type(lock) is bool and (not has_facts or facts_valid):
                values["can_to_move"] = not lock
            else:
                missing.append(lock_path)
            boarding = _mapping(facts.get("boarding"))
            onboard = boarding.get("on_board") if has_facts else unit.get("on_board")
            onboard_path = f"{root}.boarding.on_board" if has_facts else "unit.on_board"
            take("on_board", onboard, onboard_path,
                 _flag(onboard) and (not has_facts or facts_valid))
            if "on_board" in values:
                values["on_board"] = bool(values["on_board"])
        elif name == "status":
            fatigue = _mapping(facts.get("fatigue"))
            level = fatigue.get("native_level") if has_facts else native.get("fatigue_level")
            if has_facts:
                inputs[f"{root}.fatigue"] = deepcopy(fatigue)
            level_path = f"{root}.fatigue.native_level" if has_facts else "unit.extra.native.fatigue_level"
            take("tire", level, level_path,
                 type(level) is int and 0 <= level <= 2 and (not has_facts or facts_valid))
            suppression = _mapping(facts.get("suppression"))
            active, remaining = suppression.get("active"), suppression.get("remaining_ms")
            inputs[f"{root}.suppression.remaining_ms"] = deepcopy(remaining)
            take("lose_control", active, f"{root}.suppression.active",
                 facts_valid and type(active) is bool and type(remaining) is int
                 and remaining >= 0 and active == (remaining > 0))
            combat = facts.get("close_combat") if has_facts else unit.get("close_combat")
            take("close_combat", combat,
                 f"{root}.close_combat" if has_facts else "unit.close_combat",
                 _flag(combat) and (not has_facts or facts_valid))
            # The owner defines Native loss-of-control as fire suppression.
            # Exclude legacy keep to avoid applying the same event twice.
            # This is a policy constant, never a claim of observed unit.keep.
            values["keep"] = False
        else:
            for key in FACTOR_SOURCE_FIELDS[name]:
                take(key, unit.get(key), f"unit.{key}", _valid(key, unit.get(key)))

        converted.update(values)
        if name in {"ammo", "mobility", "status"}:
            raw_value: Any = {"kind": "DERIVED_NATIVE", "inputs": inputs,
                              "converted": deepcopy(values)}
            if name == "ammo":
                raw_value["projected_weapon_fields"] = list(binding_fields)
            if name == "status":
                raw_value.update({
                    "policy_constants": {"keep": False},
                    "excluded_terms": {"keep": "NOT_APPLICABLE_BY_NATIVE_POLICY"},
                })
        else:
            raw = tuple(inputs[f"unit.{key}"] for key in FACTOR_SOURCE_FIELDS[name])
            raw_value = raw[0] if len(raw) == 1 else list(raw)
        evidence.append(FactorEvidence(
            name=name,
            raw_value=raw_value,
            source_paths=tuple(inputs),
            status=FactorStatus.UNKNOWN if missing else FactorStatus.OBSERVED,
            missing_fields=tuple(sorted(set(missing))),
        ))
    return NativeUnitInputs(converted, tuple(evidence))
