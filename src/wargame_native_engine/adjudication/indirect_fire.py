"""Validated, non-mutating indirect-fire planning and impact contracts.

Impact resolution consumes only a caller-provided cloned NativeRNG.  This
module deliberately has no WorldState, damage, event, recorder, or replay
dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain
from wargame_native_engine.core.indirect_fire import FireMissionObservationEvidence
from wargame_native_engine.core.rng import NativeRNG
from wargame_native_engine.rules.indirect_fire import (
    ArtilleryClass,
    IndirectFireDamagePolicy,
    IndirectRangePolicy,
)


@dataclass(frozen=True, slots=True)
class IndirectFirePlanValidationEvidence:
    """Authority facts checked against one detached engine pre-state."""

    checked_profile_id: str
    checked_policy_hash: str
    planned_hex_validated: bool
    range_policy: IndirectRangePolicy

    def __post_init__(self) -> None:
        _require_nonempty(self.checked_profile_id, "checked_profile_id")
        _require_sha256(self.checked_policy_hash, "checked_policy_hash")
        if self.planned_hex_validated is not True:
            raise ValueError("planned_hex_validated must be true")
        if self.range_policy is not (
            IndirectRangePolicy.SCENARIO_DEFINED_GLOBAL_HEX
        ):
            raise ValueError("unsupported indirect-fire range policy")

    def to_record(self) -> dict[str, object]:
        return {
            "checked_profile_id": self.checked_profile_id,
            "checked_policy_hash": self.checked_policy_hash,
            "planned_hex_validated": self.planned_hex_validated,
            "range_policy": self.range_policy.value,
        }


@dataclass(frozen=True, slots=True, init=False)
class IndirectFirePlanRequest:
    """Detached evidence for a validated indirect-fire plan.

    The public constructor is disabled.  Only ``NativeEngine`` may revalidate
    a current capability and call the module-private factory.  ``ammo_before``
    records the complete, deterministically sorted ammunition inventory because
    the Phase C-1 authority does not yet define a weapon-to-ammunition key.

    This is not an action or mission and intentionally has no ``impact_hex``,
    scatter result, damage, or executable effect.
    """

    tick: int
    action_id: str
    shooter_id: int
    weapon_id: int
    planned_hex: int
    artillery_profile_id: str
    execution_policy_id: str
    execution_policy_hash: str
    ammo_before: tuple[tuple[str, int], ...]
    authority_version: str
    validation_evidence: IndirectFirePlanValidationEvidence

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("indirect_fire_plan_request_requires_engine_validation")

    @property
    def status(self) -> str:
        return "PLAN_ONLY"

    @property
    def executable(self) -> bool:
        return False

    def to_record(self) -> dict[str, object]:
        return {
            "tick": self.tick,
            "action_id": self.action_id,
            "shooter_id": self.shooter_id,
            "weapon_id": self.weapon_id,
            "planned_hex": self.planned_hex,
            "artillery_profile_id": self.artillery_profile_id,
            "execution_policy_id": self.execution_policy_id,
            "execution_policy_hash": self.execution_policy_hash,
            "ammo_before": [
                {"key": key, "amount": amount}
                for key, amount in self.ammo_before
            ],
            "authority_version": self.authority_version,
            "validation_evidence": self.validation_evidence.to_record(),
            "status": self.status,
            "executable": self.executable,
        }


@dataclass(frozen=True, slots=True)
class IndirectFirePlanValidationResult:
    """Stable success/failure result for server-side plan validation."""

    valid: bool
    reason: str
    blockers: tuple[str, ...]
    request: IndirectFirePlanRequest | None = None

    def __post_init__(self) -> None:
        if type(self.valid) is not bool:
            raise TypeError("valid must be an exact bool")
        _require_nonempty(self.reason, "reason")
        if (
            not isinstance(self.blockers, tuple)
            or any(not isinstance(item, str) or not item for item in self.blockers)
            or self.blockers != tuple(sorted(set(self.blockers)))
        ):
            raise ValueError("blockers must be sorted unique non-empty strings")
        if self.valid:
            if self.blockers or not isinstance(self.request, IndirectFirePlanRequest):
                raise ValueError("valid result requires one request and no blockers")
        elif self.request is not None or not self.blockers:
            raise ValueError("invalid result requires blockers and no request")


class IndirectFireObservationPolicy(str, Enum):
    UNRESOLVED = "UNRESOLVED"
    UNOBSERVED = "UNOBSERVED"
    GRID_CORRECTED = "GRID_CORRECTED"
    TARGET_CORRECTED = "TARGET_CORRECTED"


@dataclass(frozen=True, slots=True)
class IndirectFireRngState:
    seed: int
    state: int
    draws: int

    def __post_init__(self) -> None:
        for name in ("seed", "state", "draws"):
            _require_nonnegative_exact_int(getattr(self, name), name)

    def to_record(self) -> dict[str, int]:
        return {"seed": self.seed, "state": self.state, "draws": self.draws}


@dataclass(frozen=True, slots=True)
class IndirectFireRngTransition:
    before: IndirectFireRngState
    after: IndirectFireRngState

    def __post_init__(self) -> None:
        if not isinstance(self.before, IndirectFireRngState):
            raise TypeError("before must be IndirectFireRngState")
        if not isinstance(self.after, IndirectFireRngState):
            raise TypeError("after must be IndirectFireRngState")
        if self.before.seed != self.after.seed:
            raise ValueError("RNG seed cannot change during resolution")
        if self.after.draws < self.before.draws:
            raise ValueError("RNG draws cannot move backwards")

    @property
    def draw_count(self) -> int:
        return self.after.draws - self.before.draws

    def to_record(self) -> dict[str, object]:
        return {
            "before": self.before.to_record(),
            "after": self.after.to_record(),
            "draw_count": self.draw_count,
        }


@dataclass(frozen=True, slots=True)
class IndirectFireScatterResult:
    direction_roll: int
    direction_index: int
    distance_dice: tuple[int, int]
    distance_roll: int
    distance_band: str
    table_id: str
    table_result: str
    scatter_distance: int

    def __post_init__(self) -> None:
        if self.direction_roll not in range(1, 7):
            raise ValueError("direction_roll must be within 1..6")
        if self.direction_index != self.direction_roll - 1:
            raise ValueError("direction_index must map d6 1..6 to index 0..5")
        if (
            not isinstance(self.distance_dice, tuple)
            or len(self.distance_dice) != 2
            or any(value not in range(1, 7) for value in self.distance_dice)
        ):
            raise ValueError("distance_dice must contain exactly two d6 values")
        if self.distance_roll != sum(self.distance_dice):
            raise ValueError("distance_roll must equal the two dice total")
        for name in ("distance_band", "table_id", "table_result"):
            _require_nonempty(getattr(self, name), name)
        _require_nonnegative_exact_int(self.scatter_distance, "scatter_distance")


@dataclass(frozen=True, slots=True)
class IndirectFireResolutionEvidence:
    observation_policy: IndirectFireObservationPolicy
    spotter_evidence: str
    observation_evidence: FireMissionObservationEvidence
    affected_hex_policy: str
    operator_order: str
    pre_state_hash: str
    adjudication_table_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.observation_policy, IndirectFireObservationPolicy):
            raise TypeError("observation_policy must be IndirectFireObservationPolicy")
        if not isinstance(
            self.observation_evidence, FireMissionObservationEvidence
        ):
            raise TypeError(
                "observation_evidence must be FireMissionObservationEvidence"
            )
        if (
            self.observation_evidence.correction_level
            != self.observation_policy.value
        ):
            raise ValueError("observation evidence level mismatch")
        for name in ("spotter_evidence", "affected_hex_policy", "operator_order"):
            _require_nonempty(getattr(self, name), name)
        if self.affected_hex_policy != "IMPACT_HEX_ONLY":
            raise ValueError("only impact_hex may be affected")
        if self.operator_order != "OPERATOR_ID_ASCENDING":
            raise ValueError("affected operators must use operator_id order")
        _require_sha256(self.pre_state_hash, "pre_state_hash")
        _require_sha256(
            self.adjudication_table_hash,
            "adjudication_table_hash",
        )


@dataclass(frozen=True, slots=True, init=False)
class IndirectFireImpactRequest:
    mission_id: str
    tick: int
    shooter_id: int
    weapon_id: int
    planned_hex: int
    impact_hex: int
    scatter_result: IndirectFireScatterResult
    execution_policy_id: str
    execution_policy_hash: str
    rng_before: IndirectFireRngState
    rng_after: IndirectFireRngState
    spotter_evidence: str
    observation_policy: IndirectFireObservationPolicy

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("indirect_fire_impact_request_requires_engine_validation")


@dataclass(frozen=True, slots=True)
class IndirectFireResolution:
    mission_id: str
    shooter_id: int
    weapon_id: int
    planned_hex: int
    impact_hex: int
    execution_policy_id: str
    execution_policy_hash: str
    affected_operator_ids: tuple[int, ...]
    scatter_result: IndirectFireScatterResult
    rng_transition: IndirectFireRngTransition
    resolution_evidence: IndirectFireResolutionEvidence

    def __post_init__(self) -> None:
        _require_nonempty(self.mission_id, "mission_id")
        _require_nonempty(self.execution_policy_id, "execution_policy_id")
        _require_sha256(self.execution_policy_hash, "execution_policy_hash")
        for name in ("shooter_id", "weapon_id", "planned_hex", "impact_hex"):
            _require_nonnegative_exact_int(getattr(self, name), name)
        if (
            not isinstance(self.affected_operator_ids, tuple)
            or any(
                type(operator_id) is not int or operator_id < 0
                for operator_id in self.affected_operator_ids
            )
            or self.affected_operator_ids
            != tuple(sorted(set(self.affected_operator_ids)))
        ):
            raise ValueError("affected_operator_ids must be sorted unique ids")
        if not isinstance(self.rng_transition, IndirectFireRngTransition):
            raise TypeError("rng_transition must be IndirectFireRngTransition")
        if not isinstance(self.scatter_result, IndirectFireScatterResult):
            raise TypeError("scatter_result must be IndirectFireScatterResult")
        if not isinstance(
            self.resolution_evidence,
            IndirectFireResolutionEvidence,
        ):
            raise TypeError(
                "resolution_evidence must be IndirectFireResolutionEvidence"
            )


@dataclass(frozen=True, slots=True)
class IndirectFireMissResolution:
    """A fully sampled scatter result whose path leaves the map topology."""

    mission_id: str
    shooter_id: int
    weapon_id: int
    planned_hex: int
    outcome: str
    execution_policy_id: str
    execution_policy_hash: str
    scatter_result: IndirectFireScatterResult
    rng_transition: IndirectFireRngTransition
    resolution_evidence: IndirectFireResolutionEvidence

    def __post_init__(self) -> None:
        _require_nonempty(self.mission_id, "mission_id")
        _require_nonempty(self.execution_policy_id, "execution_policy_id")
        _require_sha256(self.execution_policy_hash, "execution_policy_hash")
        for name in ("shooter_id", "weapon_id", "planned_hex"):
            _require_nonnegative_exact_int(getattr(self, name), name)
        if self.outcome != "SCATTER_OUT_OF_MAP":
            raise ValueError("unsupported indirect-fire miss outcome")
        if not isinstance(self.scatter_result, IndirectFireScatterResult):
            raise TypeError("scatter_result must be IndirectFireScatterResult")
        if not isinstance(self.rng_transition, IndirectFireRngTransition):
            raise TypeError("rng_transition must be IndirectFireRngTransition")
        if not isinstance(
            self.resolution_evidence,
            IndirectFireResolutionEvidence,
        ):
            raise TypeError(
                "resolution_evidence must be IndirectFireResolutionEvidence"
            )


class IndirectFireEffectStatus(str, Enum):
    RESOLVED = "RESOLVED"
    UNRESOLVED_RULE = "UNRESOLVED_RULE"
    INVALID = "INVALID"


class IndirectFireTriState(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class IndirectFireModifier:
    name: str
    value: int

    def __post_init__(self) -> None:
        _require_nonempty(self.name, "modifier.name")
        if type(self.value) is not int:
            raise TypeError("modifier.value must be an exact int")


@dataclass(frozen=True, slots=True)
class IndirectFireTargetEvidence:
    operator_id: int
    domain: UnitDomain
    armor: ArmorClass
    blood: int
    terrain: str
    concealed: bool
    moving: bool
    stacked: bool
    marching: bool

    def __post_init__(self) -> None:
        _require_nonnegative_exact_int(self.operator_id, "operator_id")
        if not isinstance(self.domain, UnitDomain):
            raise TypeError("domain must be UnitDomain")
        if not isinstance(self.armor, ArmorClass):
            raise TypeError("armor must be ArmorClass")
        if type(self.blood) is not int or self.blood <= 0:
            raise ValueError("blood must be a positive exact int")
        _require_nonempty(self.terrain, "terrain")
        for name in ("concealed", "moving", "stacked", "marching"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be an exact bool")


@dataclass(frozen=True, slots=True)
class IndirectFireDamageResolutionEvidence:
    status: IndirectFireEffectStatus
    blockers: tuple[str, ...]
    base_table_id: str | None
    base_dice: tuple[int, int] | None
    base_result: str | None
    correction_die: int | None
    modified_roll: int | None
    result_correction: int | None
    table_content_hash: str
    rng_transition: IndirectFireRngTransition
    native_convention_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, IndirectFireEffectStatus):
            raise TypeError("status must be IndirectFireEffectStatus")
        _require_sorted_blockers(self.blockers)
        _require_sha256(self.table_content_hash, "table_content_hash")
        if not isinstance(self.rng_transition, IndirectFireRngTransition):
            raise TypeError("rng_transition must be IndirectFireRngTransition")
        if self.status is IndirectFireEffectStatus.RESOLVED:
            if self.blockers:
                raise ValueError("resolved damage evidence cannot have blockers")
            _require_nonempty(self.base_table_id, "base_table_id")
            if (
                not isinstance(self.base_dice, tuple)
                or len(self.base_dice) != 2
                or any(value not in range(1, 7) for value in self.base_dice)
            ):
                raise ValueError("base_dice must contain exactly two d6 values")
            _require_nonempty(self.base_result, "base_result")
            if self.correction_die not in range(1, 7):
                raise ValueError("correction_die must be a d6 value")
            if type(self.modified_roll) is not int:
                raise TypeError("modified_roll must be an exact int")
            if self.result_correction not in {-2, -1, 0, 1}:
                raise ValueError("result_correction is outside the authority table")
        elif not self.blockers:
            raise ValueError("unresolved damage evidence requires blockers")
        if (
            not isinstance(self.native_convention_flags, tuple)
            or any(
                not isinstance(value, str) or not value
                for value in self.native_convention_flags
            )
        ):
            raise ValueError("native_convention_flags must be non-empty strings")


@dataclass(frozen=True, slots=True)
class IndirectFireDamageEffect:
    mission_id: str
    operator_id: int
    impact_hex: int
    weapon_id: int
    artillery_class: ArtilleryClass
    damage_result: str
    suppression_result: IndirectFireTriState
    destroyed_result: IndirectFireTriState
    modifiers: tuple[IndirectFireModifier, ...]
    resolution_evidence: IndirectFireDamageResolutionEvidence

    def __post_init__(self) -> None:
        _require_nonempty(self.mission_id, "mission_id")
        for name in ("operator_id", "impact_hex", "weapon_id"):
            _require_nonnegative_exact_int(getattr(self, name), name)
        if not isinstance(self.artillery_class, ArtilleryClass):
            raise TypeError("artillery_class must be ArtilleryClass")
        _require_nonempty(self.damage_result, "damage_result")
        if not isinstance(self.suppression_result, IndirectFireTriState):
            raise TypeError("suppression_result must be IndirectFireTriState")
        if not isinstance(self.destroyed_result, IndirectFireTriState):
            raise TypeError("destroyed_result must be IndirectFireTriState")
        if (
            not isinstance(self.modifiers, tuple)
            or any(not isinstance(value, IndirectFireModifier) for value in self.modifiers)
        ):
            raise TypeError("modifiers must contain IndirectFireModifier values")
        if not isinstance(
            self.resolution_evidence,
            IndirectFireDamageResolutionEvidence,
        ):
            raise TypeError(
                "resolution_evidence must be IndirectFireDamageResolutionEvidence"
            )


@dataclass(frozen=True, slots=True)
class IndirectFireEffectBundle:
    """Pure affected-unit evidence with no damage or authoritative effect."""

    mission_id: str
    shooter_id: int
    weapon_id: int
    planned_hex: int
    impact_hex: int
    execution_policy_id: str
    execution_policy_hash: str
    affected_units: tuple[int, ...]
    scatter_result: IndirectFireScatterResult
    resolution_evidence: IndirectFireResolutionEvidence
    rng_transition: IndirectFireRngTransition
    damage_effects: tuple[IndirectFireDamageEffect, ...] = ()
    damage_rng_transition: IndirectFireRngTransition | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.mission_id, "mission_id")
        _require_nonempty(self.execution_policy_id, "execution_policy_id")
        _require_sha256(self.execution_policy_hash, "execution_policy_hash")
        for name in ("shooter_id", "weapon_id", "planned_hex", "impact_hex"):
            _require_nonnegative_exact_int(getattr(self, name), name)
        if (
            not isinstance(self.affected_units, tuple)
            or any(
                type(operator_id) is not int or operator_id < 0
                for operator_id in self.affected_units
            )
            or self.affected_units != tuple(sorted(set(self.affected_units)))
        ):
            raise ValueError("affected_units must be sorted unique ids")
        if not isinstance(self.rng_transition, IndirectFireRngTransition):
            raise TypeError("rng_transition must be IndirectFireRngTransition")
        if not isinstance(self.scatter_result, IndirectFireScatterResult):
            raise TypeError("scatter_result must be IndirectFireScatterResult")
        if not isinstance(
            self.resolution_evidence,
            IndirectFireResolutionEvidence,
        ):
            raise TypeError(
                "resolution_evidence must be IndirectFireResolutionEvidence"
            )
        if (
            not isinstance(self.damage_effects, tuple)
            or any(
                not isinstance(effect, IndirectFireDamageEffect)
                for effect in self.damage_effects
            )
            or tuple(effect.operator_id for effect in self.damage_effects)
            != tuple(sorted(effect.operator_id for effect in self.damage_effects))
        ):
            raise ValueError("damage_effects must use operator_id order")
        if self.damage_effects and (
            tuple(effect.operator_id for effect in self.damage_effects)
            != self.affected_units
        ):
            raise ValueError("damage_effects must cover affected_units exactly once")
        if self.damage_rng_transition is not None and not isinstance(
            self.damage_rng_transition,
            IndirectFireRngTransition,
        ):
            raise TypeError(
                "damage_rng_transition must be IndirectFireRngTransition or None"
            )


@dataclass(frozen=True, slots=True)
class IndirectFireEffectResolutionResult:
    status: IndirectFireEffectStatus
    blockers: tuple[str, ...]
    effect_bundle: IndirectFireEffectBundle | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, IndirectFireEffectStatus):
            raise TypeError("status must be IndirectFireEffectStatus")
        _require_sorted_blockers(self.blockers)
        if self.status is IndirectFireEffectStatus.RESOLVED:
            if self.blockers or not isinstance(
                self.effect_bundle,
                IndirectFireEffectBundle,
            ):
                raise ValueError("resolved effects require a bundle and no blockers")
        elif self.status is IndirectFireEffectStatus.UNRESOLVED_RULE:
            if not self.blockers or not isinstance(
                self.effect_bundle,
                IndirectFireEffectBundle,
            ):
                raise ValueError("unresolved rules require blockers and a bundle")
        elif self.effect_bundle is not None or not self.blockers:
            raise ValueError("invalid effects require blockers and no bundle")


@dataclass(frozen=True, slots=True)
class IndirectFireImpactValidationResult:
    valid: bool
    reason: str
    blockers: tuple[str, ...]
    request: IndirectFireImpactRequest | None = None
    resolution: IndirectFireResolution | None = None
    effect_bundle: IndirectFireEffectBundle | None = None
    miss_resolution: IndirectFireMissResolution | None = None

    def __post_init__(self) -> None:
        if type(self.valid) is not bool:
            raise TypeError("valid must be an exact bool")
        _require_nonempty(self.reason, "reason")
        if (
            not isinstance(self.blockers, tuple)
            or any(not isinstance(item, str) or not item for item in self.blockers)
            or self.blockers != tuple(sorted(set(self.blockers)))
        ):
            raise ValueError("blockers must be sorted unique non-empty strings")
        values = (self.request, self.resolution, self.effect_bundle)
        if self.valid:
            normal = (
                all(value is not None for value in values)
                and self.miss_resolution is None
            )
            miss = (
                all(value is None for value in values)
                and isinstance(
                    self.miss_resolution,
                    IndirectFireMissResolution,
                )
            )
            if self.blockers or normal is miss:
                raise ValueError(
                    "valid impact result requires one resolution contract"
                )
        elif (
            any(value is not None for value in values)
            or self.miss_resolution is not None
            or not self.blockers
        ):
            raise ValueError("invalid impact result requires blockers only")


def _build_validated_indirect_fire_plan_request(
    *,
    tick: int,
    action_id: str,
    shooter_id: int,
    weapon_id: int,
    planned_hex: int,
    artillery_profile_id: str,
    execution_policy_id: str,
    execution_policy_hash: str,
    ammo_before: tuple[tuple[str, int], ...],
    authority_version: str,
    validation_evidence: IndirectFirePlanValidationEvidence,
) -> IndirectFirePlanRequest:
    """Create a request only after NativeEngine has validated current facts."""

    for name, value in (
        ("tick", tick),
        ("shooter_id", shooter_id),
        ("weapon_id", weapon_id),
        ("planned_hex", planned_hex),
    ):
        _require_nonnegative_exact_int(value, name)
    for name, value in (
        ("action_id", action_id),
        ("artillery_profile_id", artillery_profile_id),
        ("execution_policy_id", execution_policy_id),
        ("authority_version", authority_version),
    ):
        _require_nonempty(value, name)
    _require_sha256(execution_policy_hash, "execution_policy_hash")
    if (
        not isinstance(ammo_before, tuple)
        or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not item[0]
            or type(item[1]) is not int
            or item[1] < 0
            for item in ammo_before
        )
        or ammo_before != tuple(sorted(ammo_before))
        or len({item[0] for item in ammo_before}) != len(ammo_before)
    ):
        raise ValueError("ammo_before must be a sorted unique immutable inventory")
    if not isinstance(validation_evidence, IndirectFirePlanValidationEvidence):
        raise TypeError(
            "validation_evidence must be IndirectFirePlanValidationEvidence"
        )
    if validation_evidence.checked_profile_id != artillery_profile_id:
        raise ValueError("validation profile evidence mismatch")
    if validation_evidence.checked_policy_hash != execution_policy_hash:
        raise ValueError("validation policy evidence mismatch")

    request = object.__new__(IndirectFirePlanRequest)
    values = {
        "tick": tick,
        "action_id": action_id,
        "shooter_id": shooter_id,
        "weapon_id": weapon_id,
        "planned_hex": planned_hex,
        "artillery_profile_id": artillery_profile_id,
        "execution_policy_id": execution_policy_id,
        "execution_policy_hash": execution_policy_hash,
        "ammo_before": tuple(ammo_before),
        "authority_version": authority_version,
        "validation_evidence": validation_evidence,
    }
    for name, value in values.items():
        object.__setattr__(request, name, value)
    return request


def _build_validated_indirect_fire_impact_request(
    *,
    mission_id: str,
    tick: int,
    shooter_id: int,
    weapon_id: int,
    planned_hex: int,
    impact_hex: int,
    scatter_result: IndirectFireScatterResult,
    execution_policy_id: str,
    execution_policy_hash: str,
    rng_before: IndirectFireRngState,
    rng_after: IndirectFireRngState,
    spotter_evidence: str,
    observation_policy: IndirectFireObservationPolicy,
) -> IndirectFireImpactRequest:
    for name, value in (
        ("mission_id", mission_id),
        ("execution_policy_id", execution_policy_id),
        ("spotter_evidence", spotter_evidence),
    ):
        _require_nonempty(value, name)
    for name, value in (
        ("tick", tick),
        ("shooter_id", shooter_id),
        ("weapon_id", weapon_id),
        ("planned_hex", planned_hex),
        ("impact_hex", impact_hex),
    ):
        _require_nonnegative_exact_int(value, name)
    _require_sha256(execution_policy_hash, "execution_policy_hash")
    if not isinstance(scatter_result, IndirectFireScatterResult):
        raise TypeError("scatter_result must be IndirectFireScatterResult")
    if not isinstance(rng_before, IndirectFireRngState):
        raise TypeError("rng_before must be IndirectFireRngState")
    if not isinstance(rng_after, IndirectFireRngState):
        raise TypeError("rng_after must be IndirectFireRngState")
    IndirectFireRngTransition(rng_before, rng_after)
    if not isinstance(observation_policy, IndirectFireObservationPolicy):
        raise TypeError("observation_policy must be IndirectFireObservationPolicy")

    request = object.__new__(IndirectFireImpactRequest)
    values = {
        "mission_id": mission_id,
        "tick": tick,
        "shooter_id": shooter_id,
        "weapon_id": weapon_id,
        "planned_hex": planned_hex,
        "impact_hex": impact_hex,
        "scatter_result": scatter_result,
        "execution_policy_id": execution_policy_id,
        "execution_policy_hash": execution_policy_hash,
        "rng_before": rng_before,
        "rng_after": rng_after,
        "spotter_evidence": spotter_evidence,
        "observation_policy": observation_policy,
    }
    for name, value in values.items():
        object.__setattr__(request, name, value)
    return request


def resolve_indirect_fire_effects(
    impact_resolution: IndirectFireResolution,
    *,
    shooter_id: int,
    weapon_id: int,
    artillery_class: ArtilleryClass,
    shooter_strength: int,
    targets: tuple[IndirectFireTargetEvidence, ...],
    table_document: Mapping[str, Any],
    table_content_hash: str,
    damage_policy: IndirectFireDamagePolicy,
) -> IndirectFireEffectResolutionResult:
    """Build deterministic, non-mutating damage effects after D-1 scatter.

    Each fully specified target consumes 2d6 for its base result followed by
    1d6 for result correction.  Targets are processed by operator id.  A
    Native simplified conventions are accepted only through the supplied,
    hashed damage policy; no missing table value is defaulted here.
    """

    if not isinstance(impact_resolution, IndirectFireResolution):
        raise TypeError("impact_resolution must be IndirectFireResolution")
    _require_nonnegative_exact_int(shooter_id, "shooter_id")
    _require_nonnegative_exact_int(weapon_id, "weapon_id")
    if not isinstance(artillery_class, ArtilleryClass):
        raise TypeError("artillery_class must be ArtilleryClass")
    if type(shooter_strength) is not int or shooter_strength <= 0:
        raise ValueError("shooter_strength must be a positive exact int")
    if not isinstance(damage_policy, IndirectFireDamagePolicy):
        raise TypeError("damage_policy must be IndirectFireDamagePolicy")
    _require_sha256(table_content_hash, "table_content_hash")
    if (
        not isinstance(targets, tuple)
        or any(not isinstance(value, IndirectFireTargetEvidence) for value in targets)
        or tuple(value.operator_id for value in targets)
        != tuple(sorted(value.operator_id for value in targets))
        or tuple(value.operator_id for value in targets)
        != impact_resolution.affected_operator_ids
    ):
        return _indirect_fire_effect_failure("affected_operator_evidence")
    if (
        impact_resolution.resolution_evidence.adjudication_table_hash
        != table_content_hash
    ):
        return _indirect_fire_effect_failure("adjudication_table_hash")
    indirect = table_document.get("indirect_fire")
    if not isinstance(indirect, Mapping):
        return _indirect_fire_effect_failure("indirect_fire_tables")
    modifiers_document = indirect.get("result_modifiers")
    if not isinstance(modifiers_document, Mapping):
        return _indirect_fire_effect_failure("indirect_result_modifiers")

    damage_rng = NativeRNG.restore(
        impact_resolution.rng_transition.after.to_record()
    )
    damage_before = _rng_state(damage_rng)
    effects: list[IndirectFireDamageEffect] = []
    aggregate_blockers: list[str] = []
    for target in targets:
        effect = _resolve_indirect_fire_target_effect(
            impact_resolution,
            target=target,
            shooter_id=shooter_id,
            weapon_id=weapon_id,
            artillery_class=artillery_class,
            shooter_strength=shooter_strength,
            indirect=indirect,
            modifiers_document=modifiers_document,
            table_content_hash=table_content_hash,
            damage_policy=damage_policy,
            rng=damage_rng,
        )
        effects.append(effect)
        aggregate_blockers.extend(
            f"operator:{target.operator_id}:{blocker}"
            for blocker in effect.resolution_evidence.blockers
        )

    damage_after = _rng_state(damage_rng)
    damage_transition = IndirectFireRngTransition(damage_before, damage_after)
    total_transition = IndirectFireRngTransition(
        impact_resolution.rng_transition.before,
        damage_after,
    )
    bundle = IndirectFireEffectBundle(
        mission_id=impact_resolution.mission_id,
        shooter_id=impact_resolution.shooter_id,
        weapon_id=impact_resolution.weapon_id,
        planned_hex=impact_resolution.planned_hex,
        impact_hex=impact_resolution.impact_hex,
        execution_policy_id=impact_resolution.execution_policy_id,
        execution_policy_hash=impact_resolution.execution_policy_hash,
        affected_units=impact_resolution.affected_operator_ids,
        scatter_result=impact_resolution.scatter_result,
        resolution_evidence=impact_resolution.resolution_evidence,
        rng_transition=total_transition,
        damage_effects=tuple(effects),
        damage_rng_transition=damage_transition,
    )
    blockers = tuple(sorted(set(aggregate_blockers)))
    return IndirectFireEffectResolutionResult(
        status=(
            IndirectFireEffectStatus.UNRESOLVED_RULE
            if blockers
            else IndirectFireEffectStatus.RESOLVED
        ),
        blockers=blockers,
        effect_bundle=bundle,
    )


def _resolve_indirect_fire_target_effect(
    impact_resolution: IndirectFireResolution,
    *,
    target: IndirectFireTargetEvidence,
    shooter_id: int,
    weapon_id: int,
    artillery_class: ArtilleryClass,
    shooter_strength: int,
    indirect: Mapping[str, Any],
    modifiers_document: Mapping[str, Any],
    table_content_hash: str,
    damage_policy: IndirectFireDamagePolicy,
    rng: NativeRNG,
) -> IndirectFireDamageEffect:
    del shooter_id  # retained in the contract for future firing-unit modifiers
    before = _rng_state(rng)
    blockers: list[str] = []
    if target.domain is UnitDomain.VEHICLE and target.armor in {
        ArmorClass.NONE,
        ArmorClass.UNKNOWN,
    }:
        blockers.append("unknown_vehicle_armor")
    elif target.domain not in {UnitDomain.PERSONNEL, UnitDomain.VEHICLE}:
        blockers.append("indirect_target_domain")
    if shooter_strength not in range(1, 5):
        blockers.append("artillery_gun_count")
    if target.terrain.upper() == "UNKNOWN":
        blockers.append("target_terrain")
    if blockers:
        return _unresolved_indirect_fire_damage_effect(
            impact_resolution,
            target=target,
            weapon_id=weapon_id,
            artillery_class=artillery_class,
            table_content_hash=table_content_hash,
            rng_state=before,
            blockers=tuple(sorted(set(blockers))),
        )

    base_table_name = (
        "hit_target_result"
        if impact_resolution.scatter_result.table_result == "HIT"
        else "off_target_result"
    )
    base_table = indirect.get(base_table_name)
    by_roll = base_table.get("by_roll") if isinstance(base_table, Mapping) else None
    if not isinstance(by_roll, Mapping):
        return _unresolved_indirect_fire_damage_effect(
            impact_resolution,
            target=target,
            weapon_id=weapon_id,
            artillery_class=artillery_class,
            table_content_hash=table_content_hash,
            rng_state=before,
            blockers=("indirect_base_result_table",),
        )

    base_dice = (rng.randbelow(6) + 1, rng.randbelow(6) + 1)
    base_roll = sum(base_dice)
    row = by_roll.get(str(base_roll))
    class_key = artillery_class.value.lower()
    base_value = row.get(class_key) if isinstance(row, Mapping) else None
    correction_die = rng.randbelow(6) + 1
    modifiers, modifier_blockers = _indirect_fire_modifiers(
        target,
        shooter_strength=shooter_strength,
        document=modifiers_document,
        damage_policy=damage_policy,
    )
    modified_roll = correction_die + sum(value.value for value in modifiers)
    correction = _indirect_fire_result_correction(
        modified_roll,
        modifiers_document,
    )
    after = _rng_state(rng)
    if base_value is None:
        modifier_blockers = (*modifier_blockers, "indirect_base_result_cell")
    if correction is None:
        modifier_blockers = (*modifier_blockers, "indirect_result_correction_table")
    blockers = tuple(sorted(set(modifier_blockers)))
    base_result = None if base_value is None else str(base_value)
    transition = IndirectFireRngTransition(before, after)
    if blockers:
        evidence = IndirectFireDamageResolutionEvidence(
            status=IndirectFireEffectStatus.UNRESOLVED_RULE,
            blockers=blockers,
            base_table_id=f"indirect_fire.{base_table_name}",
            base_dice=base_dice,
            base_result=base_result,
            correction_die=correction_die,
            modified_roll=modified_roll,
            result_correction=correction,
            table_content_hash=table_content_hash,
            rng_transition=transition,
        )
        return IndirectFireDamageEffect(
            mission_id=impact_resolution.mission_id,
            operator_id=target.operator_id,
            impact_hex=impact_resolution.impact_hex,
            weapon_id=weapon_id,
            artillery_class=artillery_class,
            damage_result=IndirectFireEffectStatus.UNRESOLVED_RULE.value,
            suppression_result=IndirectFireTriState.UNRESOLVED,
            destroyed_result=IndirectFireTriState.UNRESOLVED,
            modifiers=modifiers,
            resolution_evidence=evidence,
        )

    damage_result, suppression, destroyed = _apply_indirect_result_correction(
        base_value,
        int(correction),
        target.blood,
        damage_policy,
    )
    evidence = IndirectFireDamageResolutionEvidence(
        status=IndirectFireEffectStatus.RESOLVED,
        blockers=(),
        base_table_id=f"indirect_fire.{base_table_name}",
        base_dice=base_dice,
        base_result=str(base_value),
        correction_die=correction_die,
        modified_roll=modified_roll,
        result_correction=int(correction),
        table_content_hash=table_content_hash,
        rng_transition=transition,
        native_convention_flags=(
            f"INDIRECT_DAMAGE_POLICY:{damage_policy.content_hash}",
            damage_policy.symbolic_authority_basis,
            damage_policy.unarmored_authority_basis,
        ),
    )
    return IndirectFireDamageEffect(
        mission_id=impact_resolution.mission_id,
        operator_id=target.operator_id,
        impact_hex=impact_resolution.impact_hex,
        weapon_id=weapon_id,
        artillery_class=artillery_class,
        damage_result=damage_result,
        suppression_result=(
            IndirectFireTriState.TRUE
            if suppression
            else IndirectFireTriState.FALSE
        ),
        destroyed_result=(
            IndirectFireTriState.TRUE
            if destroyed
            else IndirectFireTriState.FALSE
        ),
        modifiers=modifiers,
        resolution_evidence=evidence,
    )


def _unresolved_indirect_fire_damage_effect(
    impact_resolution: IndirectFireResolution,
    *,
    target: IndirectFireTargetEvidence,
    weapon_id: int,
    artillery_class: ArtilleryClass,
    table_content_hash: str,
    rng_state: IndirectFireRngState,
    blockers: tuple[str, ...],
) -> IndirectFireDamageEffect:
    evidence = IndirectFireDamageResolutionEvidence(
        status=IndirectFireEffectStatus.UNRESOLVED_RULE,
        blockers=blockers,
        base_table_id=None,
        base_dice=None,
        base_result=None,
        correction_die=None,
        modified_roll=None,
        result_correction=None,
        table_content_hash=table_content_hash,
        rng_transition=IndirectFireRngTransition(rng_state, rng_state),
    )
    return IndirectFireDamageEffect(
        mission_id=impact_resolution.mission_id,
        operator_id=target.operator_id,
        impact_hex=impact_resolution.impact_hex,
        weapon_id=weapon_id,
        artillery_class=artillery_class,
        damage_result=IndirectFireEffectStatus.UNRESOLVED_RULE.value,
        suppression_result=IndirectFireTriState.UNRESOLVED,
        destroyed_result=IndirectFireTriState.UNRESOLVED,
        modifiers=(),
        resolution_evidence=evidence,
    )


def _indirect_fire_modifiers(
    target: IndirectFireTargetEvidence,
    *,
    shooter_strength: int,
    document: Mapping[str, Any],
    damage_policy: IndirectFireDamagePolicy,
) -> tuple[tuple[IndirectFireModifier, ...], tuple[str, ...]]:
    groups = document.get("roll_modifiers")
    if not isinstance(groups, Mapping):
        return (), ("indirect_roll_modifiers",)
    terrain_group = groups.get("target_terrain")
    state_group = groups.get("target_state")
    armor_group = groups.get("armor")
    gun_group = groups.get("gun_count")
    if not all(
        isinstance(value, Mapping)
        for value in (terrain_group, state_group, armor_group, gun_group)
    ):
        return (), ("indirect_roll_modifiers",)

    values: list[IndirectFireModifier] = []
    terrain = target.terrain.upper()
    if terrain in {"SETTLEMENT", "RESIDENTIAL"}:
        values.append(IndirectFireModifier(
            "target_residential",
            int(terrain_group["residential"]),
        ))
    elif terrain == "FOREST":
        values.append(IndirectFireModifier(
            "target_forest",
            int(terrain_group["forest"]),
        ))
    if target.concealed:
        values.append(IndirectFireModifier(
            "target_concealed",
            int(state_group["concealed"]),
        ))
    if target.domain is UnitDomain.PERSONNEL and target.moving:
        values.append(IndirectFireModifier(
            "target_moving_personnel",
            int(state_group["moving_personnel"]),
        ))
    if target.stacked:
        values.append(IndirectFireModifier(
            "target_stacked",
            int(state_group["stacked"]),
        ))
    if target.marching:
        values.append(IndirectFireModifier(
            "target_marching",
            int(state_group["marching"]),
        ))
    if target.domain is UnitDomain.PERSONNEL:
        values.append(IndirectFireModifier(
            "target_unarmored_explicit",
            damage_policy.unarmored_target_modifier,
        ))
    elif target.armor in {ArmorClass.NONE, ArmorClass.UNKNOWN}:
        return tuple(values), ("unknown_vehicle_armor",)
    else:
        armor_value = armor_group.get(target.armor.value.lower())
        if type(armor_value) is not int:
            return tuple(values), ("indirect_armor_modifier",)
        values.append(IndirectFireModifier(
            f"target_armor_{target.armor.value.lower()}",
            armor_value,
        ))
    values.append(IndirectFireModifier(
        "artillery_gun_count",
        int(gun_group[str(shooter_strength)]),
    ))
    return tuple(values), ()


def _indirect_fire_result_correction(
    modified_roll: int,
    document: Mapping[str, Any],
) -> int | None:
    rows = document.get("result_correction_by_modified_roll")
    if not isinstance(rows, list):
        return None
    expected = {"<=1": -2, "2..4": -1, "5..7": 0, ">=8": 1}
    actual = {
        str(row.get("condition")): row.get("correction")
        for row in rows
        if isinstance(row, Mapping)
    }
    if actual != expected:
        return None
    if modified_roll <= 1:
        return -2
    if modified_roll <= 4:
        return -1
    if modified_roll <= 7:
        return 0
    return 1


def _apply_indirect_result_correction(
    base_value: object,
    correction: int,
    target_blood: int,
    policy: IndirectFireDamagePolicy,
) -> tuple[str, bool, bool]:
    if type(base_value) is int:
        losses = max(0, min(int(base_value) + correction, target_blood))
        return (
            "NO_EFFECT" if losses == 0 else f"LOSS_{losses}",
            False,
            losses >= target_blood,
        )
    if base_value not in {"SUPPRESS", "NO_EFFECT"}:
        raise ValueError("unknown indirect-fire base result")
    ladder = tuple(
        value
        for value in policy.symbolic_ordered_results
        if not value.startswith("LOSS_")
        or int(value.split("_", 1)[1]) <= target_blood
    )
    index = ladder.index(str(base_value))
    result = ladder[max(0, min(index + correction, len(ladder) - 1))]
    if result == "NO_EFFECT":
        return result, False, False
    if result == "SUPPRESS":
        return result, True, False
    losses = int(result.split("_", 1)[1])
    return result, False, losses >= target_blood


def _rng_state(rng: NativeRNG) -> IndirectFireRngState:
    value = rng.snapshot()
    return IndirectFireRngState(
        seed=int(value["seed"]),
        state=int(value["state"]),
        draws=int(value["draws"]),
    )


def _indirect_fire_effect_failure(
    *blockers: str,
) -> IndirectFireEffectResolutionResult:
    return IndirectFireEffectResolutionResult(
        status=IndirectFireEffectStatus.INVALID,
        blockers=tuple(sorted(set(blockers))),
        effect_bundle=None,
    )


def _require_nonnegative_exact_int(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative exact int")


def _require_nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _require_sha256(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256")


def _require_sorted_blockers(blockers: object) -> None:
    if (
        not isinstance(blockers, tuple)
        or any(not isinstance(item, str) or not item for item in blockers)
        or blockers != tuple(sorted(set(blockers)))
    ):
        raise ValueError("blockers must be sorted unique non-empty strings")


__all__ = [
    "IndirectFireDamageEffect",
    "IndirectFireDamageResolutionEvidence",
    "IndirectFireEffectBundle",
    "IndirectFireEffectResolutionResult",
    "IndirectFireEffectStatus",
    "IndirectFireImpactRequest",
    "IndirectFireImpactValidationResult",
    "IndirectFireMissResolution",
    "IndirectFireObservationPolicy",
    "IndirectFirePlanRequest",
    "IndirectFirePlanValidationEvidence",
    "IndirectFirePlanValidationResult",
    "IndirectFireResolution",
    "IndirectFireResolutionEvidence",
    "IndirectFireRngState",
    "IndirectFireRngTransition",
    "IndirectFireScatterResult",
    "IndirectFireTargetEvidence",
    "IndirectFireTriState",
    "resolve_indirect_fire_effects",
]
