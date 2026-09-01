"""Side-safe command menus built only from a frozen controller view."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from wargame_native_engine.gateway.models import (
    CommandGatewayError,
    ControllerView,
)
from wargame_native_engine.io.serialization import json_clone


COMMAND_ORDER = (
    "MOVE",
    "SHOOT",
    "BOARD",
    "UNLOAD",
    "ENTER_CONCEALMENT",
    "STOP_MOVE",
    "WEAPON_LOCK",
    "WEAPON_UNFOLD",
    "WAIT",
)


@dataclass(frozen=True, slots=True)
class UnitCommandMenu:
    session_id: str
    decision_window_index: int
    perspective: str
    unit_id: int
    unit: Mapping[str, Any]
    available: Mapping[str, tuple[Mapping[str, Any], ...]]
    unavailable: Mapping[str, str]
    move_planning_available: bool
    staged_action_id: str | None = None

    def to_record(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "decision_window_index": self.decision_window_index,
            "perspective": self.perspective,
            "unit_id": self.unit_id,
            "unit": json_clone(self.unit),
            "available": {
                key: json_clone(values)
                for key, values in self.available.items()
            },
            "unavailable": dict(self.unavailable),
            "move_planning_available": self.move_planning_available,
            "staged_action_id": self.staged_action_id,
        }


def unit_command_menu(
    view: ControllerView,
    unit_id: int,
    *,
    staged_action_id: str | None = None,
) -> UnitCommandMenu:
    """Describe one friendly unit without inventing any rule decision."""

    if type(unit_id) is not int or unit_id < 0:
        raise CommandGatewayError(
            "invalid_unit_id", "unit_id must be a non-negative exact int"
        )
    friendlies = view.observation.get("friendly_operators")
    if not isinstance(friendlies, list):
        raise RuntimeError("side observation lacks friendly_operators")
    unit = next(
        (
            item
            for item in friendlies
            if isinstance(item, Mapping) and item.get("operator_id") == unit_id
        ),
        None,
    )
    if unit is None:
        raise CommandGatewayError(
            "unit_not_controllable",
            f"unit {unit_id} is not a friendly unit in {view.perspective}",
        )
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for raw in view.valid_actions:
        if not isinstance(raw, Mapping):
            continue
        action = raw.get("action")
        if not isinstance(action, Mapping) or action.get("actor_id") != unit_id:
            continue
        kind = str(action.get("action_type", "UNKNOWN")).upper()
        grouped.setdefault(kind, []).append(json_clone(raw))
    available = {
        key: tuple(grouped[key])
        for key in (*COMMAND_ORDER, *sorted(set(grouped) - set(COMMAND_ORDER)))
        if key in grouped
    }
    unavailable = {
        key: "not_in_current_frozen_valid_actions"
        for key in COMMAND_ORDER
        if key not in available
    }
    return UnitCommandMenu(
        session_id=view.session_id,
        decision_window_index=view.step_index,
        perspective=view.perspective,
        unit_id=unit_id,
        unit=json_clone(unit),
        available=available,
        unavailable=unavailable,
        move_planning_available="MOVE" in available,
        staged_action_id=staged_action_id,
    )
