"""Typed request boundary for minimal BOARD and UNLOAD actions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BoardRequest:
    tick: int
    action_id: str
    side: str
    actor_id: int
    carrier_id: int
    passenger_id: int
    profile_id: str
    authority_version: str
    authority_hash: str

    def __post_init__(self) -> None:
        _request_fields(self)
        if self.actor_id != self.passenger_id:
            raise ValueError("BOARD actor must be passenger")


@dataclass(frozen=True, slots=True)
class UnloadRequest:
    tick: int
    action_id: str
    side: str
    actor_id: int
    carrier_id: int
    passenger_id: int
    profile_id: str
    authority_version: str
    authority_hash: str

    def __post_init__(self) -> None:
        _request_fields(self)
        if self.actor_id != self.carrier_id:
            raise ValueError("UNLOAD actor must be carrier")


@dataclass(frozen=True, slots=True)
class BoardValidationResult:
    valid: bool
    reason: str
    request: BoardRequest | None = None


@dataclass(frozen=True, slots=True)
class UnloadValidationResult:
    valid: bool
    reason: str
    request: UnloadRequest | None = None


def _request_fields(value: BoardRequest | UnloadRequest) -> None:
    for name in ("tick", "actor_id", "carrier_id", "passenger_id"):
        item = getattr(value, name)
        if type(item) is not int or item < 0:
            raise ValueError(f"{name} must be a non-negative exact integer")
    if value.carrier_id == value.passenger_id:
        raise ValueError("passenger and carrier must differ")
    for name in ("action_id", "side", "profile_id", "authority_version"):
        item = getattr(value, name)
        if not isinstance(item, str) or not item:
            raise ValueError(f"{name} must be non-empty")
    if not isinstance(value.authority_hash, str) or len(value.authority_hash) != 64:
        raise ValueError("authority_hash must be a sha256-like hash")


__all__ = [
    "BoardRequest",
    "BoardValidationResult",
    "UnloadRequest",
    "UnloadValidationResult",
]
