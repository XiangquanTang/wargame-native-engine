from __future__ import annotations

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.systems.movement import distance
from wargame_native_engine.systems.visibility import can_see


def validate_attack(attacker: OperatorState, target: OperatorState) -> str | None:
    if not attacker.alive:
        return "attacker_destroyed"
    if not target.alive:
        return "target_destroyed"
    if attacker.side == target.side:
        return "friendly_fire_not_supported"
    if not attacker.weapon_ids:
        return "no_available_weapon"
    if not can_see(attacker, target):
        return "target_not_visible"
    if distance(attacker.position, target.position) > attacker.attack_range + 1e-9:
        return "target_out_of_range"
    return None
