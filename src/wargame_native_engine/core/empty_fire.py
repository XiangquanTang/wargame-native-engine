"""Target-free game shots and their finite, replayable visibility signal.

An empty shot never resolves occupants of its selected cell. Its only effects
are resource consumption and a ten-result-frame location signal. Frame count
is deliberately independent of the simulation's elapsed milliseconds.
"""
from dataclasses import dataclass

from wargame_native_engine.adjudication.request import WeaponProfile
from wargame_native_engine.adjudication.fire_readiness import weapon_posture_failure, weapon_binding_failure
from wargame_native_engine.core.action_router import RoutedAction
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import WorldState

EMPTY_FIRE_EXPOSURE_FRAMES = 10
_EXPOSURE_KEY = "empty_fire_exposure_frames_remaining"


@dataclass(frozen=True, slots=True)
class EmptyFireIntent:
    routed: RoutedAction
    target_hex: int
    weapon: WeaponProfile
    ammunition_before: int
    cooldown_ms: int


def mark_empty_fire_exposure(operator: OperatorState) -> None:
    """Refresh the temporary signal without changing concealment posture."""
    operator.raw_attributes[_EXPOSURE_KEY] = EMPTY_FIRE_EXPOSURE_FRAMES


def has_empty_fire_exposure(operator: OperatorState) -> bool:
    remaining = operator.raw_attributes.get(_EXPOSURE_KEY, 0)
    return type(remaining) is int and 0 < remaining <= EMPTY_FIRE_EXPOSURE_FRAMES


def advance_empty_fire_exposure(world: WorldState) -> None:
    """Age signals once before applying each committed source step."""
    for operator in world.operators.values():
        remaining = operator.raw_attributes.get(_EXPOSURE_KEY, 0)
        if type(remaining) is int and 1 < remaining <= EMPTY_FIRE_EXPOSURE_FRAMES:
            operator.raw_attributes[_EXPOSURE_KEY] = remaining - 1
        else:
            operator.raw_attributes.pop(_EXPOSURE_KEY, None)


def empty_fire_readiness_error(
    attacker: OperatorState, weapon: WeaponProfile, ammunition: int,
) -> str | None:
    """Apply shooter/resource checks; target-domain and visibility do not apply."""
    if not weapon.target_domains:
        return "EMPTY_FIRE_DIRECT_MODE_UNSUPPORTED"
    if weapon.weapon_id not in attacker.weapon_ids:
        return "WEAPON_NOT_CARRIED"
    if weapon.ammunition_key is None or ammunition <= 0:
        return "AMMUNITION_UNAVAILABLE"
    binding_failure = weapon_binding_failure(weapon_id=weapon.weapon_id,
        locked=attacker.weapon_locked, locked_weapon_id=attacker.locked_weapon_id)
    if binding_failure is not None:
        return binding_failure
    moving = not attacker.stopped or attacker.movement_mode == "MARCHING"
    posture_failure = weapon_posture_failure(
        moving=moving, locked=attacker.weapon_locked,
        unfolded=attacker.weapon_unfolded,
        requires_unfolded=weapon.requires_unfolded,
        can_fire_while_moving=weapon.can_fire_while_moving,
        ordinary_direct=True,
    )
    if posture_failure is not None:
        return posture_failure
    if attacker.weapon_cooldown_remaining_ms > 0:
        return "WEAPON_ON_COOLDOWN"
    if attacker.transition_kind is not None or attacker.move_to_stop_remaining_ms > 0:
        return "ATTACKER_TRANSITIONING"
    if attacker.operator_type == "AIR" and not attacker.stopped:
        return "AIR_UNIT_MUST_HOVER"
    return None
