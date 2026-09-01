from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Mapping

from wargame_native_engine.compat.source_frame.mapping import (
    SourceMappings,
    source_identity_codes,
)
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import ObjectiveState, Stage
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.rules.perspective import (
    PerspectivePolicy,
    load_perspective_policy,
)


@dataclass(frozen=True, slots=True)
class OperatorObservation:
    """Complete detached observation of one friendly Operator."""

    operator_id: int
    side: str
    operator_type: str
    subtype: str | None
    position: tuple[float, float]
    altitude: float
    heading: float
    health: float
    max_health: float
    state: str
    alive: bool
    deployed: bool
    observed_enemy_ids: tuple[int, ...] = ()
    blood: int | None = None
    max_blood: int | None = None
    speed: float = 0.0
    movement_mode: str = "NORMAL"
    stopped: bool = True
    has_active_segment: bool = False
    has_remaining_movement_task: bool = False
    remaining_path_length: int = 0
    movement_task_pause_reason: str | None = None
    movement_blocked_reason: str | None = None
    move_to_stop_remaining_ms: int = 0
    transition_kind: str | None = None
    transition_remaining_ms: int = 0
    fatigue_level: int = 0
    fatigue_recovery_elapsed_ms: int = 0
    suppression_remaining_ms: int = 0
    weapon_cooldown_remaining_ms: int = 0
    guided_fire_recovery_remaining_ms: int = 0
    indirect_fire_planned_cooldown_remaining_ms: int = 0
    boarding_transition_type: str | None = None
    boarding_transition_remaining_ms: int | None = None
    lifecycle_profile_id: str | None = None
    lifecycle_remaining_ms: int | None = None
    concealment_posture: str | None = None

    @classmethod
    def from_state(
        cls,
        value: OperatorState,
        *,
        boarding_transition_type: str | None = None,
        boarding_transition_remaining_ms: int | None = None,
    ) -> "OperatorObservation":
        return cls(
            value.operator_id,
            value.side,
            value.operator_type,
            value.subtype,
            value.position,
            value.altitude,
            value.heading,
            value.health,
            value.max_health,
            value.state,
            value.alive,
            value.deployed,
            observed_enemy_ids=tuple(sorted({
                int(item)
                for item in value.raw_attributes.get("see_enemy_bop_ids", ())
            })),
            blood=value.blood,
            max_blood=value.max_blood,
            speed=value.speed,
            movement_mode=value.movement_mode,
            stopped=value.stopped,
            has_active_segment=value.segment_to_hex is not None,
            has_remaining_movement_task=bool(value.move_path),
            remaining_path_length=len(value.move_path),
            movement_task_pause_reason=(
                None
                if value.movement_task_pause_reason is None
                else value.movement_task_pause_reason.value
            ),
            movement_blocked_reason=value.movement_blocked_reason,
            move_to_stop_remaining_ms=value.move_to_stop_remaining_ms,
            transition_kind=value.transition_kind,
            transition_remaining_ms=value.transition_remaining_ms,
            fatigue_level=value.fatigue_level,
            fatigue_recovery_elapsed_ms=value.fatigue_recovery_elapsed_ms,
            suppression_remaining_ms=value.suppression_remaining_ms,
            weapon_cooldown_remaining_ms=value.weapon_cooldown_remaining_ms,
            guided_fire_recovery_remaining_ms=(
                value.guided_fire_recovery_remaining_ms
            ),
            indirect_fire_planned_cooldown_remaining_ms=(
                value.indirect_fire_planned_cooldown_remaining_ms
            ),
            boarding_transition_type=boarding_transition_type,
            boarding_transition_remaining_ms=boarding_transition_remaining_ms,
            lifecycle_profile_id=value.lifecycle_profile_id,
            lifecycle_remaining_ms=value.lifecycle_remaining_ms,
            concealment_posture=(
                None
                if value.concealment_posture is None
                else value.concealment_posture.value
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator_id": self.operator_id,
            "side": self.side,
            "operator_type": self.operator_type,
            "subtype": self.subtype,
            "position": list(self.position),
            "altitude": self.altitude,
            "heading": self.heading,
            "health": self.health,
            "max_health": self.max_health,
            "state": self.state,
            "alive": self.alive,
            "deployed": self.deployed,
            "observed_enemy_ids": list(self.observed_enemy_ids),
            "blood": self.blood,
            "max_blood": self.max_blood,
            "speed": self.speed,
            "movement_mode": self.movement_mode,
            "stopped": self.stopped,
            "has_active_segment": self.has_active_segment,
            "has_remaining_movement_task": self.has_remaining_movement_task,
            "remaining_path_length": self.remaining_path_length,
            "movement_task_pause_reason": self.movement_task_pause_reason,
            "movement_blocked_reason": self.movement_blocked_reason,
            "move_to_stop_remaining_ms": self.move_to_stop_remaining_ms,
            "transition_kind": self.transition_kind,
            "transition_remaining_ms": self.transition_remaining_ms,
            "fatigue_level": self.fatigue_level,
            "fatigue_recovery_elapsed_ms": self.fatigue_recovery_elapsed_ms,
            "suppression_remaining_ms": self.suppression_remaining_ms,
            "weapon_cooldown_remaining_ms": (
                self.weapon_cooldown_remaining_ms
            ),
            "guided_fire_recovery_remaining_ms": (
                self.guided_fire_recovery_remaining_ms
            ),
            "indirect_fire_planned_cooldown_remaining_ms": (
                self.indirect_fire_planned_cooldown_remaining_ms
            ),
            "boarding_transition_type": self.boarding_transition_type,
            "boarding_transition_remaining_ms": (
                self.boarding_transition_remaining_ms
            ),
            "lifecycle_profile_id": self.lifecycle_profile_id,
            "lifecycle_remaining_ms": self.lifecycle_remaining_ms,
            "concealment_posture": self.concealment_posture,
        }


@dataclass(frozen=True, slots=True)
class ObservedEnemyOperatorObservation:
    """Positive allow-list contact evidence for one observed enemy."""

    operator_id: int
    side: str
    operator_type: str
    subtype: str | None
    source_type_code: int | None
    source_subtype_code: int | None
    canonical_identity: str | None
    canonical_status: str
    domain: str
    position: tuple[float, float]
    altitude: float
    alive: bool
    destroyed: bool
    motion_state: str

    def to_dict(self) -> dict[str, Any]:
        value = {
            "operator_id": self.operator_id,
            "side": self.side,
            "operator_type": self.operator_type,
            "subtype": self.subtype,
            "source_type_code": self.source_type_code,
            "source_subtype_code": self.source_subtype_code,
            "canonical_identity": self.canonical_identity,
            "canonical_status": self.canonical_status,
            "domain": self.domain,
            "position": list(self.position),
            "altitude": self.altitude,
            "alive": self.alive,
            "destroyed": self.destroyed,
            "motion_state": self.motion_state,
        }
        policy = load_perspective_policy()
        if set(value) != set(policy.enemy_observation_allowlist):
            raise RuntimeError("enemy observation allow-list mismatch")
        return value


def side_safe_enemy_observation(
    value: OperatorState | OperatorObservation | ObservedEnemyOperatorObservation,
    *,
    policy: PerspectivePolicy | None = None,
) -> ObservedEnemyOperatorObservation:
    """Derive only policy-authorized contact fields from supplied evidence."""

    if isinstance(value, ObservedEnemyOperatorObservation):
        return value
    contract = load_perspective_policy() if policy is None else policy
    subtype = value.subtype
    source_codes = source_identity_codes(subtype)
    canonical_identity: str | None = None
    if source_codes is not None:
        mappings = _source_mappings()
        mapped = mappings.canonical_identity(*source_codes)
        mapped_domain = mappings.domain(source_codes[0])
        if (
            mapped.status == "DIRECT"
            and mapped_domain.status == "DIRECT"
            and str(mapped_domain.native_effective_value).upper()
            == str(value.operator_type).upper()
        ):
            canonical_identity = str(mapped.native_effective_value).upper()
    return ObservedEnemyOperatorObservation(
        operator_id=value.operator_id,
        side=value.side,
        operator_type=str(value.operator_type).upper(),
        subtype=subtype,
        source_type_code=None if source_codes is None else source_codes[0],
        source_subtype_code=None if source_codes is None else source_codes[1],
        canonical_identity=canonical_identity,
        canonical_status="KNOWN" if canonical_identity is not None else "UNKNOWN",
        domain=str(value.operator_type).upper(),
        position=(float(value.position[0]), float(value.position[1])),
        altitude=float(value.altitude),
        alive=bool(value.alive),
        destroyed=not bool(value.alive),
        motion_state=contract.enemy_motion(value.state),
    )


@lru_cache(maxsize=1)
def _source_mappings() -> SourceMappings:
    return SourceMappings()


@dataclass(frozen=True, slots=True)
class SideObservation:
    side: str
    tick: int
    stage: Stage
    friendly_operators: tuple[OperatorObservation, ...]
    visible_enemies: tuple[ObservedEnemyOperatorObservation, ...]
    objectives: tuple[Mapping[str, Any], ...]
    legal_action_ids: tuple[str, ...]
    mission: Mapping[str, Any] = field(default_factory=dict)

    @property
    def observation_hash(self) -> str:
        return str(self.to_dict()["observation_hash"])

    def to_dict(self) -> dict[str, Any]:
        enemies = [
            side_safe_enemy_observation(item).to_dict()
            for item in self.visible_enemies
        ]
        return {"side": self.side, "tick": self.tick,
                "stage": self.stage.value,
                "friendly_operators": [item.to_dict()
                                       for item in self.friendly_operators],
                "visible_enemies": enemies,
                "objectives": json_clone(self.objectives),
                "legal_action_ids": list(self.legal_action_ids),
                "mission": json_clone(self.mission),
                "observation_hash": canonical_hash({
                    "side": self.side, "tick": self.tick,
                    "stage": self.stage.value,
                    "friendly": [item.to_dict() for item in self.friendly_operators],
                    "enemies": enemies,
                    "objectives": self.objectives,
                    "legal_action_ids": self.legal_action_ids,
                    "mission": self.mission,
                })}


def objective_view(value: ObjectiveState) -> Mapping[str, Any]:
    return json_clone(value.to_dict())
