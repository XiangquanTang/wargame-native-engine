"""Injectable server-clock policies for decision-window admission."""

from __future__ import annotations

from enum import Enum
import math
from time import monotonic
from typing import Protocol


class DeadlineMode(str, Enum):
    SYNCHRONOUS = "synchronous"
    SERVER_MONOTONIC = "server_monotonic"


class MonotonicClock(Protocol):
    def now(self) -> float:
        """Return a monotonically increasing server-local value in seconds."""


class SystemMonotonicClock:
    def now(self) -> float:
        return monotonic()


def require_deadline_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("deadline_seconds must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("deadline_seconds must be a positive finite number")
    return result
