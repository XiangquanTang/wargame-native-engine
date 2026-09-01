"""Immutable runtime contracts for planned indirect-fire missions.

Phase C-2 advances mission clocks only.  It has no scatter, impact, damage,
effect, event, replay, Engine, or WorldState dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from wargame_native_engine.rules.indirect_fire import (
    IndirectFireCancellationReason,
    IndirectFireMissionState,
)


FIRE_MISSION_SCHEMA_VERSION = "native-indirect-fire-mission/1.1"
LEGACY_FIRE_MISSION_SCHEMA_VERSION = "native-indirect-fire-mission/1.0"
FireMissionPhase = IndirectFireMissionState


@dataclass(frozen=True, slots=True)
class IndirectFireAffectedLedger:
    """Authoritative one-hit evidence for one mission/operator pair."""

    mission_id: str
    operator_id: int
    resolved: bool = True

    def __post_init__(self) -> None:
        _require_nonempty(self.mission_id, "mission_id")
        _require_nonnegative_exact_int(self.operator_id, "operator_id")
        if type(self.resolved) is not bool or not self.resolved:
            raise ValueError("resolved must be the exact bool True")

    def to_dict(self) -> dict[str, object]:
        return {"mission_id": self.mission_id,
                "operator_id": self.operator_id, "resolved": self.resolved}


@dataclass(frozen=True, slots=True)
class IndirectFireActiveAreaView:
    """Detached, side-filtered Viewer projection of one ACTIVE area."""

    mission_id: str
    shooter_id: int
    impact_hex: int
    active_remaining_seconds: int
    affected_operator_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        _require_nonempty(self.mission_id, "mission_id")
        for name in ("shooter_id", "impact_hex", "active_remaining_seconds"):
            _require_nonnegative_exact_int(getattr(self, name), name)
        if self.active_remaining_seconds <= 0:
            raise ValueError("active_remaining_seconds must be positive")
        if self.affected_operator_ids != tuple(sorted(set(
            self.affected_operator_ids
        ))):
            raise ValueError("affected_operator_ids must be sorted unique ids")

    def to_record(self) -> dict[str, object]:
        return {
            "mission_id": self.mission_id,
            "shooter_id": self.shooter_id,
            "impact_hex": self.impact_hex,
            "active_remaining_seconds": self.active_remaining_seconds,
            "affected_operator_ids": list(self.affected_operator_ids),
        }


@dataclass(frozen=True, slots=True)
class FireMissionScatterEvidence:
    """Persisted scatter result needed by later ACTIVE-area entrants."""

    direction_roll: int
    direction_index: int
    distance_dice: tuple[int, int]
    distance_roll: int
    distance_band: str
    table_id: str
    table_result: str
    scatter_distance: int

    def __post_init__(self) -> None:
        for name in ("direction_roll", "direction_index", "distance_roll",
                     "scatter_distance"):
            _require_nonnegative_exact_int(getattr(self, name), name)
        if self.direction_roll not in range(1, 7):
            raise ValueError("direction_roll must be in [1, 6]")
        if self.direction_index != self.direction_roll - 1:
            raise ValueError("direction_index must match direction_roll")
        if (not isinstance(self.distance_dice, tuple)
                or len(self.distance_dice) != 2
                or any(type(value) is not int or value not in range(1, 7)
                       for value in self.distance_dice)
                or self.distance_roll != sum(self.distance_dice)):
            raise ValueError("distance_dice must be a valid deterministic 2d6 roll")
        for name in ("distance_band", "table_id", "table_result"):
            _require_nonempty(getattr(self, name), name)

    def to_dict(self) -> dict[str, object]:
        return {
            "direction_roll": self.direction_roll,
            "direction_index": self.direction_index,
            "distance_dice": list(self.distance_dice),
            "distance_roll": self.distance_roll,
            "distance_band": self.distance_band,
            "table_id": self.table_id,
            "table_result": self.table_result,
            "scatter_distance": self.scatter_distance,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FireMissionScatterEvidence":
        dice = value["distance_dice"]
        if not isinstance(dice, Sequence) or isinstance(dice, (str, bytes)):
            raise TypeError("distance_dice must be a sequence")
        return cls(
            direction_roll=int(value["direction_roll"]),
            direction_index=int(value["direction_index"]),
            distance_dice=tuple(int(item) for item in dice),  # type: ignore[arg-type]
            distance_roll=int(value["distance_roll"]),
            distance_band=str(value["distance_band"]),
            table_id=str(value["table_id"]),
            table_result=str(value["table_result"]),
            scatter_distance=int(value["scatter_distance"]),
        )


@dataclass(frozen=True, slots=True)
class FireMission:
    """One authoritative mission lifecycle record without impact semantics.

    ``created_at`` is authoritative simulation time in milliseconds.  Flight
    duration remains exposed in whole seconds, while the private contract field
    ``flight_elapsed_remainder_ms`` preserves exact progress for sub-second
    fixed ticks without introducing floating-point state.
    """

    mission_id: str
    created_tick: int
    shooter_id: int
    weapon_id: int
    planned_hex: int
    profile_id: str
    authority_version: str
    execution_policy_id: str
    execution_policy_hash: str
    phase: FireMissionPhase
    created_at: int
    remaining_flight_seconds: int
    ammo_consumed: int
    ammunition_key: str
    cancelable: bool
    action_stream_bound: bool = False
    flight_elapsed_remainder_ms: int = 0
    cancellation_reason: IndirectFireCancellationReason | None = None
    active_duration_seconds: int = 0
    active_remaining_seconds: int = 0
    active_elapsed_remainder_ms: int = 0
    active_started_tick: int | None = None
    impact_hex: int | None = None
    affected_operator_ids: tuple[int, ...] = ()
    scatter_evidence: FireMissionScatterEvidence | None = None

    def __post_init__(self) -> None:
        for name in (
            "mission_id",
            "profile_id",
            "authority_version",
            "execution_policy_id",
            "ammunition_key",
        ):
            _require_nonempty(getattr(self, name), name)
        for name in (
            "created_tick",
            "shooter_id",
            "weapon_id",
            "planned_hex",
            "created_at",
            "remaining_flight_seconds",
            "ammo_consumed",
            "flight_elapsed_remainder_ms",
            "active_duration_seconds",
            "active_remaining_seconds",
            "active_elapsed_remainder_ms",
        ):
            _require_nonnegative_exact_int(getattr(self, name), name)
        if self.flight_elapsed_remainder_ms >= 1_000:
            raise ValueError("flight_elapsed_remainder_ms must be below 1000")
        if self.active_elapsed_remainder_ms >= 1_000:
            raise ValueError("active_elapsed_remainder_ms must be below 1000")
        if self.active_remaining_seconds > self.active_duration_seconds:
            raise ValueError("active_remaining_seconds exceeds active duration")
        for name in ("active_started_tick", "impact_hex"):
            value = getattr(self, name)
            if value is not None:
                _require_nonnegative_exact_int(value, name)
        if (not isinstance(self.affected_operator_ids, tuple)
                or self.affected_operator_ids
                != tuple(sorted(set(self.affected_operator_ids)))
                or any(type(value) is not int or value < 0
                       for value in self.affected_operator_ids)):
            raise ValueError("affected_operator_ids must be sorted unique ids")
        if self.scatter_evidence is not None and not isinstance(
            self.scatter_evidence, FireMissionScatterEvidence
        ):
            raise TypeError("scatter_evidence must be FireMissionScatterEvidence")
        _require_sha256(self.execution_policy_hash, "execution_policy_hash")
        if not isinstance(self.phase, IndirectFireMissionState):
            raise TypeError("phase must be FireMissionPhase")
        if type(self.cancelable) is not bool:
            raise TypeError("cancelable must be an exact bool")
        if type(self.action_stream_bound) is not bool:
            raise TypeError("action_stream_bound must be an exact bool")
        expected_cancelable = self.phase is IndirectFireMissionState.FLYING
        if self.cancelable is not expected_cancelable:
            raise ValueError("cancelable must match the FLYING mission phase")
        if self.phase is IndirectFireMissionState.CANCELLED:
            if not isinstance(
                self.cancellation_reason,
                IndirectFireCancellationReason,
            ):
                raise ValueError("cancelled mission requires cancellation_reason")
        elif self.cancellation_reason is not None:
            raise ValueError("only cancelled missions may have cancellation_reason")
        if self.phase in {
            IndirectFireMissionState.ACTIVE,
            IndirectFireMissionState.RESOLVED,
        } and (
            self.remaining_flight_seconds != 0
            or self.flight_elapsed_remainder_ms != 0
        ):
            raise ValueError("post-flight mission cannot retain flight time")
        if self.phase is IndirectFireMissionState.ACTIVE:
            if self.active_started_tick is None:
                raise ValueError("ACTIVE mission requires active_started_tick")
            if self.active_remaining_seconds <= 0:
                raise ValueError("ACTIVE mission requires remaining active time")
        if self.impact_hex is None:
            if self.scatter_evidence is not None or self.affected_operator_ids:
                raise ValueError("unresolved impact cannot retain impact evidence")
        elif self.scatter_evidence is None:
            raise ValueError("impact_hex requires scatter_evidence")
        if (self.phase is IndirectFireMissionState.RESOLVED
                and self.active_remaining_seconds != 0):
            raise ValueError("RESOLVED mission cannot retain active time")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": FIRE_MISSION_SCHEMA_VERSION,
            "mission_id": self.mission_id,
            "created_tick": self.created_tick,
            "shooter_id": self.shooter_id,
            "weapon_id": self.weapon_id,
            "planned_hex": self.planned_hex,
            "profile_id": self.profile_id,
            "authority_version": self.authority_version,
            "execution_policy_id": self.execution_policy_id,
            "execution_policy_hash": self.execution_policy_hash,
            "phase": self.phase.value,
            "created_at": self.created_at,
            "remaining_flight_seconds": self.remaining_flight_seconds,
            "ammo_consumed": self.ammo_consumed,
            "ammunition_key": self.ammunition_key,
            "cancelable": self.cancelable,
            "action_stream_bound": self.action_stream_bound,
            "flight_elapsed_remainder_ms": self.flight_elapsed_remainder_ms,
            "cancellation_reason": (
                None
                if self.cancellation_reason is None
                else self.cancellation_reason.value
            ),
            "active_duration_seconds": self.active_duration_seconds,
            "active_remaining_seconds": self.active_remaining_seconds,
            "active_elapsed_remainder_ms": self.active_elapsed_remainder_ms,
            "active_started_tick": self.active_started_tick,
            "impact_hex": self.impact_hex,
            "affected_operator_ids": list(self.affected_operator_ids),
            "affected_ledger": [
                IndirectFireAffectedLedger(
                    self.mission_id, operator_id
                ).to_dict()
                for operator_id in self.affected_operator_ids
            ],
            "scatter_evidence": (
                None if self.scatter_evidence is None
                else self.scatter_evidence.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FireMission":
        schema_version = value.get("schema_version")
        if schema_version not in {
            FIRE_MISSION_SCHEMA_VERSION,
            LEGACY_FIRE_MISSION_SCHEMA_VERSION,
        }:
            raise ValueError("unsupported indirect-fire mission schema")
        cancellation = value.get("cancellation_reason")
        scatter = value.get("scatter_evidence")
        affected = tuple(int(item) for item in value.get(
            "affected_operator_ids", ()
        ))
        active_duration = int(value.get("active_duration_seconds", 0))
        active_remaining = int(value.get("active_remaining_seconds", 0))
        active_started_tick = value.get("active_started_tick")
        phase = IndirectFireMissionState(str(value["phase"]))
        if schema_version == LEGACY_FIRE_MISSION_SCHEMA_VERSION:
            if phase is IndirectFireMissionState.ACTIVE:
                active_duration = max(active_duration, 1)
                active_remaining = max(active_remaining, 1)
                active_started_tick = int(value["created_tick"])
        return cls(
            mission_id=str(value["mission_id"]),
            created_tick=int(value["created_tick"]),
            shooter_id=int(value["shooter_id"]),
            weapon_id=int(value["weapon_id"]),
            planned_hex=int(value["planned_hex"]),
            profile_id=str(value["profile_id"]),
            authority_version=str(value["authority_version"]),
            execution_policy_id=str(value["execution_policy_id"]),
            execution_policy_hash=str(value["execution_policy_hash"]),
            phase=phase,
            created_at=int(value["created_at"]),
            remaining_flight_seconds=int(value["remaining_flight_seconds"]),
            ammo_consumed=int(value["ammo_consumed"]),
            ammunition_key=str(value["ammunition_key"]),
            cancelable=bool(value["cancelable"]),
            action_stream_bound=bool(value.get("action_stream_bound", False)),
            flight_elapsed_remainder_ms=int(
                value.get("flight_elapsed_remainder_ms", 0)
            ),
            cancellation_reason=(
                None
                if cancellation is None
                else IndirectFireCancellationReason(str(cancellation))
            ),
            active_duration_seconds=active_duration,
            active_remaining_seconds=active_remaining,
            active_elapsed_remainder_ms=int(
                value.get("active_elapsed_remainder_ms", 0)
            ),
            active_started_tick=(
                None if active_started_tick is None else int(active_started_tick)
            ),
            impact_hex=(
                None if value.get("impact_hex") is None
                else int(value["impact_hex"])
            ),
            affected_operator_ids=affected,
            scatter_evidence=(
                None if scatter is None
                else FireMissionScatterEvidence.from_dict(scatter)
            ),
        )


def fire_mission_sort_key(mission: FireMission) -> str:
    return mission.mission_id


def normalize_fire_missions(
    missions: Sequence[FireMission],
) -> tuple[FireMission, ...]:
    """Return unique missions in their authoritative deterministic order."""

    return _ordered_unique_missions(missions)


def advance_indirect_fire_missions(
    missions: Sequence[FireMission],
    delta_time_ms: int,
    *,
    current_tick: int | None = None,
) -> tuple[FireMission, ...]:
    """Advance PLANNED/FLYING time; ACTIVE timing is a separate commit phase."""

    _require_positive_exact_int(delta_time_ms, "delta_time_ms")
    if current_tick is not None:
        _require_nonnegative_exact_int(current_tick, "current_tick")
    ordered = _ordered_unique_missions(missions)
    advanced: list[FireMission] = []
    for mission in ordered:
        if mission.phase is IndirectFireMissionState.PLANNED:
            mission = replace(
                mission,
                phase=IndirectFireMissionState.FLYING,
                cancelable=True,
            )
        if mission.phase is IndirectFireMissionState.FLYING:
            elapsed = mission.flight_elapsed_remainder_ms + delta_time_ms
            whole_seconds, remainder_ms = divmod(elapsed, 1_000)
            remaining = max(
                0,
                mission.remaining_flight_seconds - whole_seconds,
            )
            if remaining == 0:
                mission = replace(
                    mission,
                    phase=IndirectFireMissionState.ACTIVE,
                    remaining_flight_seconds=0,
                    flight_elapsed_remainder_ms=0,
                    cancelable=False,
                    active_remaining_seconds=mission.active_duration_seconds,
                    active_started_tick=(
                        current_tick
                        if current_tick is not None
                        else mission.created_tick
                        + mission.remaining_flight_seconds
                    ),
                )
            else:
                mission = replace(
                    mission,
                    remaining_flight_seconds=remaining,
                    flight_elapsed_remainder_ms=remainder_ms,
                )
        advanced.append(mission)
    return tuple(sorted(advanced, key=fire_mission_sort_key))


def indirect_fire_active_delta_ms(
    missions: Sequence[FireMission],
    delta_time_ms: int,
) -> tuple[tuple[str, int], ...]:
    """Return deterministic ACTIVE time attributable to this Engine step.

    A mission already ACTIVE at the sealed step boundary consumes the entire
    delta.  A PLANNED/FLYING mission consumes only the residual after its exact
    flight boundary.  The result is derived from authoritative mission state;
    it is neither a second clock nor persisted WorldState.
    """

    _require_positive_exact_int(delta_time_ms, "delta_time_ms")
    active_deltas: list[tuple[str, int]] = []
    for mission in _ordered_unique_missions(missions):
        if mission.phase is IndirectFireMissionState.ACTIVE:
            active_deltas.append((mission.mission_id, delta_time_ms))
            continue
        if mission.phase not in {
            IndirectFireMissionState.PLANNED,
            IndirectFireMissionState.FLYING,
        }:
            continue
        remaining_flight_ms = (
            mission.remaining_flight_seconds * 1_000
            - mission.flight_elapsed_remainder_ms
        )
        if remaining_flight_ms < 0:
            raise ValueError("mission flight remainder exceeds remaining time")
        residual = max(0, delta_time_ms - remaining_flight_ms)
        if residual > 0:
            active_deltas.append((mission.mission_id, residual))
    return tuple(active_deltas)


def advance_indirect_fire_active_missions(
    missions: Sequence[FireMission],
    delta_time_ms: int,
    *,
    current_tick: int,
    delta_time_ms_by_mission: Mapping[str, int] | None = None,
) -> tuple[tuple[FireMission, ...], tuple[str, ...]]:
    """Advance ACTIVE clocks after triggers and return expired mission IDs."""

    _require_positive_exact_int(delta_time_ms, "delta_time_ms")
    _require_nonnegative_exact_int(current_tick, "current_tick")
    explicit_deltas: dict[str, int] | None = None
    if delta_time_ms_by_mission is not None:
        explicit_deltas = {}
        for mission_id, value in delta_time_ms_by_mission.items():
            _require_nonempty(mission_id, "mission_id")
            _require_nonnegative_exact_int(value, "mission delta_time_ms")
            explicit_deltas[mission_id] = value
    advanced: list[FireMission] = []
    expired: list[str] = []
    for mission in _ordered_unique_missions(missions):
        mission_delta = (
            explicit_deltas.get(mission.mission_id, 0)
            if explicit_deltas is not None
            else (
                delta_time_ms
                if mission.active_started_tick is not None
                and mission.active_started_tick < current_tick
                else 0
            )
        )
        if (
            mission.phase is IndirectFireMissionState.ACTIVE
            and mission_delta > 0
        ):
            elapsed = mission.active_elapsed_remainder_ms + mission_delta
            whole_seconds, remainder_ms = divmod(elapsed, 1_000)
            remaining = max(0, mission.active_remaining_seconds - whole_seconds)
            if remaining == 0:
                mission = replace(
                    mission,
                    phase=IndirectFireMissionState.RESOLVED,
                    active_remaining_seconds=0,
                    active_elapsed_remainder_ms=0,
                    cancelable=False,
                )
                expired.append(mission.mission_id)
            else:
                mission = replace(
                    mission,
                    active_remaining_seconds=remaining,
                    active_elapsed_remainder_ms=remainder_ms,
                )
        advanced.append(mission)
    return tuple(advanced), tuple(sorted(expired))


def can_cancel_mission(mission: FireMission) -> bool:
    if not isinstance(mission, FireMission):
        raise TypeError("mission must be FireMission")
    return (
        mission.phase is IndirectFireMissionState.FLYING
        and mission.cancelable
    )


def cancel_fire_mission(
    mission: FireMission,
    reason: IndirectFireCancellationReason,
) -> FireMission:
    """Return a cancelled replacement; ammunition is intentionally untouched."""

    if not isinstance(reason, IndirectFireCancellationReason):
        raise TypeError("reason must be IndirectFireCancellationReason")
    if not can_cancel_mission(mission):
        raise ValueError("INDIRECT_FIRE_MISSION_NOT_CANCELABLE")
    return replace(
        mission,
        phase=IndirectFireMissionState.CANCELLED,
        cancelable=False,
        cancellation_reason=reason,
    )


def _ordered_unique_missions(
    missions: Sequence[FireMission],
) -> tuple[FireMission, ...]:
    if isinstance(missions, (str, bytes)) or any(
        not isinstance(item, FireMission) for item in missions
    ):
        raise TypeError("missions must contain only FireMission values")
    ordered = tuple(sorted(missions, key=fire_mission_sort_key))
    identifiers = tuple(item.mission_id for item in ordered)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("mission_id values must be unique")
    return ordered


def _require_nonnegative_exact_int(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative exact int")


def _require_positive_exact_int(value: object, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive exact int")


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


__all__ = [
    "FIRE_MISSION_SCHEMA_VERSION",
    "LEGACY_FIRE_MISSION_SCHEMA_VERSION",
    "FireMission",
    "FireMissionPhase",
    "FireMissionScatterEvidence",
    "IndirectFireActiveAreaView",
    "IndirectFireAffectedLedger",
    "advance_indirect_fire_active_missions",
    "advance_indirect_fire_missions",
    "can_cancel_mission",
    "cancel_fire_mission",
    "fire_mission_sort_key",
    "normalize_fire_missions",
]
