from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from wargame_native_engine.core.operator import OperatorState


@dataclass(frozen=True, slots=True)
class CombatRules:
    default_attack_range: float
    default_damage: float
    deterministic_hits: bool
    consume_ammunition: bool
    simultaneous_damage: bool
    destroyed_actor_executes_submitted_action: bool
    multi_attack_resolution: str
    invalidated_target_policy: str
    minimum_health: float
    destroyed_threshold: float
    float_tolerance: float
    weapon_profiles: tuple[tuple[int, tuple[tuple[str, Any], ...]], ...]

    def weapon_profile(self, weapon_id: int) -> dict[str, Any]:
        profiles = {key: dict(value) for key, value in self.weapon_profiles}
        return profiles.get(int(weapon_id), {
            "attack_range": self.default_attack_range,
            "damage": self.default_damage,
        })

    def attack_range(self, operator: "OperatorState", weapon_id: int) -> float:
        if operator.attack_range > 0:
            return operator.attack_range
        return float(self.weapon_profile(weapon_id).get(
            "attack_range", self.default_attack_range))

    def damage(self, operator: "OperatorState", weapon_id: int) -> float:
        if operator.attack_damage > 0:
            return operator.attack_damage
        return float(self.weapon_profile(weapon_id).get(
            "damage", self.default_damage))
