"""Immutable semantic projection of authoritative Operator lifecycle state.

This module is deliberately downstream of ``core`` and ``rules``.  It reads
authoritative state but never advances time, repairs invariants, enumerates
capabilities, or participates in Engine legality.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from enum import Enum
from functools import lru_cache
from typing import Any

from wargame_native_engine.compat.source_frame.models import UnitDomain
from wargame_native_engine.core.boarding import (
    BoardingTransition,
    BoardingTransitionStatus,
    BoardingTransitionType,
)
from wargame_native_engine.core.concealment import ConcealmentPosture
from wargame_native_engine.core.fire_intent import FireMode
from wargame_native_engine.core.operator import (
    MovementTaskPauseReason,
    OperatorState,
)
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.movement.models import MovementMode
from wargame_native_engine.movement.policy import (
    SourceMovementPolicy,
    load_source_movement_policy,
)
from wargame_native_engine.rules.guidance import (
    GuidanceAuthority,
    load_guidance_authority,
)
from wargame_native_engine.rules.indirect_fire import (
    IndirectFireAuthority,
    load_indirect_fire_authority,
)
from wargame_native_engine.rules.operator_identity import (
    CanonicalIdentitySource,
    resolve_operator_canonical_identity,
)
from wargame_native_engine.rules.operator_lifecycle import (
    OperatorLifecycleAuthority,
    load_operator_lifecycle_authority,
)


OPERATOR_LIFECYCLE_VIEW_SCHEMA_VERSION = (
    "native-operator-lifecycle-view/1.0"
)


class Applicability(str, Enum):
    APPLICABLE = "APPLICABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"
    MISSING = "MISSING"


class IdentityStatus(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    MISSING = "MISSING"


class IntegrityStatus(str, Enum):
    OK = "OK"
    UNKNOWN = "UNKNOWN"
    MISSING = "MISSING"
    INCONSISTENT = "INCONSISTENT"


class SpatialStatus(str, Enum):
    ON_MAP = "ON_MAP"
    ON_BOARD = "ON_BOARD"
    OFF_MAP = "OFF_MAP"
    INCONSISTENT = "INCONSISTENT"


class MovementLifecycleState(str, Enum):
    STOPPED = "STOPPED"
    MOVING = "MOVING"
    STOPPING = "STOPPING"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    TRANSITIONING = "TRANSITIONING"
    UNKNOWN = "UNKNOWN"
    INCONSISTENT = "INCONSISTENT"


class BoardingRole(str, Enum):
    NONE = "NONE"
    PASSENGER = "PASSENGER"
    CARRIER = "CARRIER"
    INCONSISTENT = "INCONSISTENT"


class BoardingLifecycleState(str, Enum):
    NONE = "NONE"
    BOARDING = "BOARDING"
    ON_BOARD = "ON_BOARD"
    UNLOADING = "UNLOADING"
    CARRYING = "CARRYING"
    INCONSISTENT = "INCONSISTENT"


class WeaponPosture(str, Enum):
    LOCKED = "LOCKED"
    UNFOLDED = "UNFOLDED"
    TRANSITIONING = "TRANSITIONING"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INCONSISTENT = "INCONSISTENT"


class LimitedLifecycleState(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"
    INCONSISTENT = "INCONSISTENT"


@dataclass(frozen=True, slots=True)
class IdentityView:
    side: str
    source_type: str
    source_subtype: str | None
    canonical_identity: str | None
    canonical_status: IdentityStatus
    canonical_source: str | None
    domain: str | None


@dataclass(frozen=True, slots=True)
class SurvivalView:
    alive: bool
    destroyed: bool
    health: float
    max_health: float
    blood: int | None
    max_blood: int | None


@dataclass(frozen=True, slots=True)
class DeploymentView:
    deployed: bool
    spatially_present: bool
    spatial_status: SpatialStatus
    current_hex: int | None
    on_board: bool


@dataclass(frozen=True, slots=True)
class MovementView:
    state: MovementLifecycleState
    mode: str | None
    speed: float
    has_active_segment: bool
    segment_progress_millionths: int | None
    segment_remaining_ms: int | None
    has_remaining_task: bool
    remaining_path_length: int
    pause_pending: bool
    paused: bool
    pause_reason: str | None
    blocked_reason: str | None
    stop_transition_remaining_ms: int


@dataclass(frozen=True, slots=True)
class OperatorTransitionView:
    active: bool
    kind: str | None
    remaining_ms: int


@dataclass(frozen=True, slots=True)
class FatigueView:
    applicability: Applicability
    level: int | None
    recovery_elapsed_ms: int | None
    recovery_remaining_ms: int | None


@dataclass(frozen=True, slots=True)
class SuppressionView:
    active: bool
    remaining_ms: int


@dataclass(frozen=True, slots=True)
class ConcealmentView:
    applicability: Applicability
    posture: str | None


@dataclass(frozen=True, slots=True)
class BoardingView:
    applicability: Applicability
    role: BoardingRole
    state: BoardingLifecycleState
    transition_type: str | None
    transition_remaining_ms: int | None
    movement_locked: bool


@dataclass(frozen=True, slots=True)
class WeaponLifecycleView:
    applicability: Applicability
    posture: WeaponPosture
    cooldown_active: bool
    cooldown_remaining_ms: int
    cooldown_clear: bool


@dataclass(frozen=True, slots=True)
class RecoveryView:
    applicability: Applicability
    active: bool
    remaining_ms: int | None
    ready: bool | None


@dataclass(frozen=True, slots=True)
class LimitedLifecycleView:
    applicability: Applicability
    enabled: bool
    profile_id: str | None
    state: LimitedLifecycleState
    remaining_ms: int | None


@dataclass(frozen=True, slots=True)
class IntegrityView:
    status: IntegrityStatus
    codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuthoritativeOperatorLifecycleView:
    schema_version: str
    semantic_hash: str
    operator_id: int
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
class OperatorLifecycleProjectionAuthority:
    operator_lifecycle: OperatorLifecycleAuthority
    guidance: GuidanceAuthority
    indirect: IndirectFireAuthority
    movement: SourceMovementPolicy


@dataclass(slots=True)
class _IntegrityCollector:
    values: list[tuple[IntegrityStatus, str]]

    def add(self, status: IntegrityStatus, code: str) -> None:
        self.values.append((status, code))

    def result(self) -> IntegrityView:
        rank = {
            IntegrityStatus.OK: 0,
            IntegrityStatus.UNKNOWN: 1,
            IntegrityStatus.MISSING: 2,
            IntegrityStatus.INCONSISTENT: 3,
        }
        status = max(
            (item[0] for item in self.values),
            default=IntegrityStatus.OK,
            key=rank.__getitem__,
        )
        return IntegrityView(
            status=status,
            codes=tuple(sorted({item[1] for item in self.values})),
        )


def project_operator_lifecycle(
    world: WorldState,
    operator_id: int,
    *,
    authority: OperatorLifecycleProjectionAuthority | None = None,
    _boarding_index: dict[int, tuple[BoardingTransition, ...]] | None = None,
) -> AuthoritativeOperatorLifecycleView:
    """Return a detached, deterministic semantic value for one Operator."""

    if type(operator_id) is not int or operator_id < 0:
        raise ValueError("operator_id must be a non-negative exact int")
    operator = world.operators.get(operator_id)
    if operator is None:
        raise ValueError("OPERATOR_NOT_FOUND")
    context = default_projection_authority() if authority is None else authority
    issues = _IntegrityCollector([])

    domain = _domain(operator, issues)
    identity = _identity(operator, domain, context, issues)
    survival = _survival(operator, issues)
    active_transitions = (
        _active_boarding_transitions(world, operator_id)
        if _boarding_index is None
        else _boarding_index.get(operator_id, ())
    )
    boarding = _boarding(
        world,
        operator,
        active_transitions,
        issues,
    )
    deployment = _deployment(operator, boarding.role, issues)
    operator_transition = _operator_transition(operator, issues)
    movement = _movement(operator, boarding.role, operator_transition, issues)
    fatigue = _fatigue(operator, domain, context.movement, issues)
    suppression = _suppression(operator, issues)
    concealment = _concealment(operator, domain, issues)
    weapon = _weapon(operator, issues)
    guided = _guided(operator, context.guidance, issues)
    indirect = _indirect(operator, context.indirect, issues)
    limited = _limited_lifecycle(
        operator,
        context.operator_lifecycle,
        issues,
    )
    _legacy_integrity(operator, boarding.role, issues)

    view = AuthoritativeOperatorLifecycleView(
        schema_version=OPERATOR_LIFECYCLE_VIEW_SCHEMA_VERSION,
        semantic_hash="",
        operator_id=operator.operator_id,
        identity=identity,
        survival=survival,
        deployment=deployment,
        movement=movement,
        operator_transition=operator_transition,
        fatigue=fatigue,
        suppression=suppression,
        concealment=concealment,
        boarding=boarding,
        weapon_lifecycle=weapon,
        guided_recovery=guided,
        indirect_planning=indirect,
        limited_lifecycle=limited,
        integrity=issues.result(),
    )
    return replace(view, semantic_hash=_view_semantic_hash(view))


def project_world_operator_lifecycles(
    world: WorldState,
    *,
    authority: OperatorLifecycleProjectionAuthority | None = None,
) -> tuple[AuthoritativeOperatorLifecycleView, ...]:
    """Project all Operators in stable id order without retaining a cache."""

    context = default_projection_authority() if authority is None else authority
    boarding_index = _boarding_transition_index(world)
    return tuple(
        project_operator_lifecycle(
            world,
            operator_id,
            authority=context,
            _boarding_index=boarding_index,
        )
        for operator_id in sorted(world.operators)
    )


@lru_cache(maxsize=1)
def default_projection_authority() -> OperatorLifecycleProjectionAuthority:
    return OperatorLifecycleProjectionAuthority(
        operator_lifecycle=load_operator_lifecycle_authority(),
        guidance=load_guidance_authority(),
        indirect=load_indirect_fire_authority(),
        movement=load_source_movement_policy(),
    )


def _domain(
    operator: OperatorState,
    issues: _IntegrityCollector,
) -> UnitDomain:
    try:
        return UnitDomain(str(operator.operator_type).upper())
    except ValueError:
        issues.add(IntegrityStatus.UNKNOWN, "UNKNOWN_OPERATOR_DOMAIN")
        return UnitDomain.UNKNOWN


def _identity(
    operator: OperatorState,
    domain: UnitDomain,
    authority: OperatorLifecycleProjectionAuthority,
    issues: _IntegrityCollector,
) -> IdentityView:
    resolution = resolve_operator_canonical_identity(
        operator,
        authority.operator_lifecycle,
        allow_compatibility_fallback=False,
    )
    if resolution.canonical_identity is None:
        issues.add(IntegrityStatus.UNKNOWN, "UNKNOWN_CANONICAL_IDENTITY")
        status = IdentityStatus.UNKNOWN
    else:
        status = IdentityStatus.KNOWN
    if (
        operator.lifecycle_profile_id is not None
        and authority.operator_lifecycle.profile(operator.lifecycle_profile_id)
        is None
    ):
        issues.add(
            IntegrityStatus.MISSING,
            "LIFECYCLE_PROFILE_AUTHORITY_MISSING",
        )
    return IdentityView(
        side=operator.side,
        source_type=operator.operator_type,
        source_subtype=operator.subtype,
        canonical_identity=resolution.canonical_identity,
        canonical_status=status,
        canonical_source=(
            None
            if resolution.source is CanonicalIdentitySource.UNKNOWN
            else resolution.source.value
        ),
        domain=(None if domain is UnitDomain.UNKNOWN else domain.value),
    )


def _survival(
    operator: OperatorState,
    issues: _IntegrityCollector,
) -> SurvivalView:
    if operator.alive and operator.health <= 0:
        issues.add(IntegrityStatus.INCONSISTENT, "ALIVE_WITH_ZERO_HEALTH")
    if operator.alive and operator.blood is not None and operator.blood <= 0:
        issues.add(IntegrityStatus.INCONSISTENT, "ALIVE_WITH_ZERO_BLOOD")
    if not operator.alive and operator.health > 0:
        issues.add(IntegrityStatus.INCONSISTENT, "DEAD_WITH_POSITIVE_HEALTH")
    if not operator.alive and operator.blood is not None and operator.blood > 0:
        issues.add(IntegrityStatus.INCONSISTENT, "DEAD_WITH_POSITIVE_BLOOD")
    return SurvivalView(
        alive=operator.alive,
        destroyed=not operator.alive,
        health=operator.health,
        max_health=operator.max_health,
        blood=operator.blood,
        max_blood=operator.max_blood,
    )


def _deployment(
    operator: OperatorState,
    role: BoardingRole,
    issues: _IntegrityCollector,
) -> DeploymentView:
    on_board = role is BoardingRole.PASSENGER
    if on_board and operator.current_hex is not None:
        issues.add(
            IntegrityStatus.INCONSISTENT,
            "ON_BOARD_WITH_CURRENT_HEX",
        )
        spatial_status = SpatialStatus.INCONSISTENT
    elif on_board:
        spatial_status = SpatialStatus.ON_BOARD
    elif operator.current_hex is not None:
        spatial_status = SpatialStatus.ON_MAP
    else:
        spatial_status = SpatialStatus.OFF_MAP
    return DeploymentView(
        deployed=operator.deployed,
        spatially_present=operator.current_hex is not None,
        spatial_status=spatial_status,
        current_hex=operator.current_hex,
        on_board=on_board,
    )


def _operator_transition(
    operator: OperatorState,
    issues: _IntegrityCollector,
) -> OperatorTransitionView:
    remaining = operator.transition_remaining_ms
    if remaining < 0:
        issues.add(IntegrityStatus.INCONSISTENT, "NEGATIVE_TRANSITION_TIMER")
    if (operator.transition_kind is None) != (remaining == 0):
        issues.add(
            IntegrityStatus.INCONSISTENT,
            "TRANSITION_KIND_TIME_MISMATCH",
        )
    known = {"WEAPON_LOCK", "WEAPON_UNFOLD", "ENTER_MARCHING", "EXIT_MARCHING"}
    if operator.transition_kind is not None and operator.transition_kind not in known:
        issues.add(IntegrityStatus.UNKNOWN, "UNKNOWN_TRANSITION_KIND")
    return OperatorTransitionView(
        active=operator.transition_kind is not None and remaining > 0,
        kind=operator.transition_kind,
        remaining_ms=remaining,
    )


def _movement(
    operator: OperatorState,
    boarding_role: BoardingRole,
    transition: OperatorTransitionView,
    issues: _IntegrityCollector,
) -> MovementView:
    active = operator.segment_to_hex is not None
    remaining_task = bool(operator.move_path)
    stop_remaining = operator.move_to_stop_remaining_ms
    pause = operator.movement_task_pause_reason
    pause_value = pause.value if isinstance(pause, MovementTaskPauseReason) else None
    structural_error = False

    try:
        mode = MovementMode(str(operator.movement_mode)).value
    except ValueError:
        mode = None
        issues.add(IntegrityStatus.UNKNOWN, "UNKNOWN_MOVEMENT_MODE")

    if stop_remaining < 0:
        structural_error = True
        issues.add(IntegrityStatus.INCONSISTENT, "NEGATIVE_STOP_TIMER")
    if active:
        if operator.segment_from_hex is None:
            structural_error = True
            issues.add(IntegrityStatus.MISSING, "ACTIVE_SEGMENT_FROM_HEX_MISSING")
        if operator.segment_traversal_ms <= 0:
            structural_error = True
            issues.add(IntegrityStatus.INCONSISTENT, "ACTIVE_SEGMENT_DURATION_INVALID")
        if not 0 <= operator.segment_elapsed_ms <= operator.segment_traversal_ms:
            structural_error = True
            issues.add(IntegrityStatus.INCONSISTENT, "ACTIVE_SEGMENT_ELAPSED_INVALID")
        if operator.stopped:
            structural_error = True
            issues.add(IntegrityStatus.INCONSISTENT, "ACTIVE_SEGMENT_MARKED_STOPPED")
        if stop_remaining > 0:
            structural_error = True
            issues.add(IntegrityStatus.INCONSISTENT, "STOPPING_WITH_ACTIVE_SEGMENT")
    elif any((
        operator.segment_from_hex is not None,
        operator.segment_elapsed_ms != 0,
        operator.segment_traversal_ms != 0,
        operator.segment_event_id is not None,
        operator.cur_pos_millionths != 0,
    )):
        structural_error = True
        issues.add(IntegrityStatus.INCONSISTENT, "SEGMENT_STATE_WITHOUT_ACTIVE_SEGMENT")

    if pause is not None and not isinstance(pause, MovementTaskPauseReason):
        issues.add(IntegrityStatus.UNKNOWN, "UNKNOWN_MOVEMENT_PAUSE_REASON")
    if pause is not None and not remaining_task:
        structural_error = True
        issues.add(IntegrityStatus.INCONSISTENT, "PAUSE_REASON_WITHOUT_MOVEMENT_TASK")
    if operator.movement_blocked_reason is not None and not remaining_task:
        structural_error = True
        issues.add(IntegrityStatus.INCONSISTENT, "BLOCK_REASON_WITHOUT_MOVEMENT_TASK")
    if stop_remaining > 0 and operator.stopped:
        structural_error = True
        issues.add(IntegrityStatus.INCONSISTENT, "STOPPING_MARKED_STOPPED")
    if (operator.transition_kind is None) != (
        operator.transition_remaining_ms == 0
    ):
        structural_error = True
    if transition.active and (active or stop_remaining > 0):
        structural_error = True
        issues.add(IntegrityStatus.INCONSISTENT, "OVERLAPPING_MOVEMENT_TRANSITIONS")
    if not operator.alive and (
        active or remaining_task or stop_remaining > 0 or transition.active
    ):
        structural_error = True
        issues.add(IntegrityStatus.INCONSISTENT, "DESTROYED_WITH_ACTIVE_MOVEMENT")
    if boarding_role is BoardingRole.PASSENGER and (
        active or remaining_task or stop_remaining > 0 or transition.active
    ):
        structural_error = True
        issues.add(IntegrityStatus.INCONSISTENT, "ON_BOARD_WITH_ACTIVE_MOVEMENT")

    if structural_error:
        state = MovementLifecycleState.INCONSISTENT
    elif mode is None:
        state = MovementLifecycleState.UNKNOWN
    elif active:
        state = MovementLifecycleState.MOVING
    elif stop_remaining > 0:
        state = MovementLifecycleState.STOPPING
    elif transition.active:
        state = MovementLifecycleState.TRANSITIONING
    elif pause is not None:
        state = MovementLifecycleState.PAUSED
    elif operator.movement_blocked_reason is not None:
        state = MovementLifecycleState.BLOCKED
    elif remaining_task:
        state = MovementLifecycleState.INCONSISTENT
        issues.add(IntegrityStatus.INCONSISTENT, "ORPHANED_MOVEMENT_TASK")
    elif operator.stopped and operator.speed == 0:
        state = MovementLifecycleState.STOPPED
    else:
        state = MovementLifecycleState.INCONSISTENT
        issues.add(IntegrityStatus.INCONSISTENT, "UNSTABLE_STOPPED_STATE")

    segment_remaining = None
    if (
        active
        and operator.segment_traversal_ms > 0
        and 0 <= operator.segment_elapsed_ms <= operator.segment_traversal_ms
    ):
        segment_remaining = (
            operator.segment_traversal_ms - operator.segment_elapsed_ms
        )
    return MovementView(
        state=state,
        mode=mode,
        speed=operator.speed,
        has_active_segment=active,
        segment_progress_millionths=(operator.cur_pos_millionths if active else None),
        segment_remaining_ms=segment_remaining,
        has_remaining_task=remaining_task,
        remaining_path_length=len(operator.move_path),
        pause_pending=active and pause is not None,
        paused=state is MovementLifecycleState.PAUSED,
        pause_reason=pause_value,
        blocked_reason=operator.movement_blocked_reason,
        stop_transition_remaining_ms=stop_remaining,
    )


def _fatigue(
    operator: OperatorState,
    domain: UnitDomain,
    policy: SourceMovementPolicy,
    issues: _IntegrityCollector,
) -> FatigueView:
    if domain is UnitDomain.UNKNOWN:
        return FatigueView(Applicability.UNKNOWN, None, None, None)
    if domain is not UnitDomain.PERSONNEL:
        if operator.fatigue_level or operator.fatigue_recovery_elapsed_ms:
            issues.add(IntegrityStatus.INCONSISTENT, "FATIGUE_NOT_APPLICABLE")
        return FatigueView(Applicability.NOT_APPLICABLE, None, None, None)
    if not 0 <= operator.fatigue_level <= 2:
        issues.add(IntegrityStatus.INCONSISTENT, "FATIGUE_LEVEL_INVALID")
    if operator.fatigue_recovery_elapsed_ms < 0:
        issues.add(IntegrityStatus.INCONSISTENT, "NEGATIVE_FATIGUE_RECOVERY")
    recovery_remaining = 0
    if operator.fatigue_level > 0:
        if operator.fatigue_recovery_elapsed_ms >= policy.fatigue_recovery_ms:
            issues.add(IntegrityStatus.INCONSISTENT, "FATIGUE_RECOVERY_BOUNDARY_UNAPPLIED")
            recovery_remaining = None
        else:
            recovery_remaining = (
                policy.fatigue_recovery_ms
                - operator.fatigue_recovery_elapsed_ms
            )
    elif operator.fatigue_recovery_elapsed_ms != 0:
        issues.add(IntegrityStatus.INCONSISTENT, "FATIGUE_RECOVERY_WITHOUT_FATIGUE")
        recovery_remaining = None
    return FatigueView(
        Applicability.APPLICABLE,
        operator.fatigue_level,
        operator.fatigue_recovery_elapsed_ms,
        recovery_remaining,
    )


def _suppression(
    operator: OperatorState,
    issues: _IntegrityCollector,
) -> SuppressionView:
    if operator.suppression_remaining_ms < 0:
        issues.add(IntegrityStatus.INCONSISTENT, "NEGATIVE_SUPPRESSION_TIMER")
    return SuppressionView(
        active=operator.suppression_remaining_ms > 0,
        remaining_ms=operator.suppression_remaining_ms,
    )


def _concealment(
    operator: OperatorState,
    domain: UnitDomain,
    issues: _IntegrityCollector,
) -> ConcealmentView:
    if domain is UnitDomain.UNKNOWN:
        applicability = Applicability.UNKNOWN
    elif domain is UnitDomain.AIR:
        applicability = Applicability.NOT_APPLICABLE
    else:
        applicability = Applicability.APPLICABLE
    posture = operator.concealment_posture
    if not isinstance(posture, ConcealmentPosture):
        issues.add(IntegrityStatus.UNKNOWN, "UNKNOWN_CONCEALMENT_POSTURE")
        return ConcealmentView(Applicability.UNKNOWN, None)
    if (
        applicability is Applicability.NOT_APPLICABLE
        and posture is ConcealmentPosture.CONCEALED
    ):
        issues.add(IntegrityStatus.INCONSISTENT, "CONCEALMENT_NOT_APPLICABLE")
    return ConcealmentView(
        applicability=applicability,
        posture=(
            None
            if applicability is Applicability.NOT_APPLICABLE
            else posture.value
        ),
    )


def _active_boarding_transitions(
    world: WorldState,
    operator_id: int,
) -> tuple[BoardingTransition, ...]:
    return tuple(
        item
        for item in world.boarding_transitions
        if item.status is BoardingTransitionStatus.ACTIVE
        and operator_id in {item.carrier_id, item.passenger_id}
    )


def _boarding_transition_index(
    world: WorldState,
) -> dict[int, tuple[BoardingTransition, ...]]:
    values: dict[int, list[BoardingTransition]] = {}
    for item in world.boarding_transitions:
        if item.status is not BoardingTransitionStatus.ACTIVE:
            continue
        values.setdefault(item.carrier_id, []).append(item)
        values.setdefault(item.passenger_id, []).append(item)
    return {
        operator_id: tuple(items)
        for operator_id, items in values.items()
    }


def _boarding(
    world: WorldState,
    operator: OperatorState,
    transitions: tuple[BoardingTransition, ...],
    issues: _IntegrityCollector,
) -> BoardingView:
    if operator.mounted_on is not None and operator.passenger_id is not None:
        role = BoardingRole.INCONSISTENT
        issues.add(IntegrityStatus.INCONSISTENT, "BOARDING_ROLE_CONFLICT")
    elif operator.mounted_on is not None:
        role = BoardingRole.PASSENGER
        carrier = world.operators.get(operator.mounted_on)
        if carrier is None:
            issues.add(IntegrityStatus.MISSING, "BOARDING_CARRIER_MISSING")
        elif carrier.passenger_id != operator.operator_id:
            issues.add(IntegrityStatus.INCONSISTENT, "BOARDING_RELATION_NOT_RECIPROCAL")
    elif operator.passenger_id is not None:
        role = BoardingRole.CARRIER
        passenger = world.operators.get(operator.passenger_id)
        if passenger is None:
            issues.add(IntegrityStatus.MISSING, "BOARDING_PASSENGER_MISSING")
        elif passenger.mounted_on != operator.operator_id:
            issues.add(IntegrityStatus.INCONSISTENT, "BOARDING_RELATION_NOT_RECIPROCAL")
    else:
        role = BoardingRole.NONE

    if len(transitions) > 1:
        issues.add(IntegrityStatus.INCONSISTENT, "MULTIPLE_ACTIVE_BOARDING_TRANSITIONS")
    transition = transitions[0] if len(transitions) == 1 else None
    if transition is not None:
        carrier = world.operators.get(transition.carrier_id)
        passenger = world.operators.get(transition.passenger_id)
        if (
            carrier is None
            or passenger is None
            or carrier.passenger_id != transition.passenger_id
            or passenger.mounted_on != transition.carrier_id
        ):
            issues.add(IntegrityStatus.INCONSISTENT, "BOARDING_TRANSITION_RELATION_MISMATCH")

    applicable = (
        operator.boarding_profile_id is not None
        or role is not BoardingRole.NONE
        or transition is not None
    )
    applicability = (
        Applicability.APPLICABLE
        if applicable
        else Applicability.NOT_APPLICABLE
    )
    if role is BoardingRole.INCONSISTENT or len(transitions) > 1:
        state = BoardingLifecycleState.INCONSISTENT
    elif transition is not None:
        state = (
            BoardingLifecycleState.BOARDING
            if transition.transition_type is BoardingTransitionType.BOARD
            else BoardingLifecycleState.UNLOADING
        )
    elif role is BoardingRole.PASSENGER:
        state = BoardingLifecycleState.ON_BOARD
    elif role is BoardingRole.CARRIER:
        state = BoardingLifecycleState.CARRYING
    else:
        state = BoardingLifecycleState.NONE
    return BoardingView(
        applicability=applicability,
        role=role,
        state=state,
        transition_type=(
            None if transition is None else transition.transition_type.value
        ),
        transition_remaining_ms=(
            None if transition is None else transition.remaining_ms
        ),
        movement_locked=operator.movement_locked,
    )


def _weapon(
    operator: OperatorState,
    issues: _IntegrityCollector,
) -> WeaponLifecycleView:
    applicable = bool(operator.weapon_ids)
    cooldown = operator.weapon_cooldown_remaining_ms
    if cooldown < 0:
        issues.add(IntegrityStatus.INCONSISTENT, "NEGATIVE_WEAPON_COOLDOWN")
    if not applicable:
        if cooldown != 0 or operator.weapon_locked or not operator.weapon_unfolded:
            issues.add(IntegrityStatus.INCONSISTENT, "WEAPON_STATE_NOT_APPLICABLE")
        return WeaponLifecycleView(
            applicability=Applicability.NOT_APPLICABLE,
            posture=WeaponPosture.NOT_APPLICABLE,
            cooldown_active=False,
            cooldown_remaining_ms=cooldown,
            cooldown_clear=False,
        )
    if operator.transition_kind in {"WEAPON_LOCK", "WEAPON_UNFOLD"}:
        posture = WeaponPosture.TRANSITIONING
    elif operator.weapon_locked and not operator.weapon_unfolded:
        posture = WeaponPosture.LOCKED
    elif not operator.weapon_locked and operator.weapon_unfolded:
        posture = WeaponPosture.UNFOLDED
    else:
        posture = WeaponPosture.INCONSISTENT
        issues.add(IntegrityStatus.INCONSISTENT, "WEAPON_POSTURE_INCONSISTENT")
    return WeaponLifecycleView(
        applicability=Applicability.APPLICABLE,
        posture=posture,
        cooldown_active=cooldown > 0,
        cooldown_remaining_ms=cooldown,
        cooldown_clear=cooldown == 0,
    )


def _guided(
    operator: OperatorState,
    authority: GuidanceAuthority,
    issues: _IntegrityCollector,
) -> RecoveryView:
    explicit = operator.guidance_profile_id is not None
    missing_profile = (
        explicit and authority.profile(operator.guidance_profile_id or "") is None
    )
    if missing_profile:
        issues.add(IntegrityStatus.MISSING, "GUIDANCE_PROFILE_AUTHORITY_MISSING")
    weapon_support = any(
        FireMode.GUIDED in authority.supported_fire_modes(weapon_id)
        for weapon_id in operator.weapon_ids
    )
    applicable = explicit or weapon_support
    remaining = operator.guided_fire_recovery_remaining_ms
    if remaining < 0:
        issues.add(IntegrityStatus.INCONSISTENT, "NEGATIVE_GUIDED_RECOVERY")
    if not applicable and remaining != 0:
        issues.add(IntegrityStatus.INCONSISTENT, "GUIDED_RECOVERY_NOT_APPLICABLE")
    if missing_profile and not weapon_support:
        applicability = Applicability.MISSING
        active = False
        remaining_ms = None
        ready = None
    else:
        applicability = (
            Applicability.APPLICABLE
            if applicable
            else Applicability.NOT_APPLICABLE
        )
        active = applicable and remaining > 0
        remaining_ms = remaining if applicable else None
        ready = (remaining == 0) if applicable else None
    return RecoveryView(applicability, active, remaining_ms, ready)


def _indirect(
    operator: OperatorState,
    authority: IndirectFireAuthority,
    issues: _IntegrityCollector,
) -> RecoveryView:
    applicable = any(
        authority.profile_for_weapon(weapon_id) is not None
        for weapon_id in operator.weapon_ids
    )
    remaining = operator.indirect_fire_planned_cooldown_remaining_ms
    if remaining < 0:
        issues.add(IntegrityStatus.INCONSISTENT, "NEGATIVE_INDIRECT_COOLDOWN")
    if not applicable and remaining != 0:
        issues.add(IntegrityStatus.INCONSISTENT, "INDIRECT_COOLDOWN_NOT_APPLICABLE")
    return RecoveryView(
        applicability=(
            Applicability.APPLICABLE
            if applicable
            else Applicability.NOT_APPLICABLE
        ),
        active=applicable and remaining > 0,
        remaining_ms=remaining if applicable else None,
        ready=(remaining == 0) if applicable else None,
    )


def _limited_lifecycle(
    operator: OperatorState,
    authority: OperatorLifecycleAuthority,
    issues: _IntegrityCollector,
) -> LimitedLifecycleView:
    profile_id = operator.lifecycle_profile_id
    remaining = operator.lifecycle_remaining_ms
    if profile_id is None and remaining is None:
        return LimitedLifecycleView(
            Applicability.NOT_APPLICABLE,
            False,
            None,
            LimitedLifecycleState.NOT_APPLICABLE,
            None,
        )
    if (profile_id is None) != (remaining is None):
        issues.add(IntegrityStatus.INCONSISTENT, "LIFECYCLE_PROFILE_TIME_MISMATCH")
        return LimitedLifecycleView(
            Applicability.MISSING,
            True,
            profile_id,
            LimitedLifecycleState.INCONSISTENT,
            remaining,
        )
    profile = authority.profile(profile_id or "")
    if profile is None:
        issues.add(IntegrityStatus.MISSING, "LIFECYCLE_PROFILE_AUTHORITY_MISSING")
        state = LimitedLifecycleState.MISSING
        applicability = Applicability.MISSING
    elif remaining is not None and remaining < 0:
        issues.add(IntegrityStatus.INCONSISTENT, "NEGATIVE_LIFECYCLE_TIMER")
        state = LimitedLifecycleState.INCONSISTENT
        applicability = Applicability.APPLICABLE
    elif remaining == 0:
        state = LimitedLifecycleState.EXPIRED
        applicability = Applicability.APPLICABLE
        if operator.alive:
            issues.add(IntegrityStatus.INCONSISTENT, "ALIVE_WITH_EXPIRED_LIFECYCLE")
    else:
        state = LimitedLifecycleState.ACTIVE
        applicability = Applicability.APPLICABLE
    return LimitedLifecycleView(
        applicability=applicability,
        enabled=True,
        profile_id=profile_id,
        state=state,
        remaining_ms=remaining,
    )


def _legacy_integrity(
    operator: OperatorState,
    boarding_role: BoardingRole,
    issues: _IntegrityCollector,
) -> None:
    legacy = str(operator.state).upper()
    if legacy == "CLOSE_COMBAT":
        issues.add(IntegrityStatus.UNKNOWN, "LEGACY_CLOSE_COMBAT_WITHOUT_TYPED_AXIS")
    if legacy == "DESTROYED" and operator.alive:
        issues.add(IntegrityStatus.INCONSISTENT, "LEGACY_DESTROYED_CONFLICT")
    if legacy == "ON_BOARD" and boarding_role is not BoardingRole.PASSENGER:
        issues.add(IntegrityStatus.INCONSISTENT, "LEGACY_ON_BOARD_CONFLICT")
    if legacy == "SUPPRESSED" and operator.suppression_remaining_ms == 0:
        issues.add(IntegrityStatus.INCONSISTENT, "LEGACY_SUPPRESSION_CONFLICT")


def _view_semantic_hash(view: AuthoritativeOperatorLifecycleView) -> str:
    payload = {
        item.name: getattr(view, item.name)
        for item in fields(view)
        if item.name != "semantic_hash"
    }
    return canonical_hash(payload)


__all__ = [
    "OPERATOR_LIFECYCLE_VIEW_SCHEMA_VERSION",
    "Applicability",
    "AuthoritativeOperatorLifecycleView",
    "BoardingLifecycleState",
    "BoardingRole",
    "IdentityStatus",
    "IntegrityStatus",
    "LimitedLifecycleState",
    "MovementLifecycleState",
    "OperatorLifecycleProjectionAuthority",
    "SpatialStatus",
    "WeaponPosture",
    "default_projection_authority",
    "project_operator_lifecycle",
    "project_world_operator_lifecycles",
]
