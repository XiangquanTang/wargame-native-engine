from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TerrainRules:
    blocked_tolerance: float = 1e-9
