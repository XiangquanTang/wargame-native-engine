from __future__ import annotations

from typing import Iterable

from wargame_native_engine.systems.movement import distance


def validate_deployment(position: tuple[float, float], *, width: float,
                        height: float,
                        blocked_positions: Iterable[tuple[float, float]],
                        tolerance: float = 1e-9) -> str | None:
    if not (0 <= position[0] <= width and 0 <= position[1] <= height):
        return "deployment_outside_map"
    if any(distance(position, item) <= tolerance for item in blocked_positions):
        return "deployment_terrain_blocked"
    return None
