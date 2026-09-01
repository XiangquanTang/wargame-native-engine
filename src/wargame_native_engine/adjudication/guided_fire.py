"""Validated, non-executable guided-fire request contracts."""

from __future__ import annotations

from dataclasses import dataclass

from wargame_native_engine.adjudication.dice import DiceRoller
from wargame_native_engine.adjudication.direct_fire import DirectFireAdjudicator
from wargame_native_engine.adjudication.request import DirectFireRequest
from wargame_native_engine.adjudication.table_loader import AdjudicationTableSet
from wargame_native_engine.core.event import EngineEvent
from wargame_native_engine.core.fire_capability import GuidedFireCapability
from wargame_native_engine.core.rng import NativeRNG
from wargame_native_engine.visibility import VisibilityResult


@dataclass(frozen=True, slots=True, init=False)
class GuidedFireRequest:
    """Detached evidence for one currently valid guided-fire opportunity.

    The public constructor is deliberately disabled.  Native authority must
    revalidate a :class:`GuidedFireCapability` against its current detached
    world snapshot and use the module-private factory below.  This request is
    preview-only: it is neither an executable action nor adjudication evidence.

    ``cooldown_before`` is expressed in authoritative milliseconds, matching
    ``OperatorState.weapon_cooldown_remaining_ms``.
    """

    tick: int
    guider_id: int
    shooter_id: int
    weapon_id: int
    target_id: int
    guidance_profile_id: str
    guider_visibility_evidence: VisibilityResult
    shooter_distance: int
    ammunition_before: int
    cooldown_before: int

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("guided_fire_request_requires_validated_capability")

    @property
    def status(self) -> str:
        return "PREVIEW_ONLY"

    @property
    def executed(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class GuidedFireValidationResult:
    """Pure validation result produced from one current pre-state."""

    valid: bool
    reason: str
    request: GuidedFireRequest

    def __post_init__(self) -> None:
        if type(self.valid) is not bool:
            raise TypeError("valid must be an exact bool")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")
        if not isinstance(self.request, GuidedFireRequest):
            raise TypeError("request must be GuidedFireRequest")


@dataclass(frozen=True, slots=True)
class GuidedFireRecoveryUpdate:
    operator_id: int
    recovery_remaining_ms: int

    def __post_init__(self) -> None:
        _require_nonnegative_exact_int(self.operator_id, "operator_id")
        _require_nonnegative_exact_int(
            self.recovery_remaining_ms,
            "recovery_remaining_ms",
        )


@dataclass(frozen=True, slots=True)
class GuidedFireEffectPlan:
    """Non-mutating resource-effect plan for a validated request.

    The future damage stage is required to reuse the selected weapon's existing
    DirectFire effect model; GUIDED changes target acquisition, not damage.
    Damage, RNG, events, cooldown application, and authoritative commit remain
    outside this Phase 4A contract.
    """

    shooter_id: int
    guider_id: int
    weapon_id: int
    target_id: int
    ammo_consume: int
    recovery_updates: tuple[GuidedFireRecoveryUpdate, ...]

    def __post_init__(self) -> None:
        for name in ("shooter_id", "guider_id", "weapon_id", "target_id"):
            _require_nonnegative_exact_int(getattr(self, name), name)
        _require_nonnegative_exact_int(self.ammo_consume, "ammo_consume")
        if not isinstance(self.recovery_updates, tuple) or any(
            not isinstance(item, GuidedFireRecoveryUpdate)
            for item in self.recovery_updates
        ):
            raise TypeError(
                "recovery_updates must be a tuple of GuidedFireRecoveryUpdate"
            )
        operator_ids = tuple(item.operator_id for item in self.recovery_updates)
        if operator_ids != tuple(sorted(set(operator_ids))):
            raise ValueError(
                "recovery_updates must have unique deterministic operator order"
            )


@dataclass(frozen=True, slots=True)
class GuidedFireDamageEffect:
    target_id: int
    vehicle_losses: int
    squad_losses: int
    suppression_applied: bool
    destroyed: bool
    no_effect: bool

    def __post_init__(self) -> None:
        for name in ("target_id", "vehicle_losses", "squad_losses"):
            _require_nonnegative_exact_int(getattr(self, name), name)
        for name in ("suppression_applied", "destroyed", "no_effect"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be an exact bool")


@dataclass(frozen=True, slots=True)
class GuidedFireResourceEffect:
    shooter_id: int
    ammunition_key: str
    ammunition_before: int
    ammunition_after: int
    cooldown_before_ms: int
    cooldown_after_ms: int
    recovery_updates: tuple[GuidedFireRecoveryUpdate, ...]

    def __post_init__(self) -> None:
        for name in (
            "shooter_id",
            "ammunition_before",
            "ammunition_after",
            "cooldown_before_ms",
            "cooldown_after_ms",
        ):
            _require_nonnegative_exact_int(getattr(self, name), name)
        if not isinstance(self.ammunition_key, str) or not self.ammunition_key:
            raise ValueError("ammunition_key must be a non-empty string")
        if not isinstance(self.recovery_updates, tuple) or any(
            not isinstance(item, GuidedFireRecoveryUpdate)
            for item in self.recovery_updates
        ):
            raise TypeError(
                "recovery_updates must be a tuple of GuidedFireRecoveryUpdate"
            )


@dataclass(frozen=True, slots=True)
class GuidedFireRngEffect:
    seed: int
    state_before: int
    draws_before: int
    state_after: int
    draws_after: int

    def __post_init__(self) -> None:
        for name in (
            "seed",
            "state_before",
            "draws_before",
            "state_after",
            "draws_after",
        ):
            _require_nonnegative_exact_int(getattr(self, name), name)
        if self.draws_after < self.draws_before:
            raise ValueError("RNG draws cannot move backwards")


@dataclass(frozen=True, slots=True)
class EffectBundle:
    """Detached effects awaiting the existing atomic commit boundary."""

    damage_effects: tuple[GuidedFireDamageEffect, ...]
    resource_effects: tuple[GuidedFireResourceEffect, ...]
    rng_effect: GuidedFireRngEffect

    def __post_init__(self) -> None:
        if not isinstance(self.damage_effects, tuple) or any(
            not isinstance(item, GuidedFireDamageEffect)
            for item in self.damage_effects
        ):
            raise TypeError(
                "damage_effects must be a tuple of GuidedFireDamageEffect"
            )
        if not isinstance(self.resource_effects, tuple) or any(
            not isinstance(item, GuidedFireResourceEffect)
            for item in self.resource_effects
        ):
            raise TypeError(
                "resource_effects must be a tuple of GuidedFireResourceEffect"
            )
        if not isinstance(self.rng_effect, GuidedFireRngEffect):
            raise TypeError("rng_effect must be GuidedFireRngEffect")


@dataclass(frozen=True, slots=True)
class GuidedFireResolutionEvent:
    guider_id: int
    shooter_id: int
    weapon_id: int
    target_id: int
    hit: bool
    damage_summary: tuple[GuidedFireDamageEffect, ...]

    def __post_init__(self) -> None:
        for name in ("guider_id", "shooter_id", "weapon_id", "target_id"):
            _require_nonnegative_exact_int(getattr(self, name), name)
        if type(self.hit) is not bool:
            raise TypeError("hit must be an exact bool")
        if not isinstance(self.damage_summary, tuple) or any(
            not isinstance(item, GuidedFireDamageEffect)
            for item in self.damage_summary
        ):
            raise TypeError(
                "damage_summary must be a tuple of GuidedFireDamageEffect"
            )


@dataclass(frozen=True, slots=True)
class FireResolution:
    guider_id: int
    shooter_id: int
    weapon_id: int
    target_id: int
    hit_result: bool
    damage_effects: tuple[GuidedFireDamageEffect, ...]
    resource_effects: tuple[GuidedFireResourceEffect, ...]
    rng_effect: GuidedFireRngEffect
    resolution_event: GuidedFireResolutionEvent
    adjudication_evidence: tuple[EngineEvent, ...]
    adjudication_id: str
    error: str | None = None

    def __post_init__(self) -> None:
        for name in ("guider_id", "shooter_id", "weapon_id", "target_id"):
            _require_nonnegative_exact_int(getattr(self, name), name)
        if type(self.hit_result) is not bool:
            raise TypeError("hit_result must be an exact bool")
        if not isinstance(self.adjudication_id, str) or not self.adjudication_id:
            raise ValueError("adjudication_id must be a non-empty string")
        if self.error is not None and (
            not isinstance(self.error, str) or not self.error
        ):
            raise ValueError("error must be a non-empty string or None")
        EffectBundle(
            self.damage_effects,
            self.resource_effects,
            self.rng_effect,
        )
        if not isinstance(self.resolution_event, GuidedFireResolutionEvent):
            raise TypeError(
                "resolution_event must be GuidedFireResolutionEvent"
            )
        if not isinstance(self.adjudication_evidence, tuple) or any(
            not isinstance(item, EngineEvent)
            for item in self.adjudication_evidence
        ):
            raise TypeError(
                "adjudication_evidence must be a tuple of EngineEvent"
            )

    @property
    def effect_bundle(self) -> EffectBundle:
        return EffectBundle(
            self.damage_effects,
            self.resource_effects,
            self.rng_effect,
        )


@dataclass(frozen=True, slots=True)
class GuidedFireStageResult:
    """Complete detached result of routing one GUIDED fire intent.

    This is deliberately not an Engine event or commit instruction.  It binds
    the request, its same-pre-state validation, the deterministic resolution,
    and the staged effects so a future joint commit phase cannot accidentally
    mix evidence produced from different snapshots.
    """

    request: GuidedFireRequest
    validation: GuidedFireValidationResult
    resolution: FireResolution
    effect_bundle: EffectBundle

    def __post_init__(self) -> None:
        if not isinstance(self.request, GuidedFireRequest):
            raise TypeError("request must be GuidedFireRequest")
        if not isinstance(self.validation, GuidedFireValidationResult):
            raise TypeError("validation must be GuidedFireValidationResult")
        if not isinstance(self.resolution, FireResolution):
            raise TypeError("resolution must be FireResolution")
        if not isinstance(self.effect_bundle, EffectBundle):
            raise TypeError("effect_bundle must be EffectBundle")
        if self.validation.request is not self.request:
            raise ValueError("GUIDED_FIRE_STAGE_VALIDATION_REQUEST_MISMATCH")
        if not self.validation.valid:
            raise ValueError("GUIDED_FIRE_STAGE_REQUIRES_VALID_VALIDATION")
        if (
            self.resolution.guider_id != self.request.guider_id
            or self.resolution.shooter_id != self.request.shooter_id
            or self.resolution.weapon_id != self.request.weapon_id
            or self.resolution.target_id != self.request.target_id
        ):
            raise ValueError("GUIDED_FIRE_STAGE_RESOLUTION_REQUEST_MISMATCH")
        if self.effect_bundle != self.resolution.effect_bundle:
            raise ValueError("GUIDED_FIRE_STAGE_EFFECT_BUNDLE_MISMATCH")


@dataclass(frozen=True, slots=True)
class GuidedFireCommitEffect:
    """Frozen GUIDED effects ready for the Engine working-state transaction."""

    guider_id: int
    shooter_id: int
    weapon_id: int
    target_id: int
    damage_effects: tuple[GuidedFireDamageEffect, ...]
    ammunition_key: str
    ammunition_before: int
    ammunition_after: int
    cooldown_before_ms: int
    cooldown_after_ms: int
    recovery_updates: tuple[GuidedFireRecoveryUpdate, ...]
    rng_transition: GuidedFireRngEffect
    evidence: tuple[EngineEvent, ...]
    hit: bool
    adjudication_id: str

    def __post_init__(self) -> None:
        for name in ("guider_id", "shooter_id", "weapon_id", "target_id"):
            _require_nonnegative_exact_int(getattr(self, name), name)
        if not isinstance(self.damage_effects, tuple) or any(
            not isinstance(item, GuidedFireDamageEffect)
            for item in self.damage_effects
        ):
            raise TypeError(
                "damage_effects must be a tuple of GuidedFireDamageEffect"
            )
        if not isinstance(self.ammunition_key, str) or not self.ammunition_key:
            raise ValueError("ammunition_key must be a non-empty string")
        for name in (
            "ammunition_before",
            "ammunition_after",
            "cooldown_before_ms",
            "cooldown_after_ms",
        ):
            _require_nonnegative_exact_int(getattr(self, name), name)
        if self.ammunition_after > self.ammunition_before:
            raise ValueError("guided fire cannot increase ammunition")
        if not isinstance(self.recovery_updates, tuple) or any(
            not isinstance(item, GuidedFireRecoveryUpdate)
            for item in self.recovery_updates
        ):
            raise TypeError(
                "recovery_updates must be a tuple of GuidedFireRecoveryUpdate"
            )
        if not isinstance(self.rng_transition, GuidedFireRngEffect):
            raise TypeError("rng_transition must be GuidedFireRngEffect")
        if not isinstance(self.evidence, tuple) or any(
            not isinstance(item, EngineEvent) for item in self.evidence
        ):
            raise TypeError("evidence must be a tuple of EngineEvent")
        if type(self.hit) is not bool:
            raise TypeError("hit must be an exact bool")
        if not isinstance(self.adjudication_id, str) or not self.adjudication_id:
            raise ValueError("adjudication_id must be a non-empty string")

    @classmethod
    def from_stage(
        cls,
        stage: GuidedFireStageResult,
    ) -> "GuidedFireCommitEffect":
        if not isinstance(stage, GuidedFireStageResult):
            raise TypeError("stage must be GuidedFireStageResult")
        resources = stage.effect_bundle.resource_effects
        if len(resources) != 1:
            raise ValueError("GUIDED_FIRE_COMMIT_REQUIRES_ONE_RESOURCE_EFFECT")
        resource = resources[0]
        return cls(
            guider_id=stage.request.guider_id,
            shooter_id=stage.request.shooter_id,
            weapon_id=stage.request.weapon_id,
            target_id=stage.request.target_id,
            damage_effects=stage.effect_bundle.damage_effects,
            ammunition_key=resource.ammunition_key,
            ammunition_before=resource.ammunition_before,
            ammunition_after=resource.ammunition_after,
            cooldown_before_ms=resource.cooldown_before_ms,
            cooldown_after_ms=resource.cooldown_after_ms,
            recovery_updates=resource.recovery_updates,
            rng_transition=stage.effect_bundle.rng_effect,
            evidence=stage.resolution.adjudication_evidence,
            hit=stage.resolution.hit_result,
            adjudication_id=stage.resolution.adjudication_id,
        )


class GuidedFireResolver:
    """Resolve one validated GUIDED request on detached runtime objects.

    The resolver delegates all weapon-73 hit, armor, damage, and dice behavior
    to ``DirectFireAdjudicator.stage``.  The supplied RNG must be a detached
    clone; its exact transition is returned in the EffectBundle for a future
    atomic commit.
    """

    def __init__(
        self,
        tables: AdjudicationTableSet,
        rng: NativeRNG,
    ) -> None:
        if not isinstance(tables, AdjudicationTableSet):
            raise TypeError("tables must be AdjudicationTableSet")
        if not isinstance(rng, NativeRNG):
            raise TypeError("rng must be NativeRNG")
        self._adjudicator = DirectFireAdjudicator(tables, DiceRoller(rng))
        self._rng = rng

    def resolve(
        self,
        validation: GuidedFireValidationResult,
        direct_request: DirectFireRequest,
        effect_plan: GuidedFireEffectPlan,
    ) -> FireResolution:
        if not isinstance(validation, GuidedFireValidationResult):
            raise TypeError("validation must be GuidedFireValidationResult")
        if not validation.valid:
            raise ValueError(
                f"GUIDED_FIRE_VALIDATION_FAILED:{validation.reason}"
            )
        if not isinstance(direct_request, DirectFireRequest):
            raise TypeError("direct_request must be DirectFireRequest")
        if not isinstance(effect_plan, GuidedFireEffectPlan):
            raise TypeError("effect_plan must be GuidedFireEffectPlan")
        request = validation.request
        _require_resolution_identity(request, direct_request, effect_plan)

        rng_before = self._rng.snapshot()
        staged = self._adjudicator.stage(direct_request)
        rng_after = self._rng.snapshot()
        rng_effect = GuidedFireRngEffect(
            seed=int(rng_before["seed"]),
            state_before=int(rng_before["state"]),
            draws_before=int(rng_before["draws"]),
            state_after=int(rng_after["state"]),
            draws_after=int(rng_after["draws"]),
        )
        damage_effects: tuple[GuidedFireDamageEffect, ...] = ()
        resource_effects: tuple[GuidedFireResourceEffect, ...] = ()
        if staged.outcome is not None:
            outcome = staged.outcome
            damage_effects = (GuidedFireDamageEffect(
                target_id=request.target_id,
                vehicle_losses=outcome.vehicle_losses,
                squad_losses=outcome.squad_losses,
                suppression_applied=outcome.suppression_applied,
                destroyed=outcome.destroyed,
                no_effect=outcome.no_effect,
            ),)
            resource_effects = (GuidedFireResourceEffect(
                shooter_id=request.shooter_id,
                ammunition_key=direct_request.weapon.ammunition_key,
                ammunition_before=direct_request.ammunition_before,
                ammunition_after=staged.ammunition_after,
                cooldown_before_ms=request.cooldown_before,
                cooldown_after_ms=staged.cooldown_after * 1000,
                recovery_updates=effect_plan.recovery_updates,
            ),)
        hit = bool(
            staged.outcome is not None and not staged.outcome.no_effect
        )
        resolution_event = GuidedFireResolutionEvent(
            guider_id=request.guider_id,
            shooter_id=request.shooter_id,
            weapon_id=request.weapon_id,
            target_id=request.target_id,
            hit=hit,
            damage_summary=damage_effects,
        )
        return FireResolution(
            guider_id=request.guider_id,
            shooter_id=request.shooter_id,
            weapon_id=request.weapon_id,
            target_id=request.target_id,
            hit_result=hit,
            damage_effects=damage_effects,
            resource_effects=resource_effects,
            rng_effect=rng_effect,
            resolution_event=resolution_event,
            adjudication_evidence=tuple(staged.events),
            adjudication_id=staged.adjudication_id,
            error=staged.error,
        )


def _require_resolution_identity(
    request: GuidedFireRequest,
    direct_request: DirectFireRequest,
    effect_plan: GuidedFireEffectPlan,
) -> None:
    if (
        direct_request.tick != request.tick
        or direct_request.attacker.operator_id != request.shooter_id
        or direct_request.target.operator_id != request.target_id
        or direct_request.weapon.weapon_id != request.weapon_id
        or direct_request.distance != request.shooter_distance
        or direct_request.ammunition_before != request.ammunition_before
        or direct_request.visibility.to_dict()
        != request.guider_visibility_evidence.to_dict()
    ):
        raise ValueError("GUIDED_FIRE_DIRECT_REQUEST_MISMATCH")
    if (
        effect_plan.guider_id != request.guider_id
        or effect_plan.shooter_id != request.shooter_id
        or effect_plan.weapon_id != request.weapon_id
        or effect_plan.target_id != request.target_id
        or effect_plan.ammo_consume != 1
    ):
        raise ValueError("GUIDED_FIRE_EFFECT_PLAN_MISMATCH")


def _build_validated_guided_fire_request(
    capability: GuidedFireCapability,
    *,
    tick: int,
    guider_visibility_evidence: VisibilityResult,
    shooter_distance: int,
    ammunition_before: int,
    cooldown_before: int,
) -> GuidedFireRequest:
    """Create a request after NativeEngine has revalidated current authority."""

    if not isinstance(capability, GuidedFireCapability):
        raise TypeError("capability must be GuidedFireCapability")
    for name, value in (
        ("tick", tick),
        ("shooter_distance", shooter_distance),
        ("ammunition_before", ammunition_before),
        ("cooldown_before", cooldown_before),
    ):
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative exact int")
    if not isinstance(guider_visibility_evidence, VisibilityResult):
        raise TypeError("guider_visibility_evidence must be VisibilityResult")
    if not guider_visibility_evidence.observable:
        raise ValueError("GUIDED_FIRE_TARGET_NOT_VISIBLE_TO_GUIDER")

    # Detach the evidence from any caller-owned nested modifier mappings.
    visibility = VisibilityResult(
        line_of_sight=guider_visibility_evidence.line_of_sight,
        observable=guider_visibility_evidence.observable,
        distance=guider_visibility_evidence.distance,
        observation_limit=guider_visibility_evidence.observation_limit,
        blocking_hexes=tuple(guider_visibility_evidence.blocking_hexes),
        modifiers=tuple(
            dict(item) for item in guider_visibility_evidence.modifiers
        ),
        reason=guider_visibility_evidence.reason,
    )
    request = object.__new__(GuidedFireRequest)
    values = {
        "tick": tick,
        "guider_id": capability.guider_id,
        "shooter_id": capability.shooter_id,
        "weapon_id": capability.weapon_id,
        "target_id": capability.target_id,
        "guidance_profile_id": capability.guidance_profile_id,
        "guider_visibility_evidence": visibility,
        "shooter_distance": shooter_distance,
        "ammunition_before": ammunition_before,
        "cooldown_before": cooldown_before,
    }
    for name, value in values.items():
        object.__setattr__(request, name, value)
    return request


def _require_nonnegative_exact_int(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative exact int")


__all__ = [
    "EffectBundle",
    "FireResolution",
    "GuidedFireDamageEffect",
    "GuidedFireCommitEffect",
    "GuidedFireEffectPlan",
    "GuidedFireResolutionEvent",
    "GuidedFireResolver",
    "GuidedFireRecoveryUpdate",
    "GuidedFireRequest",
    "GuidedFireResourceEffect",
    "GuidedFireRngEffect",
    "GuidedFireStageResult",
    "GuidedFireValidationResult",
]
