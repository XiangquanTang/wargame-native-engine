from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OperatorRules:
    destroyed_health: float = 0.0
