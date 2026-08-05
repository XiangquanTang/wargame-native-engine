from __future__ import annotations

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.rules.combat_rules import CombatRules


def apply_damage(target: OperatorState, amount: float,
                 rules: CombatRules) -> tuple[float, float, bool]:
    before = target.health
    target.health = max(rules.minimum_health,
                        target.health - max(rules.minimum_health, amount))
    destroyed = (before > rules.destroyed_threshold
                 and target.health <= rules.destroyed_threshold)
    if destroyed:
        target.alive = False
        target.state = "destroyed"
        target.speed = 0.0
    return before, target.health, destroyed
