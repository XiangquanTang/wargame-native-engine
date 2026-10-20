"""Immutable, perspective-safe Decision Semantic Projection core.

The values in this module compose already-detached lifecycle, capability, and
action-catalog evidence.  They never read WorldState, call Engine services,
advance time, enumerate actions, or decide legality.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
import math
import re
from typing import Iterable

from wargame_native_engine.core.world import Stage
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.projection.operator_lifecycle import (
    BoardingView,
    ConcealmentView,
    DeploymentView,
    FatigueView,
    IdentityView,
    LimitedLifecycleView,
    MovementView,
    OperatorTransitionView,
    RecoveryView,
    SuppressionView,
    SurvivalView,
    WeaponLifecycleView,
)
from wargame_native_engine.projection.operator_perspective import (
    OPERATOR_LIFECYCLE_PERSPECTIVE_SCHEMA_VERSION,
    ObservedEnemyIdentityPayload,
    ObservedEnemyObservationPayload,
    ObservedEnemyOperatorLifecyclePayload,
    ObservedEnemySurvivalPayload,
    OwnOperatorLifecyclePayload,
)
from wargame_native_engine.rules.perspective import (
    PerspectivePolicy,
    load_perspective_policy,
)
from wargame_native_engine.version import ENGINE_VERSION


DECISION_SEMANTIC_PROJECTION_SCHEMA_VERSION = (
    "native-decision-semantic-projection/1.0"
)
DECISION_CAPABILITY_SNAPSHOT_SCHEMA_VERSION = (
    "native-decision-capability-snapshot/1.0"
)
DECISION_ACTION_CATALOG_SCHEMA_VERSION = "native-decision-action-catalog/1.0"

_HASH = re.compile(r"^[0-9a-f]{64}$")
_STABLE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")

_OWN_PAYLOAD_FIELDS = {
    "operator_reference",
    "identity",
    "survival",
    "deployment",
    "movement",
    "operator_transition",
    "fatigue",
    "suppression",
    "concealment",
    "boarding",
    "weapon_lifecycle",
    "guided_recovery",
    "indirect_planning",
    "limited_lifecycle",
}
_ENEMY_PAYLOAD_FIELDS = {
    "operator_reference",
    "side",
    "identity",
    "observation",
    "survival",
    "motion",
}


class DecisionProjectionContractError(ValueError):
    """A supplied safe-input bundle violates the frozen Decision contract."""


class DecisionSide(str, Enum):
    RED = "red"
    BLUE = "blue"


class CapabilityAvailabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"
    MISSING = "MISSING"


class CapabilityEvidenceKind(str, Enum):
    ENGINE_VALID_ACTION_INSTANCE = "ENGINE_VALID_ACTION_INSTANCE"
    ENGINE_CAPABILITY_RECORD = "ENGINE_CAPABILITY_RECORD"
    EXPLICIT_APPLICABILITY_AUTHORITY = "EXPLICIT_APPLICABILITY_AUTHORITY"
    NO_CONCLUSIVE_ENGINE_EVIDENCE = "NO_CONCLUSIVE_ENGINE_EVIDENCE"
    REQUIRED_EVIDENCE_MISSING = "REQUIRED_EVIDENCE_MISSING"


class DecisionCapabilityFamily(str, Enum):
    DEPLOY = "DEPLOY"
    END_DEPLOYMENT = "END_DEPLOYMENT"
    MOVE = "MOVE"
    STOP_MOVE = "STOP_MOVE"
    WAIT = "WAIT"
    DIRECT_FIRE = "DIRECT_FIRE"
    GUIDED_FIRE = "GUIDED_FIRE"
    INDIRECT_FIRE = "INDIRECT_FIRE"
    BOARD = "BOARD"
    UNLOAD = "UNLOAD"
    ENTER_CONCEALMENT = "ENTER_CONCEALMENT"
    WEAPON_LOCK = "WEAPON_LOCK"
    WEAPON_UNFOLD = "WEAPON_UNFOLD"
    CHANGE_STATE = "CHANGE_STATE"
    CAPTURE = "CAPTURE"


@dataclass(frozen=True, slots=True)
class AuthorityMetadata:
    engine_version: str
    ruleset_id: str
    ruleset_version: str
    ruleset_hash: str
    perspective_schema_version: str
    perspective_policy_id: str
    perspective_policy_version: str
    perspective_policy_hash: str
    capability_snapshot_schema_version: str
    action_catalog_schema_version: str

    def __post_init__(self) -> None:
        for name in (
            "engine_version",
            "ruleset_id",
            "ruleset_version",
            "perspective_policy_id",
            "perspective_policy_version",
        ):
            _text(getattr(self, name), name)
        _sha256(self.ruleset_hash, "ruleset_hash")
        _sha256(self.perspective_policy_hash, "perspective_policy_hash")
        if (
            self.perspective_schema_version
            != OPERATOR_LIFECYCLE_PERSPECTIVE_SCHEMA_VERSION
        ):
            raise DecisionProjectionContractError(
                "unsupported perspective lifecycle schema"
            )
        if (
            self.capability_snapshot_schema_version
            != DECISION_CAPABILITY_SNAPSHOT_SCHEMA_VERSION
        ):
            raise DecisionProjectionContractError(
                "unsupported capability snapshot schema"
            )
        if (
            self.action_catalog_schema_version
            != DECISION_ACTION_CATALOG_SCHEMA_VERSION
        ):
            raise DecisionProjectionContractError(
                "unsupported action catalog schema"
            )


@dataclass(frozen=True, slots=True)
class DecisionSnapshotIdentity:
    side: DecisionSide
    decision_window_index: int
    frame_index: int
    sim_time_ms: int
    stage: Stage
    terminated: bool
    truncated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.side, DecisionSide):
            raise TypeError("side must be DecisionSide")
        for name in ("decision_window_index", "frame_index", "sim_time_ms"):
            _nonnegative_int(getattr(self, name), name)
        if not isinstance(self.stage, Stage):
            raise TypeError("stage must be Stage")
        _exact_bool(self.terminated, "terminated")
        _exact_bool(self.truncated, "truncated")
        if self.terminated and self.truncated:
            raise DecisionProjectionContractError(
                "decision snapshot cannot be terminated and truncated"
            )


@dataclass(frozen=True, slots=True)
class DecisionObjective:
    objective_id: int
    x: float
    y: float
    radius: float
    value: int
    controlling_side: str | None

    def __post_init__(self) -> None:
        _nonnegative_int(self.objective_id, "objective_id")
        for name in ("x", "y", "radius"):
            number = getattr(self, name)
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise TypeError(f"{name} must be a finite number")
            if not math.isfinite(float(number)):
                raise DecisionProjectionContractError(
                    f"{name} must be a finite number"
                )
        if float(self.radius) < 0:
            raise DecisionProjectionContractError("radius must be non-negative")
        if type(self.value) is not int:
            raise TypeError("objective value must be an exact int")
        if self.controlling_side is not None and self.controlling_side not in {
            DecisionSide.RED.value,
            DecisionSide.BLUE.value,
        }:
            raise DecisionProjectionContractError(
                "objective controlling_side must be RED, BLUE, or None"
            )


@dataclass(frozen=True, slots=True)
class DecisionTerminalRules:
    elimination: bool
    objective_score_to_win: int | None
    max_ticks: int | None

    def __post_init__(self) -> None:
        _exact_bool(self.elimination, "elimination")
        for name in ("objective_score_to_win", "max_ticks"):
            value = getattr(self, name)
            if value is not None:
                _nonnegative_int(value, name)


@dataclass(frozen=True, slots=True)
class DecisionMissionContext:
    terminal_rules: DecisionTerminalRules

    def __post_init__(self) -> None:
        if not isinstance(self.terminal_rules, DecisionTerminalRules):
            raise TypeError("terminal_rules must be DecisionTerminalRules")


@dataclass(frozen=True, slots=True)
class SideDecisionContext:
    side: DecisionSide
    objectives: tuple[DecisionObjective, ...]
    mission: DecisionMissionContext
    controllable_operator_references: tuple[int, ...]
    observed_contact_references: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.side, DecisionSide):
            raise TypeError("side must be DecisionSide")
        if not isinstance(self.mission, DecisionMissionContext):
            raise TypeError("mission must be DecisionMissionContext")
        objectives = tuple(self.objectives)
        if any(type(value) is not DecisionObjective for value in objectives):
            raise TypeError("objectives must contain DecisionObjective values")
        objectives = tuple(sorted(objectives, key=lambda item: item.objective_id))
        if len({item.objective_id for item in objectives}) != len(objectives):
            raise DecisionProjectionContractError("duplicate objective reference")
        controllable = _sorted_references(
            self.controllable_operator_references,
            "controllable_operator_references",
        )
        contacts = _sorted_references(
            self.observed_contact_references,
            "observed_contact_references",
        )
        if set(controllable) & set(contacts):
            raise DecisionProjectionContractError(
                "own and observed references must be disjoint"
            )
        object.__setattr__(self, "objectives", objectives)
        object.__setattr__(self, "controllable_operator_references", controllable)
        object.__setattr__(self, "observed_contact_references", contacts)


@dataclass(frozen=True, slots=True)
class CapabilityBlockerCode:
    producer_namespace: str
    code: str

    def __post_init__(self) -> None:
        _token(self.producer_namespace, "producer_namespace")
        _token(self.code, "blocker code")


@dataclass(frozen=True, slots=True)
class CapabilityFamilyStatus:
    family: DecisionCapabilityFamily
    status: CapabilityAvailabilityStatus
    producer_namespace: str
    evidence_kind: CapabilityEvidenceKind
    available_instance_count: int | None = None
    blocker_codes: tuple[CapabilityBlockerCode, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.family, DecisionCapabilityFamily):
            raise TypeError("family must be DecisionCapabilityFamily")
        if not isinstance(self.status, CapabilityAvailabilityStatus):
            raise TypeError("status must be CapabilityAvailabilityStatus")
        if not isinstance(self.evidence_kind, CapabilityEvidenceKind):
            raise TypeError("evidence_kind must be CapabilityEvidenceKind")
        _token(self.producer_namespace, "producer_namespace")
        count = self.available_instance_count
        if count is not None:
            _nonnegative_int(count, "available_instance_count")
        blockers = tuple(self.blocker_codes)
        if any(type(value) is not CapabilityBlockerCode for value in blockers):
            raise TypeError("blocker_codes must contain CapabilityBlockerCode")
        blockers = tuple(sorted(
            blockers,
            key=lambda item: (item.producer_namespace, item.code),
        ))
        if len(set(blockers)) != len(blockers):
            raise DecisionProjectionContractError("duplicate capability blocker")
        self._validate_evidence(count, blockers)
        object.__setattr__(self, "blocker_codes", blockers)

    def _validate_evidence(
        self,
        count: int | None,
        blockers: tuple[CapabilityBlockerCode, ...],
    ) -> None:
        status = self.status
        evidence = self.evidence_kind
        if status is CapabilityAvailabilityStatus.AVAILABLE:
            if blockers:
                raise DecisionProjectionContractError(
                    "AVAILABLE capability cannot contain blockers"
                )
            if evidence not in {
                CapabilityEvidenceKind.ENGINE_VALID_ACTION_INSTANCE,
                CapabilityEvidenceKind.ENGINE_CAPABILITY_RECORD,
            }:
                raise DecisionProjectionContractError(
                    "AVAILABLE requires explicit Engine evidence"
                )
            if (
                evidence is CapabilityEvidenceKind.ENGINE_VALID_ACTION_INSTANCE
                and (count is None or count <= 0)
            ):
                raise DecisionProjectionContractError(
                    "valid-action availability requires a positive instance count"
                )
            return
        if count is not None:
            raise DecisionProjectionContractError(
                "non-AVAILABLE capability cannot contain an instance count"
            )
        if status is CapabilityAvailabilityStatus.BLOCKED:
            if (
                evidence is not CapabilityEvidenceKind.ENGINE_CAPABILITY_RECORD
                or not blockers
            ):
                raise DecisionProjectionContractError(
                    "BLOCKED requires explicit Engine blocker evidence"
                )
            return
        if blockers:
            raise DecisionProjectionContractError(
                "only BLOCKED capability may contain blocker codes"
            )
        required = {
            CapabilityAvailabilityStatus.NOT_APPLICABLE:
                CapabilityEvidenceKind.EXPLICIT_APPLICABILITY_AUTHORITY,
            CapabilityAvailabilityStatus.UNKNOWN:
                CapabilityEvidenceKind.NO_CONCLUSIVE_ENGINE_EVIDENCE,
            CapabilityAvailabilityStatus.MISSING:
                CapabilityEvidenceKind.REQUIRED_EVIDENCE_MISSING,
        }[status]
        if evidence is not required:
            raise DecisionProjectionContractError(
                f"{status.value} requires {required.value}"
            )


@dataclass(frozen=True, slots=True)
class OperatorCapabilitySummary:
    operator_reference: int
    families: tuple[CapabilityFamilyStatus, ...]

    def __post_init__(self) -> None:
        _nonnegative_int(self.operator_reference, "operator_reference")
        families = tuple(self.families)
        if any(type(value) is not CapabilityFamilyStatus for value in families):
            raise TypeError("families must contain CapabilityFamilyStatus")
        families = tuple(sorted(families, key=lambda item: item.family.value))
        if len({item.family for item in families}) != len(families):
            raise DecisionProjectionContractError("duplicate capability family")
        object.__setattr__(self, "families", families)


@dataclass(frozen=True, slots=True)
class DecisionCapabilitySnapshot:
    schema_version: str
    side: DecisionSide
    decision_window_index: int
    frame_index: int
    sim_time_ms: int
    operators: tuple[OperatorCapabilitySummary, ...]
    semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != DECISION_CAPABILITY_SNAPSHOT_SCHEMA_VERSION:
            raise DecisionProjectionContractError(
                "unsupported capability snapshot schema"
            )
        if not isinstance(self.side, DecisionSide):
            raise TypeError("side must be DecisionSide")
        for name in ("decision_window_index", "frame_index", "sim_time_ms"):
            _nonnegative_int(getattr(self, name), name)
        operators = tuple(self.operators)
        if any(type(value) is not OperatorCapabilitySummary for value in operators):
            raise TypeError("operators must contain OperatorCapabilitySummary")
        operators = tuple(sorted(
            operators,
            key=lambda item: item.operator_reference,
        ))
        if len({item.operator_reference for item in operators}) != len(operators):
            raise DecisionProjectionContractError(
                "duplicate capability operator reference"
            )
        object.__setattr__(self, "operators", operators)
        object.__setattr__(
            self,
            "semantic_hash",
            canonical_hash(_capability_snapshot_payload(self)),
        )

    def to_dict(self) -> dict[str, object]:
        return _capability_snapshot_record(self)


@dataclass(frozen=True, slots=True)
class ActionCatalogReference:
    schema_version: str
    side: DecisionSide
    decision_window_index: int
    frame_index: int
    sim_time_ms: int
    action_count: int
    action_ids: tuple[str, ...]
    action_entry_hashes: tuple[str, ...]
    actor_references: tuple[int, ...]
    catalog_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != DECISION_ACTION_CATALOG_SCHEMA_VERSION:
            raise DecisionProjectionContractError(
                "unsupported action catalog schema"
            )
        if not isinstance(self.side, DecisionSide):
            raise TypeError("side must be DecisionSide")
        for name in (
            "decision_window_index",
            "frame_index",
            "sim_time_ms",
            "action_count",
        ):
            _nonnegative_int(getattr(self, name), name)
        action_ids = tuple(self.action_ids)
        if any(not isinstance(value, str) or not value for value in action_ids):
            raise TypeError("action_ids must be non-empty strings")
        if len(set(action_ids)) != len(action_ids):
            raise DecisionProjectionContractError("duplicate action identity")
        if self.action_count != len(action_ids):
            raise DecisionProjectionContractError("action_count mismatch")
        entry_hashes = tuple(self.action_entry_hashes)
        if len(entry_hashes) != len(action_ids):
            raise DecisionProjectionContractError(
                "action entry hash count mismatch"
            )
        for value in entry_hashes:
            _sha256(value, "action_entry_hash")
        actors = _sorted_references(self.actor_references, "actor_references")
        expected_hash = action_catalog_reference_hash(
            schema_version=self.schema_version,
            side=self.side,
            decision_window_index=self.decision_window_index,
            frame_index=self.frame_index,
            sim_time_ms=self.sim_time_ms,
            action_ids=action_ids,
            action_entry_hashes=entry_hashes,
            actor_references=actors,
        )
        if self.catalog_hash != expected_hash:
            raise DecisionProjectionContractError("action catalog hash mismatch")
        object.__setattr__(self, "action_ids", action_ids)
        object.__setattr__(self, "action_entry_hashes", entry_hashes)
        object.__setattr__(self, "actor_references", actors)


@dataclass(frozen=True, slots=True)
class SideDecisionSemanticProjection:
    schema_version: str
    authority_metadata: AuthorityMetadata
    snapshot: DecisionSnapshotIdentity
    side_context: SideDecisionContext
    own_operators: tuple[OwnOperatorLifecyclePayload, ...]
    observed_enemies: tuple[ObservedEnemyOperatorLifecyclePayload, ...]
    capabilities: DecisionCapabilitySnapshot
    action_space: ActionCatalogReference
    semantic_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != DECISION_SEMANTIC_PROJECTION_SCHEMA_VERSION:
            raise DecisionProjectionContractError(
                "unsupported Decision Semantic Projection schema"
            )
        _sha256(self.semantic_hash, "semantic_hash")
        expected = canonical_hash(_decision_semantic_payload(
            schema_version=self.schema_version,
            authority_metadata=self.authority_metadata,
            snapshot=self.snapshot,
            side_context=self.side_context,
            own_operators=self.own_operators,
            observed_enemies=self.observed_enemies,
            capabilities=self.capabilities,
            action_space=self.action_space,
        ))
        if self.semantic_hash != expected:
            raise DecisionProjectionContractError("decision semantic hash mismatch")

    def to_dict(self) -> dict[str, object]:
        return {
            **_decision_semantic_payload(
                schema_version=self.schema_version,
                authority_metadata=self.authority_metadata,
                snapshot=self.snapshot,
                side_context=self.side_context,
                own_operators=self.own_operators,
                observed_enemies=self.observed_enemies,
                capabilities=self.capabilities,
                action_space=self.action_space,
            ),
            "semantic_hash": self.semantic_hash,
        }


def current_decision_authority_metadata(
    *,
    ruleset_id: str,
    ruleset_version: str,
    ruleset_hash: str,
    engine_version: str = ENGINE_VERSION,
    perspective_policy: PerspectivePolicy | None = None,
) -> AuthorityMetadata:
    """Bind current versioned authorities without inventing another policy ID."""

    policy = (
        load_perspective_policy()
        if perspective_policy is None
        else perspective_policy
    )
    if not isinstance(policy, PerspectivePolicy):
        raise TypeError("perspective_policy must be PerspectivePolicy")
    return AuthorityMetadata(
        engine_version=engine_version,
        ruleset_id=ruleset_id,
        ruleset_version=ruleset_version,
        ruleset_hash=ruleset_hash,
        perspective_schema_version=(
            OPERATOR_LIFECYCLE_PERSPECTIVE_SCHEMA_VERSION
        ),
        perspective_policy_id=policy.policy_id,
        perspective_policy_version=policy.policy_version,
        perspective_policy_hash=policy.content_hash,
        capability_snapshot_schema_version=(
            DECISION_CAPABILITY_SNAPSHOT_SCHEMA_VERSION
        ),
        action_catalog_schema_version=DECISION_ACTION_CATALOG_SCHEMA_VERSION,
    )


def action_catalog_reference_hash(
    *,
    schema_version: str,
    side: DecisionSide,
    decision_window_index: int,
    frame_index: int,
    sim_time_ms: int,
    action_ids: Iterable[str],
    action_entry_hashes: Iterable[str],
    actor_references: Iterable[int],
) -> str:
    """Hash supplied catalog identities without producing or validating actions."""

    ids = tuple(action_ids)
    entry_hashes = tuple(action_entry_hashes)
    if len(entry_hashes) != len(ids):
        raise DecisionProjectionContractError("action entry hash count mismatch")
    for value in entry_hashes:
        _sha256(value, "action_entry_hash")
    actors = _sorted_references(actor_references, "actor_references")
    return canonical_hash({
        "schema_version": schema_version,
        "side": side.value,
        "decision_window_index": decision_window_index,
        "frame_index": frame_index,
        "sim_time_ms": sim_time_ms,
        "action_count": len(ids),
        "action_ids": list(ids),
        "action_entry_hashes": list(entry_hashes),
        "actor_references": list(actors),
    })


def build_decision_semantic_projection(
    *,
    authority_metadata: AuthorityMetadata,
    snapshot: DecisionSnapshotIdentity,
    side_context: SideDecisionContext,
    own_operators: Iterable[OwnOperatorLifecyclePayload],
    observed_enemies: Iterable[ObservedEnemyOperatorLifecyclePayload],
    capability_snapshot: DecisionCapabilitySnapshot,
    action_catalog_reference: ActionCatalogReference,
) -> SideDecisionSemanticProjection:
    """Compose already-safe immutable evidence and fail closed on every join."""

    if type(authority_metadata) is not AuthorityMetadata:
        raise TypeError("authority_metadata must be AuthorityMetadata")
    if type(snapshot) is not DecisionSnapshotIdentity:
        raise TypeError("snapshot must be DecisionSnapshotIdentity")
    if type(side_context) is not SideDecisionContext:
        raise TypeError("side_context must be SideDecisionContext")
    if type(capability_snapshot) is not DecisionCapabilitySnapshot:
        raise TypeError("capability_snapshot must be DecisionCapabilitySnapshot")
    if type(action_catalog_reference) is not ActionCatalogReference:
        raise TypeError("action_catalog_reference must be ActionCatalogReference")

    own = tuple(own_operators)
    enemies = tuple(observed_enemies)
    if any(type(value) is not OwnOperatorLifecyclePayload for value in own):
        raise TypeError("own_operators must contain exact OWN lifecycle payloads")
    if any(
        type(value) is not ObservedEnemyOperatorLifecyclePayload
        for value in enemies
    ):
        raise TypeError(
            "observed_enemies must contain exact observed-enemy payloads"
        )
    _validate_perspective_payload_schema()
    for value in own:
        _validate_own_payload_types(value)
    for value in enemies:
        _validate_enemy_payload_types(value)
    own = tuple(sorted(own, key=lambda item: item.operator_reference))
    enemies = tuple(sorted(enemies, key=lambda item: item.operator_reference))
    _validate_unique_payload_references(own, "own lifecycle")
    _validate_unique_payload_references(enemies, "observed enemy")
    _validate_projection_joins(
        authority_metadata,
        snapshot,
        side_context,
        own,
        enemies,
        capability_snapshot,
        action_catalog_reference,
    )
    semantic_payload = _decision_semantic_payload(
        schema_version=DECISION_SEMANTIC_PROJECTION_SCHEMA_VERSION,
        authority_metadata=authority_metadata,
        snapshot=snapshot,
        side_context=side_context,
        own_operators=own,
        observed_enemies=enemies,
        capabilities=capability_snapshot,
        action_space=action_catalog_reference,
    )
    return SideDecisionSemanticProjection(
        schema_version=DECISION_SEMANTIC_PROJECTION_SCHEMA_VERSION,
        authority_metadata=authority_metadata,
        snapshot=snapshot,
        side_context=side_context,
        own_operators=own,
        observed_enemies=enemies,
        capabilities=capability_snapshot,
        action_space=action_catalog_reference,
        semantic_hash=canonical_hash(semantic_payload),
    )


def _validate_projection_joins(
    metadata: AuthorityMetadata,
    snapshot: DecisionSnapshotIdentity,
    context: SideDecisionContext,
    own: tuple[OwnOperatorLifecyclePayload, ...],
    enemies: tuple[ObservedEnemyOperatorLifecyclePayload, ...],
    capabilities: DecisionCapabilitySnapshot,
    action_space: ActionCatalogReference,
) -> None:
    if metadata.capability_snapshot_schema_version != capabilities.schema_version:
        raise DecisionProjectionContractError("capability schema identity mismatch")
    if metadata.action_catalog_schema_version != action_space.schema_version:
        raise DecisionProjectionContractError("action catalog schema identity mismatch")
    if not (snapshot.side is context.side is capabilities.side is action_space.side):
        raise DecisionProjectionContractError("decision side identity mismatch")
    for value, name in (
        (capabilities.decision_window_index, "capability decision window"),
        (action_space.decision_window_index, "action catalog decision window"),
    ):
        if value != snapshot.decision_window_index:
            raise DecisionProjectionContractError(f"{name} mismatch")
    for value, name in (
        (capabilities.frame_index, "capability frame"),
        (action_space.frame_index, "action catalog frame"),
    ):
        if value != snapshot.frame_index:
            raise DecisionProjectionContractError(f"{name} mismatch")
    for value, name in (
        (capabilities.sim_time_ms, "capability simulation time"),
        (action_space.sim_time_ms, "action catalog simulation time"),
    ):
        if value != snapshot.sim_time_ms:
            raise DecisionProjectionContractError(f"{name} mismatch")

    own_refs = tuple(item.operator_reference for item in own)
    enemy_refs = tuple(item.operator_reference for item in enemies)
    capability_refs = tuple(
        item.operator_reference for item in capabilities.operators
    )
    if own_refs != context.controllable_operator_references:
        raise DecisionProjectionContractError(
            "own lifecycle/controllable reference mismatch"
        )
    if capability_refs != own_refs:
        raise DecisionProjectionContractError(
            "capability/own lifecycle reference mismatch"
        )
    if enemy_refs != context.observed_contact_references:
        raise DecisionProjectionContractError(
            "observed lifecycle/contact reference mismatch"
        )
    if set(own_refs) & set(enemy_refs):
        raise DecisionProjectionContractError(
            "own and enemy lifecycle references overlap"
        )
    if not set(action_space.actor_references).issubset(own_refs):
        raise DecisionProjectionContractError(
            "action catalog contains a non-controllable actor reference"
        )
    for value in own:
        if value.identity.side.lower() != snapshot.side.value:
            raise DecisionProjectionContractError("own lifecycle side mismatch")
    for value in enemies:
        if value.side.lower() == snapshot.side.value:
            raise DecisionProjectionContractError("enemy lifecycle side mismatch")


def _decision_semantic_payload(
    *,
    schema_version: str,
    authority_metadata: AuthorityMetadata,
    snapshot: DecisionSnapshotIdentity,
    side_context: SideDecisionContext,
    own_operators: tuple[OwnOperatorLifecyclePayload, ...],
    observed_enemies: tuple[ObservedEnemyOperatorLifecyclePayload, ...],
    capabilities: DecisionCapabilitySnapshot,
    action_space: ActionCatalogReference,
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "authority_metadata": _authority_record(authority_metadata),
        "snapshot": _snapshot_record(snapshot),
        "side_context": _side_context_record(side_context),
        "own_operators": [_own_lifecycle_record(value) for value in own_operators],
        "observed_enemies": [
            _enemy_lifecycle_record(value) for value in observed_enemies
        ],
        "capabilities": _capability_snapshot_record(capabilities),
        "action_space": _action_catalog_record(action_space),
    }


def _authority_record(value: AuthorityMetadata) -> dict[str, object]:
    return {
        "engine_version": value.engine_version,
        "ruleset_id": value.ruleset_id,
        "ruleset_version": value.ruleset_version,
        "ruleset_hash": value.ruleset_hash,
        "perspective_schema_version": value.perspective_schema_version,
        "perspective_policy_id": value.perspective_policy_id,
        "perspective_policy_version": value.perspective_policy_version,
        "perspective_policy_hash": value.perspective_policy_hash,
        "capability_snapshot_schema_version": (
            value.capability_snapshot_schema_version
        ),
        "action_catalog_schema_version": value.action_catalog_schema_version,
    }


def _snapshot_record(value: DecisionSnapshotIdentity) -> dict[str, object]:
    return {
        "side": value.side.value,
        "decision_window_index": value.decision_window_index,
        "frame_index": value.frame_index,
        "sim_time_ms": value.sim_time_ms,
        "stage": value.stage.value,
        "terminated": value.terminated,
        "truncated": value.truncated,
    }


def _objective_record(value: DecisionObjective) -> dict[str, object]:
    return {
        "objective_id": value.objective_id,
        "x": float(value.x),
        "y": float(value.y),
        "radius": float(value.radius),
        "value": value.value,
        "controlling_side": value.controlling_side,
    }


def _side_context_record(value: SideDecisionContext) -> dict[str, object]:
    rules = value.mission.terminal_rules
    return {
        "side": value.side.value,
        "objectives": [_objective_record(item) for item in value.objectives],
        "mission": {
            "terminal_rules": {
                "elimination": rules.elimination,
                "objective_score_to_win": rules.objective_score_to_win,
                "max_ticks": rules.max_ticks,
            },
        },
        "controllable_operator_references": list(
            value.controllable_operator_references
        ),
        "observed_contact_references": list(value.observed_contact_references),
    }


def _own_lifecycle_record(
    value: OwnOperatorLifecyclePayload,
) -> dict[str, object]:
    return {
        "operator_reference": value.operator_reference,
        "identity": _identity_record(value.identity),
        "survival": _survival_record(value.survival),
        "deployment": _deployment_record(value.deployment),
        "movement": _movement_record(value.movement),
        "operator_transition": _transition_record(value.operator_transition),
        "fatigue": _fatigue_record(value.fatigue),
        "suppression": _suppression_record(value.suppression),
        "concealment": _concealment_record(value.concealment),
        "boarding": _boarding_record(value.boarding),
        "weapon_lifecycle": _weapon_record(value.weapon_lifecycle),
        "guided_recovery": _recovery_record(value.guided_recovery),
        "indirect_planning": _recovery_record(value.indirect_planning),
        "limited_lifecycle": _limited_lifecycle_record(
            value.limited_lifecycle
        ),
    }


def _enemy_lifecycle_record(
    value: ObservedEnemyOperatorLifecyclePayload,
) -> dict[str, object]:
    return {
        "operator_reference": value.operator_reference,
        "side": value.side,
        "identity": {
            "operator_type": value.identity.operator_type,
            "subtype": value.identity.subtype,
            "source_type_code": value.identity.source_type_code,
            "source_subtype_code": value.identity.source_subtype_code,
            "canonical_identity": value.identity.canonical_identity,
            "canonical_status": value.identity.canonical_status.value,
            "domain": value.identity.domain,
        },
        "observation": {
            "position": list(value.observation.position),
            "altitude": value.observation.altitude,
        },
        "survival": {
            "alive": value.survival.alive,
            "destroyed": value.survival.destroyed,
        },
        "motion": value.motion.value,
    }


def _identity_record(value: IdentityView) -> dict[str, object]:
    return {
        "side": value.side,
        "source_type": value.source_type,
        "source_subtype": value.source_subtype,
        "canonical_identity": value.canonical_identity,
        "canonical_status": value.canonical_status.value,
        "canonical_source": value.canonical_source,
        "domain": value.domain,
    }


def _survival_record(value: SurvivalView) -> dict[str, object]:
    return {
        "alive": value.alive,
        "destroyed": value.destroyed,
        "health": value.health,
        "max_health": value.max_health,
        "blood": value.blood,
        "max_blood": value.max_blood,
    }


def _deployment_record(value: DeploymentView) -> dict[str, object]:
    return {
        "deployed": value.deployed,
        "spatially_present": value.spatially_present,
        "spatial_status": value.spatial_status.value,
        "current_hex": value.current_hex,
        "on_board": value.on_board,
    }


def _movement_record(value: MovementView) -> dict[str, object]:
    return {
        "state": value.state.value,
        "mode": value.mode,
        "speed": value.speed,
        "has_active_segment": value.has_active_segment,
        "segment_progress_millionths": value.segment_progress_millionths,
        "segment_remaining_ms": value.segment_remaining_ms,
        "has_remaining_task": value.has_remaining_task,
        "remaining_path_length": value.remaining_path_length,
        "pause_pending": value.pause_pending,
        "paused": value.paused,
        "pause_reason": value.pause_reason,
        "blocked_reason": value.blocked_reason,
        "stop_transition_remaining_ms": value.stop_transition_remaining_ms,
    }


def _transition_record(value: OperatorTransitionView) -> dict[str, object]:
    return {
        "active": value.active,
        "kind": value.kind,
        "remaining_ms": value.remaining_ms,
    }


def _fatigue_record(value: FatigueView) -> dict[str, object]:
    return {
        "applicability": value.applicability.value,
        "level": value.level,
        "recovery_elapsed_ms": value.recovery_elapsed_ms,
        "recovery_remaining_ms": value.recovery_remaining_ms,
    }


def _suppression_record(value: SuppressionView) -> dict[str, object]:
    return {"active": value.active, "remaining_ms": value.remaining_ms}


def _concealment_record(value: ConcealmentView) -> dict[str, object]:
    return {
        "applicability": value.applicability.value,
        "posture": value.posture,
    }


def _boarding_record(value: BoardingView) -> dict[str, object]:
    return {
        "applicability": value.applicability.value,
        "role": value.role.value,
        "state": value.state.value,
        "transition_type": value.transition_type,
        "transition_remaining_ms": value.transition_remaining_ms,
        "movement_locked": value.movement_locked,
    }


def _weapon_record(value: WeaponLifecycleView) -> dict[str, object]:
    return {
        "applicability": value.applicability.value,
        "posture": value.posture.value,
        "cooldown_active": value.cooldown_active,
        "cooldown_remaining_ms": value.cooldown_remaining_ms,
        "cooldown_clear": value.cooldown_clear,
    }


def _recovery_record(value: RecoveryView) -> dict[str, object]:
    return {
        "applicability": value.applicability.value,
        "active": value.active,
        "remaining_ms": value.remaining_ms,
        "ready": value.ready,
    }


def _limited_lifecycle_record(
    value: LimitedLifecycleView,
) -> dict[str, object]:
    return {
        "applicability": value.applicability.value,
        "enabled": value.enabled,
        "profile_id": value.profile_id,
        "state": value.state.value,
        "remaining_ms": value.remaining_ms,
    }


def _validate_perspective_payload_schema() -> None:
    if {item.name for item in fields(OwnOperatorLifecyclePayload)} != (
        _OWN_PAYLOAD_FIELDS
    ):
        raise DecisionProjectionContractError(
            "OWN lifecycle payload schema changed without Decision version bump"
        )
    if {item.name for item in fields(ObservedEnemyOperatorLifecyclePayload)} != (
        _ENEMY_PAYLOAD_FIELDS
    ):
        raise DecisionProjectionContractError(
            "enemy lifecycle payload schema changed without Decision version bump"
        )


def _validate_own_payload_types(value: OwnOperatorLifecyclePayload) -> None:
    expected = (
        (value.identity, IdentityView, "identity"),
        (value.survival, SurvivalView, "survival"),
        (value.deployment, DeploymentView, "deployment"),
        (value.movement, MovementView, "movement"),
        (value.operator_transition, OperatorTransitionView, "transition"),
        (value.fatigue, FatigueView, "fatigue"),
        (value.suppression, SuppressionView, "suppression"),
        (value.concealment, ConcealmentView, "concealment"),
        (value.boarding, BoardingView, "boarding"),
        (value.weapon_lifecycle, WeaponLifecycleView, "weapon lifecycle"),
        (value.guided_recovery, RecoveryView, "guided recovery"),
        (value.indirect_planning, RecoveryView, "indirect planning"),
        (value.limited_lifecycle, LimitedLifecycleView, "limited lifecycle"),
    )
    for supplied, expected_type, label in expected:
        if type(supplied) is not expected_type:
            raise DecisionProjectionContractError(
                f"unexpected OWN lifecycle {label} schema"
            )


def _validate_enemy_payload_types(
    value: ObservedEnemyOperatorLifecyclePayload,
) -> None:
    expected = (
        (value.identity, ObservedEnemyIdentityPayload, "identity"),
        (value.observation, ObservedEnemyObservationPayload, "observation"),
        (value.survival, ObservedEnemySurvivalPayload, "survival"),
    )
    for supplied, expected_type, label in expected:
        if type(supplied) is not expected_type:
            raise DecisionProjectionContractError(
                f"unexpected enemy lifecycle {label} schema"
            )


def _capability_status_record(
    value: CapabilityFamilyStatus,
) -> dict[str, object]:
    return {
        "family": value.family.value,
        "status": value.status.value,
        "producer_namespace": value.producer_namespace,
        "evidence_kind": value.evidence_kind.value,
        "available_instance_count": value.available_instance_count,
        "blocker_codes": [
            {
                "producer_namespace": item.producer_namespace,
                "code": item.code,
            }
            for item in value.blocker_codes
        ],
    }


def _capability_snapshot_record(
    value: DecisionCapabilitySnapshot,
) -> dict[str, object]:
    return {
        **_capability_snapshot_payload(value),
        "semantic_hash": value.semantic_hash,
    }


def _capability_snapshot_payload(
    value: DecisionCapabilitySnapshot,
) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "side": value.side.value,
        "decision_window_index": value.decision_window_index,
        "frame_index": value.frame_index,
        "sim_time_ms": value.sim_time_ms,
        "operators": [
            {
                "operator_reference": item.operator_reference,
                "families": [
                    _capability_status_record(family)
                    for family in item.families
                ],
            }
            for item in value.operators
        ],
    }


def _action_catalog_record(value: ActionCatalogReference) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "side": value.side.value,
        "decision_window_index": value.decision_window_index,
        "frame_index": value.frame_index,
        "sim_time_ms": value.sim_time_ms,
        "action_count": value.action_count,
        "action_ids": list(value.action_ids),
        "action_entry_hashes": list(value.action_entry_hashes),
        "actor_references": list(value.actor_references),
        "catalog_hash": value.catalog_hash,
    }


def _validate_unique_payload_references(
    values: tuple[OwnOperatorLifecyclePayload, ...]
    | tuple[ObservedEnemyOperatorLifecyclePayload, ...],
    label: str,
) -> None:
    references = tuple(item.operator_reference for item in values)
    for reference in references:
        _nonnegative_int(reference, f"{label} operator_reference")
    if len(set(references)) != len(references):
        raise DecisionProjectionContractError(f"duplicate {label} reference")


def _sorted_references(values: Iterable[int], name: str) -> tuple[int, ...]:
    result = tuple(values)
    for value in result:
        _nonnegative_int(value, name)
    if len(set(result)) != len(result):
        raise DecisionProjectionContractError(f"{name} contains duplicates")
    return tuple(sorted(result))


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise TypeError(f"{name} must be a non-negative exact int")
    return value


def _exact_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be an exact bool")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise TypeError(f"{name} must be a non-empty normalized string")
    return value


def _token(value: object, name: str) -> str:
    text = _text(value, name)
    if _STABLE_TOKEN.fullmatch(text) is None:
        raise DecisionProjectionContractError(f"{name} is not a stable token")
    return text


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise DecisionProjectionContractError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return value


__all__ = [
    "DECISION_ACTION_CATALOG_SCHEMA_VERSION",
    "DECISION_CAPABILITY_SNAPSHOT_SCHEMA_VERSION",
    "DECISION_SEMANTIC_PROJECTION_SCHEMA_VERSION",
    "ActionCatalogReference",
    "AuthorityMetadata",
    "CapabilityAvailabilityStatus",
    "CapabilityBlockerCode",
    "CapabilityEvidenceKind",
    "CapabilityFamilyStatus",
    "DecisionCapabilityFamily",
    "DecisionCapabilitySnapshot",
    "DecisionMissionContext",
    "DecisionObjective",
    "DecisionProjectionContractError",
    "DecisionSide",
    "DecisionSnapshotIdentity",
    "DecisionTerminalRules",
    "OperatorCapabilitySummary",
    "SideDecisionContext",
    "SideDecisionSemanticProjection",
    "action_catalog_reference_hash",
    "build_decision_semantic_projection",
    "current_decision_authority_metadata",
]
