from __future__ import annotations

import math
from typing import Iterable

from wargame_native_engine.core.operator import OperatorState


def distance(left: tuple[float, float], right: tuple[float, float]) -> float:
    return math.hypot(right[0] - left[0], right[1] - left[1])


def move_toward(start: tuple[float, float], target: tuple[float, float],
                limit: float) -> tuple[float, float]:
    total = distance(start, target)
    if total <= limit or total == 0:
        return target
    scale = limit / total
    return (start[0] + (target[0] - start[0]) * scale,
            start[1] + (target[1] - start[1]) * scale)


def validate_move(operator: OperatorState, target: tuple[float, float], *,
                  width: float, height: float,
                  blocked_positions: Iterable[tuple[float, float]],
                  tolerance: float = 1e-9) -> str | None:
    if not operator.alive:
        return "actor_destroyed"
    if not operator.deployed:
        return "actor_not_deployed"
    if not (0 <= target[0] <= width and 0 <= target[1] <= height):
        return "outside_map_bounds"
    limit = min(operator.move_range, operator.max_speed)
    if distance(operator.position, target) > limit + tolerance:
        return "move_range_exceeded"
    if any(distance(target, item) <= tolerance for item in blocked_positions):
        return "terrain_blocked"
    return None
