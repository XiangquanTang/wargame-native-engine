from __future__ import annotations

import math
from typing import Iterable

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.rules.movement_rules import MovementRules


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
                  rules: MovementRules,
                  occupied_positions: Iterable[tuple[float, float]] = ()) -> str | None:
    if not operator.alive:
        return "actor_destroyed"
    if not operator.deployed:
        return "actor_not_deployed"
    if not (0 <= target[0] <= width and 0 <= target[1] <= height):
        return "outside_map_bounds"
    is_blocked = any(distance(target, item) <= rules.float_tolerance
                     for item in blocked_positions)
    terrain_type = "blocked" if is_blocked else "open"
    multipliers = dict(rules.terrain_multipliers)
    multiplier = multipliers.get(terrain_type, 1.0)
    if terrain_type in rules.impassable_terrain or multiplier <= 0:
        return "terrain_blocked"
    configured_range = rules.for_operator(operator)
    limit = min(configured_range,
                operator.max_speed or configured_range) * multiplier
    if distance(operator.position, target) > limit + rules.float_tolerance:
        return "move_range_exceeded"
    if (not rules.allow_position_overlap
            and any(distance(target, item) < rules.minimum_unit_spacing
                    - rules.float_tolerance for item in occupied_positions)):
        return "minimum_unit_spacing"
    return None
