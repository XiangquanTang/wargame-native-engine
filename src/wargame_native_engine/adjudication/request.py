from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain
from wargame_native_engine.core.fire_intent import FireMode
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.visibility.result import VisibilityResult


DIRECT_ONLY_FIRE_MODES = frozenset({FireMode.DIRECT})


class DirectFireResourcePolicy(str, Enum):
    AMMUNITION_KEY = "AMMUNITION_KEY"
    SELF_AS_MUNITION = "SELF_AS_MUNITION"


@dataclass(frozen=True, slots=True)
class WeaponProfile:
    weapon_id: int | str
    weapon_class: str
    maximum_range: int
    target_domains: frozenset[UnitDomain]
    ammunition_key: str | None = "0"
    cooldown_ticks: int | None = None
    requires_unfolded: bool = True
    # Ordinary DIRECT also requires a completed weapon lock while travelling.
    can_fire_while_moving: bool = False
    supported_fire_modes: frozenset[FireMode] = DIRECT_ONLY_FIRE_MODES

    def __post_init__(self) -> None:
        if self.ammunition_key is not None and (
            not isinstance(self.ammunition_key, str) or not self.ammunition_key
        ):
            raise ValueError("ammunition_key must be None or non-empty text")
        if not isinstance(self.supported_fire_modes, frozenset):
            raise TypeError("supported_fire_modes must be a frozenset")
        if not self.supported_fire_modes or any(
            not isinstance(item, FireMode) for item in self.supported_fire_modes
        ):
            raise TypeError("supported_fire_modes must contain FireMode values")
        if FireMode.DIRECT not in self.supported_fire_modes:
            raise ValueError("WeaponProfile must preserve DIRECT compatibility")


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
    ammunition_before: int | None
    weapon_cooldown_before: int = 0
    weapon_unfolded: bool = True
    # None means a DIRECT overshoot: no firing table is applicable.
    elevation_difference: int | None = 0
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
    resource_policy: DirectFireResourcePolicy = (
        DirectFireResourcePolicy.AMMUNITION_KEY
    )
    fire_mode: FireMode = FireMode.DIRECT
    # Appended for positional-constructor compatibility; sealed into replays.
    weapon_locked: bool = False
    locked_weapon_id: int | None = None

    def __post_init__(self) -> None:
        if (self.elevation_difference is None and self.permits_out_of_range_attempt
                and self.distance > self.weapon.maximum_range):
            return
        if isinstance(self.elevation_difference, bool) or not isinstance(self.elevation_difference, int):
            raise ValueError("elevation_difference must be an integer elevation-level difference")

    @property
    def permits_out_of_range_attempt(self) -> bool:
        """Only ordinary DIRECT ammunition shots may be wasted beyond range."""
        return (self.fire_mode is FireMode.DIRECT
                and self.resource_policy is DirectFireResourcePolicy.AMMUNITION_KEY)


def fire_elevation_level_difference(
    map_profile: Any, shooter_hex: int, target_hex: int, *,
    distance: int, weapon: WeaponProfile,
    fire_mode: FireMode = FireMode.DIRECT,
    resource_policy: DirectFireResourcePolicy = DirectFireResourcePolicy.AMMUNITION_KEY,
) -> int | None:
    """Exact grades; an ordinary overshoot never requires an applicable table.

    Preserve grade evidence when defined. If normalization is unsupported, None
    explicitly records that the overshoot resolver does not consult elevation.
    """
    try:
        return map_profile.elevation_level_difference(shooter_hex, target_hex)
    except ValueError:
        if (fire_mode is FireMode.DIRECT
                and resource_policy is DirectFireResourcePolicy.AMMUNITION_KEY
                and distance > weapon.maximum_range):
            return None
        raise


@dataclass(frozen=True, slots=True)
class ActionLegalityResult:
    legal: bool
    reason_code: str
    evidence: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"legal": self.legal, "reason_code": self.reason_code,
                "evidence": dict(self.evidence)}
