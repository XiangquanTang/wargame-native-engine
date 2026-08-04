from __future__ import annotations

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.systems.movement import distance


def can_see(observer: OperatorState, target: OperatorState,
            *, tolerance: float = 1e-9) -> bool:
    return (observer.alive and observer.deployed and target.alive and target.deployed
            and observer.side != target.side
            and distance(observer.position, target.position)
            <= observer.sensor_range + tolerance)
