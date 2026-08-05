from __future__ import annotations

from typing import Iterable

from wargame_native_engine.rules.movement_rules import MovementRules
from wargame_native_engine.systems.movement import distance


def validate_deployment(position: tuple[float, float], *, width: float,
                        height: float,
                        blocked_positions: Iterable[tuple[float, float]],
                        rules: MovementRules,
                        occupied_positions: Iterable[tuple[float, float]] = ()) -> str | None:
    if not (0 <= position[0] <= width and 0 <= position[1] <= height):
        return "deployment_outside_map"
    is_blocked = any(distance(position, item) <= rules.float_tolerance
                     for item in blocked_positions)
    terrain_type = "blocked" if is_blocked else "open"
    if (terrain_type in rules.impassable_terrain
            or dict(rules.terrain_multipliers).get(terrain_type, 1.0) <= 0):
        return "deployment_terrain_blocked"
    if (not rules.allow_position_overlap
            and any(distance(position, item) < rules.minimum_unit_spacing
                    - rules.float_tolerance for item in occupied_positions)):
        return "minimum_unit_spacing"
    return None
