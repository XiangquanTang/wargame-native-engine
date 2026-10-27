"""Side-safe command menus built only from a frozen controller view."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from wargame_native_engine.gateway.models import (
    CommandGatewayError,
    ControllerView,
)
from wargame_native_engine.io.serialization import json_clone
from wargame_native_engine.projection.decision_capability import (
    ActionCatalogRecordAdapter,
)


COMMAND_ORDER = (
    "MOVE",
    "SHOOT",
    "BOARD",
    "UNLOAD",
    "CAPTURE",
    "ENTER_CONCEALMENT",
    "STOP_MOVE",
    "WEAPON_LOCK",
    "WEAPON_UNLOCK",
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


@dataclass(frozen=True, slots=True)
class _FrozenActionRecords:
    """One adapter bound to the exact detached view that produced it."""

    _owner: ControllerView
    _adapter: ActionCatalogRecordAdapter

    def require_owner(self, view: ControllerView) -> None:
        if view is not self._owner:
            raise CommandGatewayError(
                "action_records_view_mismatch",
                "frozen action records belong to a different controller view",
            )

    def query_records(self, **filters: object) -> tuple[Mapping[str, object], ...]:
        return self._adapter.query_records(**filters)


def unit_command_menu(
    view: ControllerView,
    unit_id: int,
    *,
    staged_action_id: str | None = None,
    action_records: _FrozenActionRecords | None = None,
) -> UnitCommandMenu:
    """Describe one friendly unit without inventing any rule decision."""

    if type(unit_id) is not int or unit_id < 0:
        raise CommandGatewayError(
            "invalid_unit_id", "unit_id must be a non-negative exact int"
        )
    if action_records is None:
        records = frozen_action_records(view)
    elif type(action_records) is not _FrozenActionRecords:
        raise TypeError("action_records must be created by frozen_action_records")
    else:
        action_records.require_owner(view)
        records = action_records
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
    for raw in records.query_records(actor_reference=unit_id):
        action = raw.get("action")
        if not isinstance(action, Mapping):  # adapter validates normal catalogs
            continue
        kind = str(action.get("action_type", "UNKNOWN")).upper()
        grouped.setdefault(kind, []).append(raw)
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


def frozen_action_records(view: ControllerView) -> _FrozenActionRecords:
    """Build one view-local catalog/legacy adapter without cross-view caching."""

    return _FrozenActionRecords(
        view,
        ActionCatalogRecordAdapter.from_records(
            view.action_catalog,
            view.valid_actions,
        ),
    )
