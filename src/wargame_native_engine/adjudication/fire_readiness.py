"""Shared weapon-posture rules for targeted and target-free direct shots."""
from __future__ import annotations


def weapon_posture_failure(
    *, moving: bool, locked: bool, unfolded: bool,
    requires_unfolded: bool, can_fire_while_moving: bool,
    ordinary_direct: bool,
) -> str | None:
    """Validate posture without changing movement or resource state.

    A completed concrete lock is an alternative ready posture for ordinary
    direct shots, both stationary and moving. Binding and transition checks
    remain independent, so a boolean-only lock cannot bypass weapon identity.
    Guided/indirect modes retain their established deployment requirements.
    """
    if ordinary_direct:
        if locked:
            return None
        if moving:
            return "WEAPON_NOT_LOCKED" if can_fire_while_moving else "ATTACKER_MUST_STOP"
    if requires_unfolded and not unfolded:
        return "WEAPON_NOT_UNFOLDED"
    if moving and not can_fire_while_moving:
        return "ATTACKER_MUST_STOP"
    return None


def weapon_transition_failure(
    *, transitioning: bool, transition_kind: str | None,
    ordinary_direct: bool, locked: bool, locked_weapon_id: int | None,
    weapon_id: int | str,
) -> str | None:
    """A completed matching lock survives movement/posture transitions.

    Starting LOCK reserves an ID but is not a ready lock. Also fail closed for
    inconsistent snapshots claiming a lock during LOCK/UNFOLD. This exemption
    does not alter guided/indirect readiness, resources, or aircraft hovering.
    """
    if not transitioning:
        return None
    completed_lock = (
        locked and type(locked_weapon_id) is int and locked_weapon_id >= 0
        and locked_weapon_id == weapon_id
        and transition_kind not in {"WEAPON_LOCK", "WEAPON_UNFOLD"}
    )
    if ordinary_direct and completed_lock:
        return None
    return "ATTACKER_TRANSITIONING"


def weapon_binding_failure(*, weapon_id: int, locked: bool,
                           locked_weapon_id: int | None) -> str | None:
    """Legacy boolean-only locks fail closed; never infer a carried weapon."""
    if locked and locked_weapon_id is None:
        return "LOCKED_WEAPON_ID_REQUIRED"
    if locked_weapon_id is not None:
        if type(locked_weapon_id) is not int or locked_weapon_id < 0:
            return "INVALID_LOCKED_WEAPON_ID"
        if weapon_id != locked_weapon_id:
            return "WEAPON_LOCKED_TO_OTHER_WEAPON"
    return None
