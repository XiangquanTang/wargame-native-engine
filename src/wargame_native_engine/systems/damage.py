from __future__ import annotations

from wargame_native_engine.core.operator import OperatorState


def apply_damage(target: OperatorState, amount: float) -> tuple[float, float, bool]:
    before = target.health
    target.health = max(0.0, target.health - max(0.0, amount))
    destroyed = before > 0 and target.health <= 0
    if destroyed:
        target.alive = False
        target.state = "destroyed"
        target.speed = 0.0
    return before, target.health, destroyed
