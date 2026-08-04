from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


_MASK = (1 << 64) - 1


@dataclass(slots=True)
class NativeRNG:
    """Small serializable deterministic generator; all draws are countable."""

    seed: int
    state: int = 0
    draws: int = 0

    def __post_init__(self) -> None:
        if self.state == 0:
            self.state = (int(self.seed) ^ 0x9E3779B97F4A7C15) & _MASK

    def random(self) -> float:
        self.state = (6364136223846793005 * self.state + 1442695040888963407) & _MASK
        self.draws += 1
        return self.state / float(1 << 64)

    def snapshot(self) -> dict[str, int]:
        return {"seed": int(self.seed), "state": self.state, "draws": self.draws}

    @classmethod
    def restore(cls, value: Mapping[str, Any]) -> "NativeRNG":
        return cls(int(value["seed"]), int(value["state"]), int(value["draws"]))
