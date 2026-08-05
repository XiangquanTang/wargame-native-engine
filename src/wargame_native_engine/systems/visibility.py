from __future__ import annotations

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.rules.config import load_ruleset
from wargame_native_engine.rules.visibility_rules import VisibilityRules
from wargame_native_engine.systems.movement import distance


def can_see(observer: OperatorState, target: OperatorState,
            rules: VisibilityRules | None = None) -> bool:
    if rules is None:
        rules = load_ruleset().visibility
    sensor_range = rules.sensor_range(observer)
    visible_distance = distance(observer.position, target.position)
    boundary = (visible_distance <= sensor_range
                if rules.inclusive_boundary else visible_distance < sensor_range)
    return (observer.alive and observer.deployed and target.alive and target.deployed
            and observer.side != target.side
            and boundary)
