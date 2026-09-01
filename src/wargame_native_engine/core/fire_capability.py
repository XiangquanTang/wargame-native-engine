"""Detached, read-only fire opportunity models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from wargame_native_engine.rules.indirect_fire import (
    ArtilleryClass,
    IndirectRangePolicy,
    IndirectTargetDomain,
)


@dataclass(frozen=True, slots=True)
class GuidedFireCapability:
    """One currently legal guided-fire opportunity, not an executable action."""

    guider_id: int
    shooter_id: int
    weapon_id: int
    target_id: int
    guidance_profile_id: str

    def __post_init__(self) -> None:
        for name in ("guider_id", "shooter_id", "weapon_id", "target_id"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative exact int")
        if (
            not isinstance(self.guidance_profile_id, str)
            or not self.guidance_profile_id.strip()
        ):
            raise ValueError("guidance_profile_id must be a non-empty string")


def guided_fire_capability_sort_key(
    value: GuidedFireCapability,
) -> tuple[int, int, int, int]:
    return (
        value.shooter_id,
        value.guider_id,
        value.weapon_id,
        value.target_id,
    )


class IndirectFireCapabilityStatus(str, Enum):
    """Execution readiness of a detached indirect-fire authority record."""

    AVAILABLE = "AVAILABLE"
    BLOCKED = "BLOCKED"


class IndirectFireTargetHexStatus(str, Enum):
    """Read-only target-hex validation outcome."""

    VALID = "VALID"
    INVALID = "INVALID"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class IndirectFireCapability:
    """One side-owned indirect-fire authority record.

    This is deliberately not an action and contains no target hex.  A blocked
    record lets trusted Human/Agent adapters explain incomplete authority
    without turning UNKNOWN fields into executable rules.
    """

    shooter_id: int
    weapon_id: int
    artillery_class: ArtilleryClass
    profile_id: str
    range_policy: IndirectRangePolicy
    minimum_range: int | None
    maximum_range: int | None
    target_policy: IndirectTargetDomain
    status: IndirectFireCapabilityStatus
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("shooter_id", "weapon_id"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative exact int")
        if not isinstance(self.artillery_class, ArtilleryClass):
            raise TypeError("artillery_class must be ArtilleryClass")
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("profile_id must be a non-empty string")
        if not isinstance(self.range_policy, IndirectRangePolicy):
            raise TypeError("range_policy must be IndirectRangePolicy")
        for name in ("minimum_range", "maximum_range"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a non-negative exact int or None")
        if (
            self.minimum_range is not None
            and self.maximum_range is not None
            and self.minimum_range > self.maximum_range
        ):
            raise ValueError("minimum_range cannot exceed maximum_range")
        if not isinstance(self.target_policy, IndirectTargetDomain):
            raise TypeError("target_policy must be IndirectTargetDomain")
        if not isinstance(self.status, IndirectFireCapabilityStatus):
            raise TypeError("status must be IndirectFireCapabilityStatus")
        if (
            not isinstance(self.blockers, tuple)
            or any(not isinstance(value, str) or not value for value in self.blockers)
            or self.blockers != tuple(sorted(set(self.blockers)))
        ):
            raise ValueError("blockers must be sorted unique non-empty strings")
        if self.status is IndirectFireCapabilityStatus.AVAILABLE and self.blockers:
            raise ValueError("AVAILABLE capability cannot contain blockers")
        if self.status is IndirectFireCapabilityStatus.BLOCKED and not self.blockers:
            raise ValueError("BLOCKED capability requires blockers")

    def to_record(self) -> dict[str, object]:
        return {
            "shooter_id": self.shooter_id,
            "weapon_id": self.weapon_id,
            "artillery_class": self.artillery_class.value,
            "profile_id": self.profile_id,
            "range_policy": self.range_policy.value,
            "minimum_range": self.minimum_range,
            "maximum_range": self.maximum_range,
            "target_policy": self.target_policy.value,
            "status": self.status.value,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True, slots=True)
class IndirectFireTargetHexValidation:
    """Detached preview result; it never creates a mission or action."""

    status: IndirectFireTargetHexStatus
    reason: str
    shooter_id: int
    weapon_id: int
    target_hex: int
    distance: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, IndirectFireTargetHexStatus):
            raise TypeError("status must be IndirectFireTargetHexStatus")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("reason must be a non-empty string")
        for name in ("shooter_id", "weapon_id", "target_hex"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative exact int")
        if self.distance is not None and (
            type(self.distance) is not int or self.distance < 0
        ):
            raise ValueError("distance must be a non-negative exact int or None")

    def to_record(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "shooter_id": self.shooter_id,
            "weapon_id": self.weapon_id,
            "target_hex": self.target_hex,
            "distance": self.distance,
        }


def indirect_fire_capability_sort_key(
    value: IndirectFireCapability,
) -> tuple[int, int, str]:
    return value.shooter_id, value.weapon_id, value.profile_id
