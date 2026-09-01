"""Typed contracts for explicit Native concealment posture changes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConcealmentPosture(str, Enum):
    """Authoritative unit posture; visibility remains observer-relative."""

    EXPOSED = "EXPOSED"
    CONCEALED = "CONCEALED"


@dataclass(frozen=True, slots=True)
class ConcealmentCapability:
    """One deterministic, side-owned ENTER_CONCEALMENT opportunity."""

    operator_id: int
    available: bool
    reason: str

    def __post_init__(self) -> None:
        if type(self.operator_id) is not int or self.operator_id < 0:
            raise ValueError("operator_id must be a non-negative exact integer")
        if type(self.available) is not bool:
            raise TypeError("available must be an exact bool")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("reason must be non-empty")

    def to_record(self) -> dict[str, object]:
        return {
            "operator_id": self.operator_id,
            "action": "ENTER_CONCEALMENT",
            "available": self.available,
            "reason": self.reason,
        }


def concealment_capability_sort_key(
    value: ConcealmentCapability,
) -> tuple[int, bool, str]:
    return value.operator_id, not value.available, value.reason


__all__ = [
    "ConcealmentCapability",
    "ConcealmentPosture",
    "concealment_capability_sort_key",
]
