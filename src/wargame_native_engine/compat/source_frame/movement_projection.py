"""Verified Source MoveType projection, separate from Native movement rules."""
from __future__ import annotations

from wargame_native_engine.core.operator import OperatorState


def source_move_state(operator: OperatorState) -> int | None:
    """Original MoveType: Maneuver=0, March=1, Walk=2, Fly=3.

    Source move_state is not a boolean moving/stopped flag. Charge modes and
    unknown domains have no verified numeric equivalent; the Native mode is
    retained separately. This does not infer target_state's action enum.
    """
    domain = operator.operator_type.upper()
    if operator.movement_mode == "MARCHING":
        return 1 if domain == "VEHICLE" else None
    if operator.movement_mode != "NORMAL":
        return None
    return {"VEHICLE": 0, "PERSONNEL": 2, "AIR": 3}.get(domain)
