"""Immutable capability and transition records for minimal boarding."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.io.serialization import canonical_hash

if TYPE_CHECKING:
    from wargame_native_engine.adjudication.boarding import (
        BoardRequest,
        UnloadRequest,
    )


class BoardingTransitionType(str, Enum):
    BOARD = "BOARD"
    UNLOAD = "UNLOAD"


class BoardingTransitionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"


class BoardingCapabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class BoardingCapabilityAssessment:
    operation_type: BoardingTransitionType
    actor_id: int
    status: BoardingCapabilityStatus
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.operation_type, BoardingTransitionType):
            raise TypeError("operation_type must be BoardingTransitionType")
        if type(self.actor_id) is not int or self.actor_id < 0:
            raise ValueError("actor_id must be a non-negative exact int")
        if not isinstance(self.status, BoardingCapabilityStatus):
            raise TypeError("status must be BoardingCapabilityStatus")
        if (
            not isinstance(self.blockers, tuple)
            or self.blockers != tuple(sorted(set(self.blockers)))
            or any(not isinstance(value, str) or not value for value in self.blockers)
        ):
            raise ValueError("blockers must be sorted unique non-empty strings")
        if self.status is BoardingCapabilityStatus.AVAILABLE and self.blockers:
            raise ValueError("AVAILABLE assessment cannot contain blockers")
        if self.status is not BoardingCapabilityStatus.AVAILABLE and not self.blockers:
            raise ValueError("unavailable assessment requires blockers")


@dataclass(frozen=True, slots=True)
class BoardCapability:
    passenger_id: int
    carrier_id: int
    profile_id: str
    authority_version: str
    authority_hash: str

    def __post_init__(self) -> None:
        _identifier(self.passenger_id, "passenger_id")
        _identifier(self.carrier_id, "carrier_id")
        _authority(self.profile_id, self.authority_version, self.authority_hash)
        if self.passenger_id == self.carrier_id:
            raise ValueError("passenger and carrier must differ")


@dataclass(frozen=True, slots=True)
class UnloadCapability:
    carrier_id: int
    passenger_id: int
    profile_id: str
    authority_version: str
    authority_hash: str

    def __post_init__(self) -> None:
        _identifier(self.carrier_id, "carrier_id")
        _identifier(self.passenger_id, "passenger_id")
        _authority(self.profile_id, self.authority_version, self.authority_hash)
        if self.passenger_id == self.carrier_id:
            raise ValueError("passenger and carrier must differ")


def _encode_board_action(
    side: str,
    value: BoardCapability | BoardRequest,
) -> EngineAction:
    return EngineAction(
        side=side,
        actor_id=value.passenger_id,
        action_type=ActionType.BOARD,
        target_id=value.carrier_id,
        parameters=_boarding_action_parameters(value),
    )


def _encode_unload_action(
    side: str,
    value: UnloadCapability | UnloadRequest,
) -> EngineAction:
    return EngineAction(
        side=side,
        actor_id=value.carrier_id,
        action_type=ActionType.UNLOAD,
        target_id=value.passenger_id,
        parameters=_boarding_action_parameters(value),
    )


def _boarding_action_parameters(
    value: BoardCapability | UnloadCapability | BoardRequest | UnloadRequest,
) -> dict[str, str]:
    return {
        "profile_id": value.profile_id,
        "authority_version": value.authority_version,
        "authority_hash": value.authority_hash,
    }


@dataclass(frozen=True, slots=True)
class BoardingTransition:
    transition_id: str
    transition_type: BoardingTransitionType
    actor_id: int
    carrier_id: int
    passenger_id: int
    remaining_ms: int
    transition_start_tick: int
    completed_tick: int | None
    status: BoardingTransitionStatus
    profile_id: str
    authority_version: str
    authority_hash: str

    def __post_init__(self) -> None:
        if not self.transition_id:
            raise ValueError("transition_id must be non-empty")
        if not isinstance(self.transition_type, BoardingTransitionType):
            raise TypeError("transition_type must be BoardingTransitionType")
        for name in (
            "actor_id", "carrier_id", "passenger_id", "transition_start_tick",
        ):
            _identifier(getattr(self, name), name)
        if self.completed_tick is not None:
            _identifier(self.completed_tick, "completed_tick")
        if not isinstance(self.status, BoardingTransitionStatus):
            raise TypeError("status must be BoardingTransitionStatus")
        if self.remaining_ms < 0:
            raise ValueError("remaining_ms must be non-negative")
        if self.status is BoardingTransitionStatus.ACTIVE:
            if self.remaining_ms == 0 or self.completed_tick is not None:
                raise ValueError("ACTIVE transition requires remaining time")
        elif self.remaining_ms != 0 or self.completed_tick is None:
            raise ValueError("COMPLETED transition requires completed_tick")
        if (
            self.completed_tick is not None
            and self.completed_tick < self.transition_start_tick
        ):
            raise ValueError("completed_tick precedes transition_start_tick")
        if self.carrier_id == self.passenger_id:
            raise ValueError("passenger and carrier must differ")
        _authority(self.profile_id, self.authority_version, self.authority_hash)

    def to_dict(self) -> dict[str, object]:
        return {
            "transition_id": self.transition_id,
            "type": self.transition_type.value,
            "actor_id": self.actor_id,
            "carrier_id": self.carrier_id,
            "passenger_id": self.passenger_id,
            "remaining_ms": self.remaining_ms,
            "transition_start_tick": self.transition_start_tick,
            "completed_tick": self.completed_tick,
            "status": self.status.value,
            "profile_id": self.profile_id,
            "authority_version": self.authority_version,
            "authority_hash": self.authority_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BoardingTransition":
        return cls(
            transition_id=str(value["transition_id"]),
            transition_type=BoardingTransitionType(str(value["type"]).upper()),
            actor_id=int(value["actor_id"]),
            carrier_id=int(value["carrier_id"]),
            passenger_id=int(value["passenger_id"]),
            remaining_ms=int(value["remaining_ms"]),
            transition_start_tick=int(value.get(
                "transition_start_tick", value.get("created_tick")
            )),
            completed_tick=(
                None if value.get("completed_tick") is None
                else int(value["completed_tick"])
            ),
            status=BoardingTransitionStatus(str(
                value.get("status", BoardingTransitionStatus.ACTIVE.value)
            ).upper()),
            profile_id=str(value["profile_id"]),
            authority_version=str(value["authority_version"]),
            authority_hash=str(value["authority_hash"]),
        )


def boarding_transition_id(action_id: str, transition_type: BoardingTransitionType) -> str:
    if not isinstance(action_id, str) or not action_id:
        raise ValueError("action_id must be non-empty")
    return "boarding-" + canonical_hash({
        "action_id": action_id,
        "type": transition_type.value,
    })[:24]


def boarding_transition_sort_key(value: BoardingTransition) -> tuple[object, ...]:
    return (
        value.transition_start_tick,
        value.carrier_id,
        value.passenger_id,
        value.transition_type.value,
        value.transition_id,
    )


def normalize_boarding_transitions(
    transitions: Sequence[BoardingTransition],
) -> tuple[BoardingTransition, ...]:
    result = tuple(sorted(transitions, key=boarding_transition_sort_key))
    if len({item.transition_id for item in result}) != len(result):
        raise ValueError("boarding transition IDs must be unique")
    active = tuple(
        item for item in result
        if item.status is BoardingTransitionStatus.ACTIVE
    )
    carriers = [item.carrier_id for item in active]
    passengers = [item.passenger_id for item in active]
    if len(set(carriers)) != len(carriers):
        raise ValueError("carrier cannot have multiple boarding transitions")
    if len(set(passengers)) != len(passengers):
        raise ValueError("passenger cannot have multiple boarding transitions")
    return result


def advance_boarding_transitions(
    transitions: Sequence[BoardingTransition],
    delta_time_ms: int,
    *,
    completed_tick: int,
) -> tuple[tuple[BoardingTransition, ...], tuple[BoardingTransition, ...]]:
    if type(delta_time_ms) is not int or delta_time_ms <= 0:
        raise ValueError("delta_time_ms must be a positive exact integer")
    retained: list[BoardingTransition] = []
    completed: list[BoardingTransition] = []
    for item in normalize_boarding_transitions(transitions):
        if item.status is BoardingTransitionStatus.COMPLETED:
            retained.append(item)
            continue
        remaining_ms = max(0, item.remaining_ms - delta_time_ms)
        if remaining_ms == 0:
            updated = replace(
                item,
                remaining_ms=0,
                completed_tick=completed_tick,
                status=BoardingTransitionStatus.COMPLETED,
            )
            completed.append(updated)
        else:
            updated = replace(item, remaining_ms=remaining_ms)
        retained.append(updated)
    return (
        normalize_boarding_transitions(retained),
        tuple(sorted(completed, key=boarding_transition_sort_key)),
    )


def board_capability_sort_key(value: BoardCapability) -> tuple[object, ...]:
    return value.carrier_id, value.passenger_id, value.profile_id


def unload_capability_sort_key(value: UnloadCapability) -> tuple[object, ...]:
    return value.carrier_id, value.passenger_id, value.profile_id


def _identifier(value: object, field: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative exact integer")


def _authority(profile_id: object, version: object, content_hash: object) -> None:
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("profile_id must be non-empty")
    if not isinstance(version, str) or not version:
        raise ValueError("authority_version must be non-empty")
    if not isinstance(content_hash, str) or len(content_hash) != 64:
        raise ValueError("authority_hash must be a sha256-like hash")


__all__ = [
    "BoardCapability",
    "BoardingCapabilityAssessment",
    "BoardingCapabilityStatus",
    "BoardingTransition",
    "BoardingTransitionStatus",
    "BoardingTransitionType",
    "UnloadCapability",
    "advance_boarding_transitions",
    "board_capability_sort_key",
    "boarding_transition_id",
    "boarding_transition_sort_key",
    "normalize_boarding_transitions",
    "unload_capability_sort_key",
]
