"""Deterministic numerical encoders for the Decision-v1 training boundary.

The encoders consume only immutable Decision projection and ActionCatalog
values.  They do not read WorldState, call Engine services, calculate
visibility, or decide legality.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Mapping, Sequence

from wargame_native_engine.io.serialization import canonical_hash, canonical_json
from wargame_native_engine.projection.decision_capability import (
    ActionCatalogSnapshot,
)
from wargame_native_engine.projection.decision_semantic import (
    CapabilityAvailabilityStatus,
    DecisionCapabilityFamily,
    DecisionProjectionContractError,
    DecisionSide,
    SideDecisionSemanticProjection,
)
from wargame_native_engine.projection.operator_lifecycle import (
    Applicability,
    BoardingLifecycleState,
    BoardingRole,
    IdentityStatus,
    LimitedLifecycleState,
    MovementLifecycleState,
    SpatialStatus,
    WeaponPosture,
)
from wargame_native_engine.projection.operator_perspective import (
    ObservedIdentityStatus,
    ObservedMotionState,
)


DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION = (
    "native-decision-observation-encoder/1.0"
)
DECISION_ACTION_ENCODER_SCHEMA_VERSION = "native-decision-action-encoder/1.0"

SNAPSHOT_FEATURES = (
    "side",
    "stage",
    "sim_time_ms",
    "terminated",
    "truncated",
    "objective_count",
    "own_operator_count",
    "observed_enemy_count",
    "action_count",
    "terminal_elimination",
    "objective_score_to_win_present",
    "objective_score_to_win",
    "max_ticks_present",
    "max_ticks",
)
OBJECTIVE_FEATURES = (
    "objective_reference",
    "x",
    "y",
    "radius",
    "value",
    "control_relation",
)
OWN_OPERATOR_FEATURES = (
    "operator_reference",
    "domain",
    "canonical_status",
    "alive",
    "destroyed",
    "health",
    "max_health",
    "health_ratio",
    "blood_present",
    "blood",
    "max_blood",
    "deployed",
    "spatially_present",
    "spatial_status",
    "current_hex_present",
    "current_hex",
    "on_board",
    "movement_state",
    "movement_mode",
    "speed",
    "has_active_segment",
    "segment_progress_present",
    "segment_progress_millionths",
    "segment_remaining_present",
    "segment_remaining_ms",
    "has_remaining_task",
    "remaining_path_length",
    "pause_pending",
    "paused",
    "stop_transition_remaining_ms",
    "transition_active",
    "transition_remaining_ms",
    "fatigue_applicability",
    "fatigue_level_present",
    "fatigue_level",
    "fatigue_recovery_remaining_present",
    "fatigue_recovery_remaining_ms",
    "suppression_active",
    "suppression_remaining_ms",
    "concealment_applicability",
    "concealment_posture",
    "boarding_applicability",
    "boarding_role",
    "boarding_state",
    "boarding_transition_remaining_present",
    "boarding_transition_remaining_ms",
    "boarding_movement_locked",
    "weapon_applicability",
    "weapon_posture",
    "weapon_cooldown_active",
    "weapon_cooldown_remaining_ms",
    "weapon_cooldown_clear",
    "guided_applicability",
    "guided_active",
    "guided_remaining_present",
    "guided_remaining_ms",
    "guided_ready",
    "indirect_applicability",
    "indirect_active",
    "indirect_remaining_present",
    "indirect_remaining_ms",
    "indirect_ready",
    "limited_applicability",
    "limited_enabled",
    "limited_state",
    "limited_remaining_present",
    "limited_remaining_ms",
)
OBSERVED_ENEMY_FEATURES = (
    "operator_reference",
    "domain",
    "canonical_status",
    "source_type_code_present",
    "source_type_code",
    "source_subtype_code_present",
    "source_subtype_code",
    "x",
    "y",
    "altitude",
    "alive",
    "destroyed",
    "motion",
)
CAPABILITY_FAMILY_ORDER = (
    DecisionCapabilityFamily.DEPLOY,
    DecisionCapabilityFamily.END_DEPLOYMENT,
    DecisionCapabilityFamily.MOVE,
    DecisionCapabilityFamily.STOP_MOVE,
    DecisionCapabilityFamily.WAIT,
    DecisionCapabilityFamily.DIRECT_FIRE,
    DecisionCapabilityFamily.GUIDED_FIRE,
    DecisionCapabilityFamily.INDIRECT_FIRE,
    DecisionCapabilityFamily.BOARD,
    DecisionCapabilityFamily.UNLOAD,
    DecisionCapabilityFamily.ENTER_CONCEALMENT,
    DecisionCapabilityFamily.WEAPON_LOCK,
    DecisionCapabilityFamily.WEAPON_UNFOLD,
    DecisionCapabilityFamily.CHANGE_STATE,
)
CAPABILITY_FEATURES = (
    "operator_reference",
    *tuple(
        f"{family.value}.{suffix}"
        for family in CAPABILITY_FAMILY_ORDER
        for suffix in ("status", "instance_count_present", "instance_count")
    ),
)

_SIDE = {DecisionSide.RED.value: 1.0, DecisionSide.BLUE.value: 2.0}
_STAGE = {"deployment": 1.0, "combat": 2.0, "terminal": 3.0}
_DOMAIN = {"UNKNOWN": 0.0, "PERSONNEL": 1.0, "VEHICLE": 2.0, "AIR": 3.0}
_IDENTITY = {
    IdentityStatus.UNKNOWN.value: 0.0,
    IdentityStatus.MISSING.value: 1.0,
    IdentityStatus.KNOWN.value: 2.0,
}
_OBSERVED_IDENTITY = {
    ObservedIdentityStatus.UNKNOWN.value: 0.0,
    ObservedIdentityStatus.KNOWN.value: 1.0,
}
_APPLICABILITY = {
    Applicability.UNKNOWN.value: 0.0,
    Applicability.MISSING.value: 1.0,
    Applicability.NOT_APPLICABLE.value: 2.0,
    Applicability.APPLICABLE.value: 3.0,
}
_SPATIAL = {value.value: float(index) for index, value in enumerate(SpatialStatus)}
_MOVEMENT = {
    value.value: float(index) for index, value in enumerate(MovementLifecycleState)
}
_MOVEMENT_MODE = {
    None: 0.0,
    "NORMAL": 1.0,
    "MARCHING": 2.0,
    "CHARGE_1": 3.0,
    "CHARGE_2": 4.0,
}
_CONCEALMENT = {None: 0.0, "EXPOSED": 1.0, "CONCEALED": 2.0}
_BOARDING_ROLE = {value.value: float(index) for index, value in enumerate(BoardingRole)}
_BOARDING_STATE = {
    value.value: float(index) for index, value in enumerate(BoardingLifecycleState)
}
_WEAPON_POSTURE = {value.value: float(index) for index, value in enumerate(WeaponPosture)}
_LIMITED_STATE = {
    value.value: float(index) for index, value in enumerate(LimitedLifecycleState)
}
_MOTION = {value.value: float(index) for index, value in enumerate(ObservedMotionState)}
_CAPABILITY_STATUS = {
    CapabilityAvailabilityStatus.UNKNOWN.value: 0.0,
    CapabilityAvailabilityStatus.MISSING.value: 1.0,
    CapabilityAvailabilityStatus.NOT_APPLICABLE.value: 2.0,
    CapabilityAvailabilityStatus.BLOCKED.value: 3.0,
    CapabilityAvailabilityStatus.AVAILABLE.value: 4.0,
}


class DecisionEncodingError(ValueError):
    """The safe semantic input cannot be represented by encoder v1."""


@dataclass(frozen=True, slots=True)
class DecisionEncoderConfig:
    max_own_operators: int = 64
    max_observed_enemies: int = 64
    max_objectives: int = 32
    max_actions: int = 256

    def __post_init__(self) -> None:
        for name in (
            "max_own_operators",
            "max_observed_enemies",
            "max_objectives",
            "max_actions",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive exact int")


@dataclass(frozen=True, slots=True)
class EncodedDecisionObservation:
    side: DecisionSide
    decision_window_index: int
    frame_index: int
    sim_time_ms: int
    decision_semantic_hash: str
    encoder_schema_version: str
    encoder_contract_hash: str
    snapshot_features: tuple[float, ...]
    objective_features: tuple[tuple[float, ...], ...]
    objective_mask: tuple[bool, ...]
    own_operator_features: tuple[tuple[float, ...], ...]
    own_operator_mask: tuple[bool, ...]
    observed_enemy_features: tuple[tuple[float, ...], ...]
    observed_enemy_mask: tuple[bool, ...]
    capability_features: tuple[tuple[float, ...], ...]
    capability_mask: tuple[bool, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.side, DecisionSide):
            raise TypeError("side must be DecisionSide")
        for name in ("decision_window_index", "frame_index", "sim_time_ms"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TypeError(f"{name} must be a non-negative exact int")
        if self.encoder_schema_version != DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION:
            raise DecisionEncodingError("unsupported observation encoder schema")
        _sha256(self.decision_semantic_hash, "decision_semantic_hash")
        _sha256(self.encoder_contract_hash, "encoder_contract_hash")
        if len(self.snapshot_features) != len(SNAPSHOT_FEATURES):
            raise DecisionEncodingError("snapshot feature width mismatch")
        for rows, mask, width, name in (
            (self.objective_features, self.objective_mask, len(OBJECTIVE_FEATURES), "objective"),
            (self.own_operator_features, self.own_operator_mask, len(OWN_OPERATOR_FEATURES), "own"),
            (self.observed_enemy_features, self.observed_enemy_mask, len(OBSERVED_ENEMY_FEATURES), "enemy"),
            (self.capability_features, self.capability_mask, len(CAPABILITY_FEATURES), "capability"),
        ):
            if len(rows) != len(mask) or any(type(value) is not bool for value in mask):
                raise DecisionEncodingError(f"{name} feature/mask mismatch")
            if any(len(row) != width for row in rows):
                raise DecisionEncodingError(f"{name} feature width mismatch")
        if any(not math.isfinite(value) for value in self.tensor):
            raise DecisionEncodingError("observation contains NaN or Inf")

    @property
    def tensor(self) -> tuple[float, ...]:
        values = list(self.snapshot_features)
        for rows in (
            self.objective_features,
            self.own_operator_features,
            self.observed_enemy_features,
            self.capability_features,
        ):
            for row in rows:
                values.extend(row)
        values.extend(float(value) for value in self.objective_mask)
        values.extend(float(value) for value in self.own_operator_mask)
        values.extend(float(value) for value in self.observed_enemy_mask)
        values.extend(float(value) for value in self.capability_mask)
        return tuple(values)

    def policy_record(self) -> dict[str, object]:
        """Return only numerical policy features; hashes stay out-of-band."""

        return {
            "snapshot_features": list(self.snapshot_features),
            "objective_features": [list(value) for value in self.objective_features],
            "objective_mask": list(self.objective_mask),
            "own_operator_features": [
                list(value) for value in self.own_operator_features
            ],
            "own_operator_mask": list(self.own_operator_mask),
            "observed_enemy_features": [
                list(value) for value in self.observed_enemy_features
            ],
            "observed_enemy_mask": list(self.observed_enemy_mask),
            "capability_features": [list(value) for value in self.capability_features],
            "capability_mask": list(self.capability_mask),
        }


@dataclass(frozen=True, slots=True)
class EncodedDecisionActionSpace:
    side: DecisionSide
    decision_window_index: int
    frame_index: int
    sim_time_ms: int
    catalog_hash: str
    encoder_schema_version: str
    encoder_contract_hash: str
    action_ids: tuple[str, ...]
    canonical_actions_json: tuple[str, ...]
    mask: tuple[bool, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.side, DecisionSide):
            raise TypeError("side must be DecisionSide")
        for name in ("decision_window_index", "frame_index", "sim_time_ms"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TypeError(f"{name} must be a non-negative exact int")
        if self.encoder_schema_version != DECISION_ACTION_ENCODER_SCHEMA_VERSION:
            raise DecisionEncodingError("unsupported action encoder schema")
        _sha256(self.catalog_hash, "catalog_hash")
        _sha256(self.encoder_contract_hash, "encoder_contract_hash")
        if len(self.action_ids) != len(self.canonical_actions_json):
            raise DecisionEncodingError("action identity/record count mismatch")
        if len(self.mask) < len(self.action_ids):
            raise DecisionEncodingError("action mask is shorter than catalog")
        if self.mask != (
            (True,) * len(self.action_ids)
            + (False,) * (len(self.mask) - len(self.action_ids))
        ):
            raise DecisionEncodingError("action mask does not match catalog entries")
        for value in self.canonical_actions_json:
            parsed = json.loads(value)
            if canonical_json(parsed) != value:
                raise DecisionEncodingError("action record JSON is not canonical")

    @property
    def actions(self) -> tuple[Mapping[str, object], ...]:
        return tuple(json.loads(value) for value in self.canonical_actions_json)

    def to_record(self) -> dict[str, object]:
        return {
            "encoder_schema_version": self.encoder_schema_version,
            "encoder_contract_hash": self.encoder_contract_hash,
            "side": self.side.value,
            "decision_window_index": self.decision_window_index,
            "frame_index": self.frame_index,
            "sim_time_ms": self.sim_time_ms,
            "catalog_hash": self.catalog_hash,
            "action_ids": list(self.action_ids),
            "actions": list(self.actions),
            "mask": list(self.mask),
        }


@dataclass(frozen=True, slots=True)
class DecisionObservationSpace:
    config: DecisionEncoderConfig

    @property
    def flat_shape(self) -> tuple[int]:
        width = len(SNAPSHOT_FEATURES)
        width += self.config.max_objectives * len(OBJECTIVE_FEATURES)
        width += self.config.max_own_operators * len(OWN_OPERATOR_FEATURES)
        width += self.config.max_observed_enemies * len(OBSERVED_ENEMY_FEATURES)
        width += self.config.max_own_operators * len(CAPABILITY_FEATURES)
        width += (
            self.config.max_objectives
            + self.config.max_own_operators * 2
            + self.config.max_observed_enemies
        )
        return (width,)

    def contains(self, value: EncodedDecisionObservation | Mapping[str, object]) -> bool:
        try:
            record = value.policy_record() if isinstance(
                value, EncodedDecisionObservation
            ) else value
            return _record_shape_is_valid(record, self.config)
        except (TypeError, ValueError):
            return False


@dataclass(frozen=True, slots=True)
class DecisionDiscreteActionSpace:
    n: int

    def __post_init__(self) -> None:
        if type(self.n) is not int or self.n <= 0:
            raise ValueError("action capacity must be a positive exact int")

    def contains(self, value: object) -> bool:
        return type(value) is int and 0 <= value < self.n


class DecisionObservationEncoder:
    """Encode one immutable Decision projection into fixed numerical groups."""

    def __init__(self, config: DecisionEncoderConfig | None = None) -> None:
        self.config = DecisionEncoderConfig() if config is None else config
        if not isinstance(self.config, DecisionEncoderConfig):
            raise TypeError("config must be DecisionEncoderConfig")
        self.contract_hash = canonical_hash(_observation_contract(self.config))
        self.observation_space = DecisionObservationSpace(self.config)

    def encode(
        self, decision: SideDecisionSemanticProjection
    ) -> EncodedDecisionObservation:
        if type(decision) is not SideDecisionSemanticProjection:
            raise TypeError("decision must be SideDecisionSemanticProjection")
        if len(decision.own_operators) > self.config.max_own_operators:
            raise DecisionEncodingError("own operator capacity exceeded")
        if len(decision.observed_enemies) > self.config.max_observed_enemies:
            raise DecisionEncodingError("observed enemy capacity exceeded")
        if len(decision.side_context.objectives) > self.config.max_objectives:
            raise DecisionEncodingError("objective capacity exceeded")

        objectives = tuple(
            (
                float(value.objective_id),
                float(value.x),
                float(value.y),
                float(value.radius),
                float(value.value),
                _control_relation(value.controlling_side, decision.snapshot.side),
            )
            for value in decision.side_context.objectives
        )
        own = tuple(_encode_own(value) for value in decision.own_operators)
        enemies = tuple(_encode_enemy(value) for value in decision.observed_enemies)
        capability_by_operator = {
            value.operator_reference: value
            for value in decision.capabilities.operators
        }
        capabilities = tuple(
            _encode_capability(capability_by_operator[value.operator_reference])
            for value in decision.own_operators
        )
        terminal = decision.side_context.mission.terminal_rules
        objective_score = _optional(terminal.objective_score_to_win)
        max_ticks = _optional(terminal.max_ticks)
        snapshot = (
            _SIDE[decision.snapshot.side.value],
            _STAGE[decision.snapshot.stage.value],
            float(decision.snapshot.sim_time_ms),
            float(decision.snapshot.terminated),
            float(decision.snapshot.truncated),
            float(len(objectives)),
            float(len(own)),
            float(len(enemies)),
            float(decision.action_space.action_count),
            float(terminal.elimination),
            *objective_score,
            *max_ticks,
        )
        result = EncodedDecisionObservation(
            side=decision.snapshot.side,
            decision_window_index=decision.snapshot.decision_window_index,
            frame_index=decision.snapshot.frame_index,
            sim_time_ms=decision.snapshot.sim_time_ms,
            decision_semantic_hash=decision.semantic_hash,
            encoder_schema_version=DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION,
            encoder_contract_hash=self.contract_hash,
            snapshot_features=snapshot,
            objective_features=_pad_rows(
                objectives, self.config.max_objectives, len(OBJECTIVE_FEATURES)
            ),
            objective_mask=_mask(len(objectives), self.config.max_objectives),
            own_operator_features=_pad_rows(
                own, self.config.max_own_operators, len(OWN_OPERATOR_FEATURES)
            ),
            own_operator_mask=_mask(len(own), self.config.max_own_operators),
            observed_enemy_features=_pad_rows(
                enemies,
                self.config.max_observed_enemies,
                len(OBSERVED_ENEMY_FEATURES),
            ),
            observed_enemy_mask=_mask(
                len(enemies), self.config.max_observed_enemies
            ),
            capability_features=_pad_rows(
                capabilities,
                self.config.max_own_operators,
                len(CAPABILITY_FEATURES),
            ),
            capability_mask=_mask(
                len(capabilities), self.config.max_own_operators
            ),
        )
        if not self.observation_space.contains(result):
            raise DecisionEncodingError("encoded observation violates its space")
        return result


class DecisionActionEncoder:
    """Encode only the supplied frozen catalog; never infer legal actions."""

    def __init__(self, max_actions: int = 256) -> None:
        if type(max_actions) is not int or max_actions <= 0:
            raise ValueError("max_actions must be a positive exact int")
        self.max_actions = max_actions
        self.contract_hash = canonical_hash({
            "schema_version": DECISION_ACTION_ENCODER_SCHEMA_VERSION,
            "capacity": max_actions,
            "index_semantics": "catalog-order-window-local",
            "padding": False,
            "resolution": "index-to-current-catalog-action-id",
        })
        self.action_space = DecisionDiscreteActionSpace(max_actions)

    def encode(self, catalog: ActionCatalogSnapshot) -> EncodedDecisionActionSpace:
        if type(catalog) is not ActionCatalogSnapshot:
            raise TypeError("catalog must be ActionCatalogSnapshot")
        if catalog.action_count > self.max_actions:
            raise DecisionEncodingError(
                f"action catalog exceeds max_actions={self.max_actions}"
            )
        actions = tuple(
            canonical_json(_action_record(value, catalog.side))
            for value in catalog.entries
        )
        return EncodedDecisionActionSpace(
            side=catalog.side,
            decision_window_index=catalog.decision_window_index,
            frame_index=catalog.frame_index,
            sim_time_ms=catalog.sim_time_ms,
            catalog_hash=catalog.catalog_hash,
            encoder_schema_version=DECISION_ACTION_ENCODER_SCHEMA_VERSION,
            encoder_contract_hash=self.contract_hash,
            action_ids=catalog.action_ids,
            canonical_actions_json=actions,
            mask=(True,) * catalog.action_count
            + (False,) * (self.max_actions - catalog.action_count),
        )

    def resolve(
        self,
        encoded: EncodedDecisionActionSpace,
        catalog: ActionCatalogSnapshot,
        action_index: int | None,
    ) -> str | None:
        self._validate_binding(encoded, catalog)
        if action_index is None:
            return None
        if type(action_index) is not int:
            raise TypeError("action_index must be an exact int or None")
        if not self.action_space.contains(action_index):
            raise ValueError(f"action_index {action_index!r} is outside capacity")
        if not encoded.mask[action_index]:
            raise ValueError(f"action_index {action_index!r} is not currently valid")
        return catalog.entries[action_index].action_id

    def resolve_batch(
        self,
        encoded: EncodedDecisionActionSpace,
        catalog: ActionCatalogSnapshot,
        action_indices: Sequence[int],
    ) -> tuple[str, ...]:
        if isinstance(action_indices, (str, bytes)) or not isinstance(
            action_indices, Sequence
        ):
            raise TypeError("action_indices must be a sequence")
        result: list[str] = []
        seen: set[int] = set()
        for index in action_indices:
            if type(index) is not int:
                raise TypeError("action_indices must contain exact ints")
            if index in seen:
                raise ValueError(f"action_index {index!r} occurs more than once")
            seen.add(index)
            value = self.resolve(encoded, catalog, index)
            if value is None:  # pragma: no cover - exact int cannot resolve None
                raise RuntimeError("action index unexpectedly resolved to NoOp")
            result.append(value)
        return tuple(result)

    def _validate_binding(
        self,
        encoded: EncodedDecisionActionSpace,
        catalog: ActionCatalogSnapshot,
    ) -> None:
        if type(encoded) is not EncodedDecisionActionSpace:
            raise TypeError("encoded must be EncodedDecisionActionSpace")
        if type(catalog) is not ActionCatalogSnapshot:
            raise TypeError("catalog must be ActionCatalogSnapshot")
        if (
            encoded.encoder_schema_version != DECISION_ACTION_ENCODER_SCHEMA_VERSION
            or encoded.encoder_contract_hash != self.contract_hash
            or encoded.side is not catalog.side
            or encoded.decision_window_index != catalog.decision_window_index
            or encoded.frame_index != catalog.frame_index
            or encoded.sim_time_ms != catalog.sim_time_ms
            or encoded.catalog_hash != catalog.catalog_hash
            or encoded.action_ids != catalog.action_ids
        ):
            raise DecisionProjectionContractError(
                "encoded action space/catalog snapshot mismatch"
            )


def _encode_own(value) -> tuple[float, ...]:
    survival = value.survival
    deployment = value.deployment
    movement = value.movement
    fatigue = value.fatigue
    boarding = value.boarding
    weapon = value.weapon_lifecycle
    guided = value.guided_recovery
    indirect = value.indirect_planning
    limited = value.limited_lifecycle
    blood_present = survival.blood is not None and survival.max_blood is not None
    health_ratio = (
        0.0 if survival.max_health <= 0 else survival.health / survival.max_health
    )
    return tuple(float(item) for item in (
        value.operator_reference,
        _enum(_DOMAIN, value.identity.domain),
        _enum(_IDENTITY, value.identity.canonical_status.value),
        survival.alive,
        survival.destroyed,
        survival.health,
        survival.max_health,
        health_ratio,
        blood_present,
        0 if survival.blood is None else survival.blood,
        0 if survival.max_blood is None else survival.max_blood,
        deployment.deployed,
        deployment.spatially_present,
        _enum(_SPATIAL, deployment.spatial_status.value),
        deployment.current_hex is not None,
        0 if deployment.current_hex is None else deployment.current_hex,
        deployment.on_board,
        _enum(_MOVEMENT, movement.state.value),
        _enum(_MOVEMENT_MODE, movement.mode),
        movement.speed,
        movement.has_active_segment,
        movement.segment_progress_millionths is not None,
        0 if movement.segment_progress_millionths is None else movement.segment_progress_millionths,
        movement.segment_remaining_ms is not None,
        0 if movement.segment_remaining_ms is None else movement.segment_remaining_ms,
        movement.has_remaining_task,
        movement.remaining_path_length,
        movement.pause_pending,
        movement.paused,
        movement.stop_transition_remaining_ms,
        value.operator_transition.active,
        value.operator_transition.remaining_ms,
        _enum(_APPLICABILITY, fatigue.applicability.value),
        fatigue.level is not None,
        0 if fatigue.level is None else fatigue.level,
        fatigue.recovery_remaining_ms is not None,
        0 if fatigue.recovery_remaining_ms is None else fatigue.recovery_remaining_ms,
        value.suppression.active,
        value.suppression.remaining_ms,
        _enum(_APPLICABILITY, value.concealment.applicability.value),
        _enum(_CONCEALMENT, value.concealment.posture),
        _enum(_APPLICABILITY, boarding.applicability.value),
        _enum(_BOARDING_ROLE, boarding.role.value),
        _enum(_BOARDING_STATE, boarding.state.value),
        boarding.transition_remaining_ms is not None,
        0 if boarding.transition_remaining_ms is None else boarding.transition_remaining_ms,
        boarding.movement_locked,
        _enum(_APPLICABILITY, weapon.applicability.value),
        _enum(_WEAPON_POSTURE, weapon.posture.value),
        weapon.cooldown_active,
        weapon.cooldown_remaining_ms,
        weapon.cooldown_clear,
        _enum(_APPLICABILITY, guided.applicability.value),
        guided.active,
        guided.remaining_ms is not None,
        0 if guided.remaining_ms is None else guided.remaining_ms,
        _tri_bool(guided.ready),
        _enum(_APPLICABILITY, indirect.applicability.value),
        indirect.active,
        indirect.remaining_ms is not None,
        0 if indirect.remaining_ms is None else indirect.remaining_ms,
        _tri_bool(indirect.ready),
        _enum(_APPLICABILITY, limited.applicability.value),
        limited.enabled,
        _enum(_LIMITED_STATE, limited.state.value),
        limited.remaining_ms is not None,
        0 if limited.remaining_ms is None else limited.remaining_ms,
    ))


def _encode_enemy(value) -> tuple[float, ...]:
    identity = value.identity
    observation = value.observation
    return tuple(float(item) for item in (
        value.operator_reference,
        _enum(_DOMAIN, identity.domain),
        _enum(_OBSERVED_IDENTITY, identity.canonical_status.value),
        identity.source_type_code is not None,
        0 if identity.source_type_code is None else identity.source_type_code,
        identity.source_subtype_code is not None,
        0 if identity.source_subtype_code is None else identity.source_subtype_code,
        observation.position[0],
        observation.position[1],
        observation.altitude,
        value.survival.alive,
        value.survival.destroyed,
        _enum(_MOTION, value.motion.value),
    ))


def _encode_capability(value) -> tuple[float, ...]:
    by_family = {item.family: item for item in value.families}
    if set(by_family) != set(CAPABILITY_FAMILY_ORDER):
        raise DecisionEncodingError(
            "capability family set requires encoder version update"
        )
    result = [float(value.operator_reference)]
    for family in CAPABILITY_FAMILY_ORDER:
        status = by_family.get(family)
        if status is None:
            raise DecisionEncodingError(
                f"capability family {family.value} requires encoder version update"
            )
        count = status.available_instance_count
        result.extend((
            _CAPABILITY_STATUS[status.status.value],
            float(count is not None),
            0.0 if count is None else float(count),
        ))
    return tuple(result)


def _action_record(value, side: DecisionSide) -> dict[str, object]:
    return {
        "action_id": value.action_id,
        "priority": value.priority,
        "source": value.producer_namespace,
        "action": {
            "side": side.value,
            "actor_id": value.actor_reference,
            "action_type": value.action_type,
            "target_id": value.target_reference,
            "target_position": (
                None
                if value.target_position is None
                else list(value.target_position)
            ),
            "parameters": value.parameters(),
        },
    }


def _optional(value: int | None) -> tuple[float, float]:
    return (float(value is not None), 0.0 if value is None else float(value))


def _tri_bool(value: bool | None) -> float:
    return 0.0 if value is None else 2.0 if value else 1.0


def _enum(mapping: Mapping[object, float], value: object) -> float:
    # Every table includes an explicit UNKNOWN/None code.  Unknown future
    # values fail closed instead of using Python hash or Enum ordinal.
    if value not in mapping:
        raise DecisionEncodingError(f"unmapped enum value: {value!r}")
    return mapping[value]


def _control_relation(value: str | None, side: DecisionSide) -> float:
    if value is None:
        return 0.0
    if value == side.value:
        return 1.0
    if value in {DecisionSide.RED.value, DecisionSide.BLUE.value}:
        return 2.0
    raise DecisionEncodingError("unmapped objective controlling side")


def _pad_rows(
    rows: tuple[tuple[float, ...], ...], capacity: int, width: int
) -> tuple[tuple[float, ...], ...]:
    if any(len(value) != width for value in rows):
        raise DecisionEncodingError("feature row width mismatch")
    return rows + ((0.0,) * width,) * (capacity - len(rows))


def _mask(count: int, capacity: int) -> tuple[bool, ...]:
    return (True,) * count + (False,) * (capacity - count)


def _record_shape_is_valid(
    value: Mapping[str, object], config: DecisionEncoderConfig
) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "snapshot_features",
        "objective_features",
        "objective_mask",
        "own_operator_features",
        "own_operator_mask",
        "observed_enemy_features",
        "observed_enemy_mask",
        "capability_features",
        "capability_mask",
    }:
        return False
    groups = (
        (value["snapshot_features"], 1, len(SNAPSHOT_FEATURES), False),
        (value["objective_features"], config.max_objectives, len(OBJECTIVE_FEATURES), True),
        (value["own_operator_features"], config.max_own_operators, len(OWN_OPERATOR_FEATURES), True),
        (value["observed_enemy_features"], config.max_observed_enemies, len(OBSERVED_ENEMY_FEATURES), True),
        (value["capability_features"], config.max_own_operators, len(CAPABILITY_FEATURES), True),
    )
    for group, count, width, nested in groups:
        if isinstance(group, (str, bytes)) or not isinstance(group, Sequence):
            return False
        rows = group if nested else (group,)
        if len(rows) != count:
            return False
        for row in rows:
            if isinstance(row, (str, bytes)) or not isinstance(row, Sequence):
                return False
            if len(row) != width or any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                for item in row
            ):
                return False
    for name, count in (
        ("objective_mask", config.max_objectives),
        ("own_operator_mask", config.max_own_operators),
        ("observed_enemy_mask", config.max_observed_enemies),
        ("capability_mask", config.max_own_operators),
    ):
        mask = value[name]
        if not isinstance(mask, Sequence) or len(mask) != count:
            return False
        if any(type(item) is not bool for item in mask):
            return False
    return True


def _observation_contract(config: DecisionEncoderConfig) -> dict[str, object]:
    return {
        "schema_version": DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION,
        "dtype": "float64",
        "normalization": "raw-ms-and-native-units;health-ratio-derived",
        "padding_value": 0.0,
        "config": {
            "max_own_operators": config.max_own_operators,
            "max_observed_enemies": config.max_observed_enemies,
            "max_objectives": config.max_objectives,
        },
        "features": {
            "snapshot": SNAPSHOT_FEATURES,
            "objectives": OBJECTIVE_FEATURES,
            "own_operators": OWN_OPERATOR_FEATURES,
            "observed_enemies": OBSERVED_ENEMY_FEATURES,
            "capabilities": CAPABILITY_FEATURES,
        },
        "enum_mappings": {
            "side": _SIDE,
            "stage": _STAGE,
            "domain": _DOMAIN,
            "identity": _IDENTITY,
            "observed_identity": _OBSERVED_IDENTITY,
            "applicability": _APPLICABILITY,
            "spatial": _SPATIAL,
            "movement": _MOVEMENT,
            "movement_mode": {str(key): value for key, value in _MOVEMENT_MODE.items()},
            "concealment": {str(key): value for key, value in _CONCEALMENT.items()},
            "boarding_role": _BOARDING_ROLE,
            "boarding_state": _BOARDING_STATE,
            "weapon_posture": _WEAPON_POSTURE,
            "limited_state": _LIMITED_STATE,
            "motion": _MOTION,
            "capability_status": _CAPABILITY_STATUS,
            "capability_family_order": tuple(
                value.value for value in CAPABILITY_FAMILY_ORDER
            ),
        },
        "excluded": (
            "authority_metadata",
            "semantic_hashes",
            "reward",
            "winner",
            "outcome",
            "EightFactor",
            "GREEN",
            "blocker_text",
        ),
    }


def _sha256(value: object, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise DecisionEncodingError(f"{name} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise DecisionEncodingError(
            f"{name} must be a SHA-256 hex digest"
        ) from exc


__all__ = [
    "CAPABILITY_FAMILY_ORDER",
    "CAPABILITY_FEATURES",
    "DECISION_ACTION_ENCODER_SCHEMA_VERSION",
    "DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION",
    "OBJECTIVE_FEATURES",
    "OBSERVED_ENEMY_FEATURES",
    "OWN_OPERATOR_FEATURES",
    "SNAPSHOT_FEATURES",
    "DecisionActionEncoder",
    "DecisionDiscreteActionSpace",
    "DecisionEncoderConfig",
    "DecisionEncodingError",
    "DecisionObservationEncoder",
    "DecisionObservationSpace",
    "EncodedDecisionActionSpace",
    "EncodedDecisionObservation",
]
