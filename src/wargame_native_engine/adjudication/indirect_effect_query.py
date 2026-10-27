"""Exact, read-only indirect-fire estimates for a declared frozen target.

This module accepts only detached actors, explicit target assumptions and static
rules/map readers. It never looks up WorldState occupants, observation state or
live RNG. Scatter and damage are independent fair-d6 model distributions, not
predictions of the engine's seeded generator or a target's future movement.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict
from fractions import Fraction
from typing import Any

from wargame_native_engine.adjudication.indirect_fire import (
    IndirectFireObservationPolicy,
    IndirectFireTargetEvidence,
    _apply_indirect_result_correction,
    _indirect_fire_modifiers,
    _indirect_fire_result_correction,
)
from wargame_native_engine.adjudication.indirect_fire_service import (
    _distance_band,
    _scatter_distance,
)
from wargame_native_engine.adjudication.table_loader import AdjudicationTableSet
from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.movement.map_adapter import SourceHexMap
from wargame_native_engine.rules.indirect_fire import (
    IndirectFireDamagePolicy,
    IndirectFireExecutionPolicy,
    IndirectFireProfile,
    IndirectRangePolicy,
)
from wargame_native_engine.visibility import hex_distance

# Constant work: 66 weighted scatter branches and at most 2 * 66 damage branches.
# This path also fails closed if a custom table requests unbounded map traversal.
MAX_SCATTER_DISTANCE = 64
_TWO_D6 = tuple((total, 6 - abs(7 - total)) for total in range(2, 13))
_DISPERSION_TABLES = {
    "UNOBSERVED": "unobserved_dispersion",
    "GRID_CORRECTED": "grid_corrected_dispersion",
    "TARGET_CORRECTED": "target_corrected_dispersion",
}


def indirect_fire_probabilities(
    tables: AdjudicationTableSet,
    *,
    source_map: SourceHexMap,
    profile: IndirectFireProfile,
    damage_policy: IndirectFireDamagePolicy,
    execution_policy: IndirectFireExecutionPolicy,
    attacker: OperatorState,
    target: OperatorState,
    target_profile: Mapping[str, Any],
    observation_policy: str,
    planned_hex: int | None = None,
) -> dict[str, Any]:
    """Estimate one selected target's first impact under stated assumptions.

    The caller must validate actor ownership and visible target identity before
    supplying detached inputs. ``planned_hex`` defaults to the target's declared
    cell. A hypothetical target is not authorization to resolve a real occupant.
    Launch legality/resources are deliberately not inferred by this calculator.
    """
    if isinstance(observation_policy, IndirectFireObservationPolicy):
        observation_policy = observation_policy.value
    if not isinstance(observation_policy, str) or observation_policy not in _DISPERSION_TABLES:
        return _unresolved("INDIRECT_OBSERVATION_POLICY_REQUIRED")
    if target_profile.get("stacked") is not False:
        return _unresolved("INDIRECT_STACKED_PROFILE_INCOMPATIBLE", ("single_selected_target_requires_stacked_false",))
    if planned_hex is None:
        planned_hex = target.current_hex
    if any(type(value) is not int or value < 0 for value in (
        attacker.current_hex, target.current_hex, planned_hex,
    )):
        return _unresolved("INDIRECT_HEX_INVALID")
    try:
        for value in (attacker.current_hex, target.current_hex, planned_hex):
            source_map.cell(value)
    except ValueError:
        return _unresolved("TARGET_HEX_NOT_FOUND")
    if profile.unresolved_fields:
        return _unresolved("INDIRECT_PROFILE_UNRESOLVED", profile.unresolved_fields)
    if execution_policy.impact_policy.affected_hex_policy.value != "IMPACT_HEX_ONLY":
        return _unresolved("INDIRECT_AFFECTED_AREA_UNSUPPORTED")
    distance = hex_distance(attacker.current_hex, planned_hex)
    distance_band, band_index = _distance_band(distance)
    context = {
        "hit_probability": None,
        "hit_probability_reason": "NO_SEPARATE_HIT_TEST",
        "distribution_condition": "launched_mission_frozen_target_initial_indirect_impact",
        "can_execute_under_assumptions": None,
        "probability_basis": "independent_fair_d6",
        "geometry": {
            "launch_hex": attacker.current_hex, "planned_hex": planned_hex,
            "target_hex": target.current_hex, "distance": distance,
            "distance_band": distance_band, "affected_area": "IMPACT_HEX_ONLY",
        },
        "indirect_context": {
            "observation_policy": observation_policy,
            "flight_time_seconds": profile.flight_time_seconds,
            "active_duration_seconds": profile.active_duration_seconds,
            "one_hit_per_unit": execution_policy.impact_policy.one_hit_per_unit,
            "scope": "SELECTED_TARGET_INITIAL_IMPACT_ONLY",
            "target_cell_and_profile_frozen": True,
            "observation_policy_is_assumption": True,
            "excluded_predictions": ["future_target_movement", "future_observation_changes",
                                     "mission_cancellation", "later_area_entrants", "seeded_rng"],
            "profile_id": profile.profile_id,
            "profile_hash": canonical_hash(asdict(profile)),
            "scatter_table_id": "indirect_fire." + _DISPERSION_TABLES[observation_policy],
            "artillery_class": profile.artillery_class.value,
            "ignored_target_profile_fields": ["suppressed", "close_combat"],
            "damage_policy_hash": damage_policy.content_hash,
            "execution_policy_hash": execution_policy.content_hash,
            "table_set_hash": tables.content_hash,
        },
    }
    if (profile.range_policy is IndirectRangePolicy.BOUNDED_HEX_DISTANCE
            and not profile.minimum_range <= distance <= profile.maximum_range):
        return {
            **context, **_summarize(Counter({(0, False, False): Fraction(1)}), target),
            "reason_code": "INDIRECT_OUT_OF_RANGE_ZERO_DAMAGE",
            "target_cell_affected_probability": 0.0,
            "target_cell_affected_probability_fraction": "0/1",
            "dispersion_table_hit_probability": 0.0,
            "out_of_map_probability": 0.0,
            "scatter_distribution": [],
            "enumeration": {"scatter_branches": 0, "damage_branches": 0},
        }
    indirect = tables.document.get("indirect_fire")
    if not isinstance(indirect, Mapping):
        return {**context, **_unresolved("INDIRECT_TABLES_UNAVAILABLE")}
    table_name = _DISPERSION_TABLES[observation_policy]
    dispersion = indirect.get(table_name)
    by_roll = dispersion.get("by_roll") if isinstance(dispersion, Mapping) else None
    if not isinstance(by_roll, Mapping):
        return {**context, **_unresolved("INDIRECT_SCATTER_TABLE_UNAVAILABLE")}
    scatter: Counter = Counter()
    # Reuse the service's band and table-result interpretation. Directional
    # traversal follows its exact neighbor order and out-of-map stopping rule.
    try:
        for total, weight in _TWO_D6:
            row = by_roll[str(total)]
            table_result = str(row[band_index] if isinstance(row, list) else row)
            steps = _scatter_distance(table_result)
            if steps > MAX_SCATTER_DISTANCE:
                return {**context, **_unresolved("INDIRECT_QUERY_BUDGET_EXCEEDED")}
            for direction in range(6):
                impact_hex = _scatter_impact_hex(source_map, planned_hex, direction, steps)
                scatter[(impact_hex, table_result, steps)] += Fraction(weight, 216)
    except (KeyError, IndexError, TypeError, ValueError):
        return {**context, **_unresolved("INDIRECT_SCATTER_TABLE_UNRESOLVED")}
    affected = sum((weight for (cell, _, _), weight in scatter.items()
                    if cell == target.current_hex), Fraction())
    coverage = {
        "target_cell_affected_probability": float(affected),
        "target_cell_affected_probability_fraction": _fraction_text(affected),
        "dispersion_table_hit_probability": float(sum(
            (weight for (_, result, _), weight in scatter.items() if result == "HIT"), Fraction(),
        )),
        "out_of_map_probability": float(sum(
            (weight for (cell, _, _), weight in scatter.items() if cell is None), Fraction(),
        )),
        "scatter_distribution": [
            {"impact_hex": cell, "table_result": result, "scatter_distance": steps,
             "out_of_map": cell is None, **_probability(weight)}
            for (cell, result, steps), weight in sorted(
                scatter.items(), key=lambda item: (-1 if item[0][0] is None else item[0][0], item[0][1]),
            )
        ],
    }
    outcomes: Counter = Counter({(0, False, False): 1 - affected})
    cache: dict[str, Counter] = {}
    modifiers: list[dict[str, Any]] = []
    for (cell, result, _), weight in scatter.items():
        if cell != target.current_hex:
            continue
        base_table = "hit_target_result" if result == "HIT" else "off_target_result"
        if base_table not in cache:
            distribution, modifiers, blockers = _damage_distribution(
                indirect, base_table=base_table, profile=profile,
                damage_policy=damage_policy, shooter_strength=attacker.blood,
                target=target, target_profile=target_profile,
                terrain=source_map.terrain(target.current_hex).value,
            )
            if blockers:
                return {**context, **coverage, **_unresolved("INDIRECT_DAMAGE_UNRESOLVED", blockers)}
            cache[base_table] = distribution
        for outcome, conditional_weight in cache[base_table].items():
            outcomes[outcome] += weight * conditional_weight
    return {
        **context, **coverage, **_summarize(outcomes, target),
        "target_modifiers": modifiers,
        "enumeration": {"scatter_branches": 66, "damage_branches": 66 * len(cache)},
    }


def _scatter_impact_hex(
    source_map: SourceHexMap, planned_hex: int, direction: int, steps: int,
) -> int | None:
    impact_hex = planned_hex
    for _ in range(steps):
        neighbor = int(source_map.cell(impact_hex).neighbors[direction])
        if neighbor < 0:
            return None
        try:
            source_map.cell(neighbor)
        except ValueError:
            return None
        impact_hex = neighbor
    return impact_hex


def _damage_distribution(
    indirect: Mapping[str, Any], *, base_table: str,
    profile: IndirectFireProfile, damage_policy: IndirectFireDamagePolicy,
    shooter_strength: int, target: OperatorState,
    target_profile: Mapping[str, Any], terrain: str,
) -> tuple[Counter, list[dict[str, Any]], tuple[str, ...]]:
    """Factor the production resolver's shared scalar calculators over 2d6+1d6."""
    if type(shooter_strength) is not int or shooter_strength not in range(1, 5):
        return Counter(), [], ("artillery_gun_count",)
    try:
        evidence = IndirectFireTargetEvidence(
            operator_id=target.operator_id, domain=UnitDomain(target_profile["domain"]),
            armor=ArmorClass(target_profile["armor_class"]), blood=target_profile["blood"],
            terrain=terrain, concealed=target_profile["concealed"], moving=target_profile["moving"],
            stacked=target_profile["stacked"], marching=target_profile["marching"],
        )
    except (KeyError, TypeError, ValueError):
        return Counter(), [], ("indirect_target_profile",)
    if evidence.domain not in {UnitDomain.PERSONNEL, UnitDomain.VEHICLE}:
        return Counter(), [], ("indirect_target_domain",)
    if evidence.domain is UnitDomain.VEHICLE and evidence.armor in {ArmorClass.NONE, ArmorClass.UNKNOWN}:
        return Counter(), [], ("unknown_vehicle_armor",)
    if terrain.upper() == "UNKNOWN":
        return Counter(), [], ("target_terrain",)
    document = indirect.get("result_modifiers")
    if not isinstance(document, Mapping):
        return Counter(), [], ("indirect_result_modifiers",)
    try:
        modifiers, blockers = _indirect_fire_modifiers(
            evidence, shooter_strength=shooter_strength, document=document, damage_policy=damage_policy,
        )
        records = [{"name": value.name, "value": value.value} for value in modifiers]
        if blockers:
            return Counter(), records, blockers
        by_roll = indirect[base_table]["by_roll"]
        outcomes: Counter = Counter()
        for total, weight in _TWO_D6:
            base = by_roll[str(total)][profile.artillery_class.value.lower()]
            for die in range(1, 7):
                correction = _indirect_fire_result_correction(die + sum(m.value for m in modifiers), document)
                if correction is None:
                    return Counter(), records, ("indirect_result_correction_table",)
                result, suppressed, destroyed = _apply_indirect_result_correction(
                    base, correction, evidence.blood, damage_policy,
                )
                loss = int(result.split("_", 1)[1]) if result.startswith("LOSS_") else 0
                outcomes[(loss, suppressed, destroyed)] += Fraction(weight, 216)
    except (KeyError, IndexError, TypeError, ValueError):
        return Counter(), [], ("indirect_damage_table",)
    return outcomes, records, ()


def _summarize(outcomes: Counter, target: OperatorState) -> dict[str, Any]:
    outcomes = Counter({key: weight for key, weight in outcomes.items() if weight})
    losses: Counter = Counter()
    for (loss, _, _), weight in outcomes.items():
        losses[loss] += weight
    personnel = target.operator_type.upper() == "PERSONNEL"
    return {
        "status": "RESOLVED", "reason_code": "EXACT_FAIR_D6_FACTORIZATION",
        "damage_probability": float(sum((weight for loss, weight in losses.items() if loss), Fraction())),
        "expected_damage": float(sum((loss * weight for loss, weight in losses.items()), Fraction())),
        "damage_unit": "blood_strength_loss",
        "suppression_probability": float(sum((weight for (_, suppression, _), weight in outcomes.items() if suppression), Fraction())),
        "destruction_probability": float(sum((weight for (_, _, destroyed), weight in outcomes.items() if destroyed), Fraction())),
        "damage_distribution": [{"losses": loss, **_probability(weight)} for loss, weight in sorted(losses.items())],
        "outcome_distribution": [
            {"vehicle_losses": 0 if personnel else loss, "squad_losses": loss if personnel else 0,
             "suppression_applied": suppression, "destroyed": destroyed,
             "no_effect": not loss and not suppression, **_probability(weight)}
            for (loss, suppression, destroyed), weight in sorted(outcomes.items())
        ],
    }


def _unresolved(reason: str, blockers: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "status": "UNRESOLVED", "reason_code": reason, "blockers": list(blockers),
        "hit_probability": None, "hit_probability_reason": "NO_SEPARATE_HIT_TEST",
        "distribution_condition": "launched_mission_frozen_target_initial_indirect_impact",
        "probability_basis": "independent_fair_d6",
        "can_execute_under_assumptions": None, "damage_probability": None,
        "expected_damage": None, "damage_distribution": [], "outcome_distribution": [],
    }


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _probability(value: Fraction) -> dict[str, Any]:
    return {"probability": float(value), "probability_fraction": _fraction_text(value)}
