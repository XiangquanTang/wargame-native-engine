from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CombatRules:
    deterministic_hits: bool = True
    simultaneous_damage: bool = True
