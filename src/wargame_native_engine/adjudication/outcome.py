from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CombatOutcome:
    vehicle_losses: int = 0
    squad_losses: int = 0
    suppression_applied: bool = False
    destroyed: bool = False
    no_effect: bool = False

    def __post_init__(self) -> None:
        if self.vehicle_losses < 0 or self.squad_losses < 0:
            raise ValueError("losses cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {"vehicle_losses": self.vehicle_losses,
                "squad_losses": self.squad_losses,
                "suppression_applied": self.suppression_applied,
                "destroyed": self.destroyed, "no_effect": self.no_effect}
