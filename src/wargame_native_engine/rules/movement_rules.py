from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MovementRules:
    distance_tolerance: float = 1e-9
