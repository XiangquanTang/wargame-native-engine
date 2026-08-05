from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.visibility.result import VisibilityResult


@dataclass(frozen=True, slots=True)
class WeaponProfile:
    weapon_id: int | str
    weapon_class: str
    maximum_range: int
    target_domains: frozenset[UnitDomain]
    ammunition_key: str = "0"
    cooldown_ticks: int | None = None
    requires_unfolded: bool = True
    can_fire_while_moving: bool = False


@dataclass(slots=True)
class DirectFireRequest:
    tick: int
    episode_seed: int
    action_id: str
    attacker: OperatorState
    target: OperatorState
    weapon: WeaponProfile
    visibility: VisibilityResult
    distance: int
    target_domain: UnitDomain
    target_armor: ArmorClass
    ammunition_before: int
    weapon_cooldown_before: int = 0
    weapon_unfolded: bool = True
    elevation_difference: int = 0
    attacker_suppressed: bool = False
    attacker_moving: bool = False
    target_terrain: str = "OPEN"
    target_concealed: bool = False
    target_moving: bool = False
    target_stacked: bool = False
    target_marching: bool = False
    attacker_transitioning: bool = False
    attacker_tank_on_water: bool = False
    attacker_hovering: bool = True
    target_external_direct_fire_prohibited: bool = False
    carried_passengers: tuple[OperatorState, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActionLegalityResult:
    legal: bool
    reason_code: str
    evidence: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"legal": self.legal, "reason_code": self.reason_code,
                "evidence": dict(self.evidence)}
