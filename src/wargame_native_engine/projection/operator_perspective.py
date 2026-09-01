"""Immutable perspective-safe projections of Operator lifecycle semantics.

GREEN and OWN consume an already-derived authoritative lifecycle value.
OBSERVED_ENEMY consumes only detached, policy-safe contact evidence.  This
module never reads WorldState, advances time, runs visibility, or enumerates
capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from enum import Enum
from typing import Iterable, Mapping

from wargame_native_engine.core.observation import (
    ObservedEnemyOperatorObservation,
    SideObservation,
)
from wargame_native_engine.io.serialization import canonical_hash, to_json_value
from wargame_native_engine.projection.operator_lifecycle import (
    OPERATOR_LIFECYCLE_VIEW_SCHEMA_VERSION,
    AuthoritativeOperatorLifecycleView,
    BoardingView,
    ConcealmentView,
    DeploymentView,
    FatigueView,
    IdentityView,
    IntegrityView,
    LimitedLifecycleView,
    MovementView,
    OperatorTransitionView,
    RecoveryView,
    SuppressionView,
    SurvivalView,
    WeaponLifecycleView,
)
from wargame_native_engine.rules.perspective import (
    PerspectivePolicy,
    load_perspective_policy,
)


OPERATOR_LIFECYCLE_PERSPECTIVE_SCHEMA_VERSION = (
    "native-operator-lifecycle-perspective/1.0"
)


class OperatorLifecyclePerspectiveKind(str, Enum):
    GREEN = "GREEN"
    OWN = "OWN"
    OBSERVED_ENEMY = "OBSERVED_ENEMY"


class ObservedMotionState(str, Enum):
    MOVING = "MOVING"
    STATIONARY = "STATIONARY"
    UNKNOWN = "UNKNOWN"


class ObservedIdentityStatus(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class GreenOperatorLifecyclePayload:
    operator_reference: int
    authoritative_schema_version: str
    authoritative_semantic_hash: str
    identity: IdentityView
    survival: SurvivalView
    deployment: DeploymentView
    movement: MovementView
    operator_transition: OperatorTransitionView
    fatigue: FatigueView
    suppression: SuppressionView
    concealment: ConcealmentView
    boarding: BoardingView
    weapon_lifecycle: WeaponLifecycleView
    guided_recovery: RecoveryView
    indirect_planning: RecoveryView
    limited_lifecycle: LimitedLifecycleView
    integrity: IntegrityView


@dataclass(frozen=True, slots=True)
class OwnOperatorLifecyclePayload:
    operator_reference: int
    identity: IdentityView
    survival: SurvivalView
    deployment: DeploymentView
    movement: MovementView
    operator_transition: OperatorTransitionView
    fatigue: FatigueView
    suppression: SuppressionView
    concealment: ConcealmentView
    boarding: BoardingView
    weapon_lifecycle: WeaponLifecycleView
    guided_recovery: RecoveryView
    indirect_planning: RecoveryView
    limited_lifecycle: LimitedLifecycleView


@dataclass(frozen=True, slots=True)
class ObservedEnemyIdentityPayload:
    operator_type: str
    subtype: str | None
    source_type_code: int | None
    source_subtype_code: int | None
    canonical_identity: str | None
    canonical_status: ObservedIdentityStatus
    domain: str


@dataclass(frozen=True, slots=True)
class ObservedEnemyObservationPayload:
    position: tuple[float, float]
    altitude: float


@dataclass(frozen=True, slots=True)
class ObservedEnemySurvivalPayload:
    alive: bool
    destroyed: bool


@dataclass(frozen=True, slots=True)
class ObservedEnemyOperatorLifecyclePayload:
    operator_reference: int
    side: str
    identity: ObservedEnemyIdentityPayload
    observation: ObservedEnemyObservationPayload
    survival: ObservedEnemySurvivalPayload
    motion: ObservedMotionState


OperatorLifecyclePerspectivePayload = (
    GreenOperatorLifecyclePayload
    | OwnOperatorLifecyclePayload
    | ObservedEnemyOperatorLifecyclePayload
)


@dataclass(frozen=True, slots=True)
class OperatorLifecyclePerspectiveEnvelope:
    schema_version: str
    policy_id: str
    policy_version: str
    policy_hash: str
    perspective_kind: OperatorLifecyclePerspectiveKind
    payload: OperatorLifecyclePerspectivePayload
    semantic_hash: str

    def to_dict(self) -> dict[str, object]:
        value = to_json_value(self)
        if not isinstance(value, dict):
            raise RuntimeError("perspective envelope serialization failed")
        return value


def project_green_operator_lifecycle(
    view: AuthoritativeOperatorLifecycleView,
    *,
    policy: PerspectivePolicy | None = None,
) -> OperatorLifecyclePerspectiveEnvelope:
    """Project the complete internal lifecycle semantic for GREEN."""

    _validate_authoritative_view(view)
    payload = GreenOperatorLifecyclePayload(
        operator_reference=view.operator_id,
        authoritative_schema_version=view.schema_version,
        authoritative_semantic_hash=view.semantic_hash,
        identity=view.identity,
        survival=view.survival,
        deployment=view.deployment,
        movement=view.movement,
        operator_transition=view.operator_transition,
        fatigue=view.fatigue,
        suppression=view.suppression,
        concealment=view.concealment,
        boarding=view.boarding,
        weapon_lifecycle=view.weapon_lifecycle,
        guided_recovery=view.guided_recovery,
        indirect_planning=view.indirect_planning,
        limited_lifecycle=view.limited_lifecycle,
        integrity=view.integrity,
    )
    return _envelope(
        OperatorLifecyclePerspectiveKind.GREEN,
        payload,
        load_perspective_policy() if policy is None else policy,
    )


def project_own_operator_lifecycle(
    view: AuthoritativeOperatorLifecycleView,
    *,
    policy: PerspectivePolicy | None = None,
) -> OperatorLifecyclePerspectiveEnvelope:
    """Project full operational lifecycle state without GREEN integrity/hash."""

    _validate_authoritative_view(view)
    contract = load_perspective_policy() if policy is None else policy
    if contract.document.get("own_integrity_visibility") != "GREEN_ONLY":
        raise ValueError("unsafe own integrity policy")
    if not contract.own_lifecycle_fields:
        raise ValueError("own lifecycle field policy is empty")
    payload = OwnOperatorLifecyclePayload(
        operator_reference=view.operator_id,
        identity=view.identity,
        survival=view.survival,
        deployment=view.deployment,
        movement=view.movement,
        operator_transition=view.operator_transition,
        fatigue=view.fatigue,
        suppression=view.suppression,
        concealment=view.concealment,
        boarding=view.boarding,
        weapon_lifecycle=view.weapon_lifecycle,
        guided_recovery=view.guided_recovery,
        indirect_planning=view.indirect_planning,
        limited_lifecycle=view.limited_lifecycle,
    )
    return _envelope(OperatorLifecyclePerspectiveKind.OWN, payload, contract)


def project_observed_enemy_operator_lifecycle(
    evidence: ObservedEnemyOperatorObservation,
    *,
    policy: PerspectivePolicy | None = None,
) -> OperatorLifecyclePerspectiveEnvelope:
    """Project one current safe contact without World or GREEN backfill."""

    if not isinstance(evidence, ObservedEnemyOperatorObservation):
        raise TypeError("observed enemy projection requires safe contact evidence")
    contract = load_perspective_policy() if policy is None else policy
    evidence_fields = {item.name for item in fields(evidence)}
    if evidence_fields != set(contract.enemy_observation_allowlist):
        raise ValueError("observed enemy evidence/policy field mismatch")
    if type(evidence.operator_id) is not int or evidence.operator_id < 0:
        raise ValueError("observed enemy operator reference is missing")
    if not evidence.side or not evidence.operator_type or not evidence.domain:
        raise ValueError("observed enemy identity evidence is missing")
    identity_status = _observed_identity_status(evidence)
    canonical_identity = (
        evidence.canonical_identity
        if identity_status is ObservedIdentityStatus.KNOWN
        else None
    )
    try:
        motion = ObservedMotionState(str(evidence.motion_state).upper())
    except ValueError:
        motion = ObservedMotionState.UNKNOWN
    payload = ObservedEnemyOperatorLifecyclePayload(
        operator_reference=evidence.operator_id,
        side=evidence.side,
        identity=ObservedEnemyIdentityPayload(
            operator_type=evidence.operator_type,
            subtype=evidence.subtype,
            source_type_code=evidence.source_type_code,
            source_subtype_code=evidence.source_subtype_code,
            canonical_identity=canonical_identity,
            canonical_status=identity_status,
            domain=evidence.domain,
        ),
        observation=ObservedEnemyObservationPayload(
            position=(float(evidence.position[0]), float(evidence.position[1])),
            altitude=float(evidence.altitude),
        ),
        survival=ObservedEnemySurvivalPayload(
            alive=bool(evidence.alive),
            destroyed=bool(evidence.destroyed),
        ),
        motion=motion,
    )
    return _envelope(
        OperatorLifecyclePerspectiveKind.OBSERVED_ENEMY,
        payload,
        contract,
    )


def project_green_operator_lifecycles(
    views: Iterable[AuthoritativeOperatorLifecycleView],
    *,
    policy: PerspectivePolicy | None = None,
) -> tuple[OperatorLifecyclePerspectiveEnvelope, ...]:
    """Project GREEN lifecycle values in stable Operator order."""

    contract = load_perspective_policy() if policy is None else policy
    return tuple(
        project_green_operator_lifecycle(view, policy=contract)
        for view in sorted(views, key=lambda item: item.operator_id)
    )


def project_side_operator_lifecycles(
    observation: SideObservation,
    own_views: Mapping[int, AuthoritativeOperatorLifecycleView],
    *,
    policy: PerspectivePolicy | None = None,
) -> tuple[OperatorLifecyclePerspectiveEnvelope, ...]:
    """Project current OWN and observed-enemy values without rebuilding contact."""

    contract = load_perspective_policy() if policy is None else policy
    values: list[OperatorLifecyclePerspectiveEnvelope] = []
    for friendly in sorted(
        observation.friendly_operators,
        key=lambda item: item.operator_id,
    ):
        view = own_views.get(friendly.operator_id)
        if view is None:
            raise ValueError("own lifecycle view is missing")
        if view.identity.side != observation.side:
            raise ValueError("own lifecycle side mismatch")
        values.append(project_own_operator_lifecycle(view, policy=contract))
    values.extend(
        project_observed_enemy_operator_lifecycle(item, policy=contract)
        for item in sorted(
            observation.visible_enemies,
            key=lambda item: item.operator_id,
        )
    )
    return tuple(sorted(values, key=_envelope_order))


def _validate_authoritative_view(
    view: AuthoritativeOperatorLifecycleView,
) -> None:
    if not isinstance(view, AuthoritativeOperatorLifecycleView):
        raise TypeError("authoritative lifecycle view is required")
    if view.schema_version != OPERATOR_LIFECYCLE_VIEW_SCHEMA_VERSION:
        raise ValueError("unsupported authoritative lifecycle view schema")


def _observed_identity_status(
    evidence: ObservedEnemyOperatorObservation,
) -> ObservedIdentityStatus:
    if (
        str(evidence.canonical_status).upper() == "KNOWN"
        and evidence.canonical_identity
    ):
        return ObservedIdentityStatus.KNOWN
    return ObservedIdentityStatus.UNKNOWN


def _envelope(
    kind: OperatorLifecyclePerspectiveKind,
    payload: OperatorLifecyclePerspectivePayload,
    policy: PerspectivePolicy,
) -> OperatorLifecyclePerspectiveEnvelope:
    value = OperatorLifecyclePerspectiveEnvelope(
        schema_version=OPERATOR_LIFECYCLE_PERSPECTIVE_SCHEMA_VERSION,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_hash=policy.content_hash,
        perspective_kind=kind,
        payload=payload,
        semantic_hash="",
    )
    semantic_payload = {
        item.name: getattr(value, item.name)
        for item in fields(value)
        if item.name != "semantic_hash"
    }
    return replace(value, semantic_hash=canonical_hash(semantic_payload))


def _envelope_order(
    value: OperatorLifecyclePerspectiveEnvelope,
) -> tuple[int, str]:
    return (value.payload.operator_reference, value.perspective_kind.value)


__all__ = [
    "OPERATOR_LIFECYCLE_PERSPECTIVE_SCHEMA_VERSION",
    "GreenOperatorLifecyclePayload",
    "ObservedEnemyIdentityPayload",
    "ObservedEnemyObservationPayload",
    "ObservedEnemyOperatorLifecyclePayload",
    "ObservedEnemySurvivalPayload",
    "ObservedIdentityStatus",
    "ObservedMotionState",
    "OperatorLifecyclePerspectiveEnvelope",
    "OperatorLifecyclePerspectiveKind",
    "OwnOperatorLifecyclePayload",
    "project_green_operator_lifecycle",
    "project_green_operator_lifecycles",
    "project_observed_enemy_operator_lifecycle",
    "project_own_operator_lifecycle",
    "project_side_operator_lifecycles",
]
