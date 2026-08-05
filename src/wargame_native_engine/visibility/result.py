from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class VisibilityResult:
    line_of_sight: bool
    observable: bool
    distance: int
    observation_limit: float
    blocking_hexes: tuple[int, ...]
    modifiers: tuple[dict[str, Any], ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"line_of_sight": bool(self.line_of_sight),
                "observable": bool(self.observable), "distance": int(self.distance),
                "observation_limit": float(self.observation_limit),
                "blocking_hexes": list(self.blocking_hexes),
                "modifiers": [dict(item) for item in self.modifiers],
                "reason": self.reason}
