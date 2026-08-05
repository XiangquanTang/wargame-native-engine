from __future__ import annotations

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.rules.combat_rules import CombatRules
from wargame_native_engine.rules.visibility_rules import VisibilityRules
from wargame_native_engine.systems.movement import distance
from wargame_native_engine.systems.visibility import can_see


def validate_attack(attacker: OperatorState, target: OperatorState, *,
                    combat_rules: CombatRules,
                    visibility_rules: VisibilityRules,
                    weapon_id: int) -> str | None:
    if not attacker.alive:
        return "attacker_destroyed"
    if not target.alive:
        return "target_destroyed"
    if attacker.side == target.side:
        return "friendly_fire_not_supported"
    if not attacker.weapon_ids:
        return "no_available_weapon"
    if weapon_id not in attacker.weapon_ids:
        return "weapon_not_available"
    if not can_see(attacker, target, visibility_rules):
        return "target_not_visible"
    if (distance(attacker.position, target.position)
            > combat_rules.attack_range(attacker, weapon_id)
            + combat_rules.float_tolerance):
        return "target_out_of_range"
    return None
