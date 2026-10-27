"""Application-level side controller over the existing CommandGateway."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Mapping
from copy import deepcopy

from wargame_native_engine.gateway.command_gateway import CommandGateway
from wargame_native_engine.gateway.models import (
    CommandGatewayError,
    CommandResult,
    ControllerView,
    DecisionWindowView,
)
from wargame_native_engine.operations.commands import (
    _FrozenActionRecords,
    UnitCommandMenu,
    unit_command_menu,
)
from wargame_native_engine.operations.proposals import ActionProposal


@dataclass(frozen=True, slots=True)
class StagedAction:
    actor_id: int | None
    action_id: str
    action_type: str

    def to_record(self) -> dict[str, object]:
        return {
            "actor_id": self.actor_id,
            "action_id": self.action_id,
            "action_type": self.action_type,
        }


class SideController:
    """Simple Python API: observe, inspect, propose, stage and submit."""

    def __init__(
        self,
        gateway: CommandGateway,
        session_id: str,
        controller_id: str,
    ) -> None:
        self._gateway = gateway
        self.session_id = str(session_id)
        self.controller_id = str(controller_id)
        self._staged: dict[int | None, StagedAction] = {}
        self._staged_window: int | None = None
        self._stage_requests: dict[
            str, tuple[tuple[int, str], tuple[StagedAction, ...]]
        ] = {}
        self._lock = RLock()

    def observe(self) -> ControllerView:
        _, view = self.state()
        return view

    def state(self) -> tuple[DecisionWindowView, ControllerView]:
        """Return one detached, mutually consistent service snapshot."""

        window, view = self._gateway.controller_state(
            self.session_id, self.controller_id
        )
        with self._lock:
            self._sync_window(window.decision_window_index)
        return window, view

    def window(self) -> DecisionWindowView:
        value = self._gateway.poll_window(self.session_id, self.controller_id)
        with self._lock:
            self._sync_window(value.decision_window_index)
        return value

    def decision_core(self):
        """Return the detached machine semantic and catalog for Human/Agent use."""

        view = self.observe()
        if view.decision_semantic is None or view.action_catalog is None:
            raise CommandGatewayError(
                "decision_core_unavailable", "legacy ControllerView has no core"
            )
        return deepcopy(view.decision_semantic), deepcopy(view.action_catalog)

    def get_unit_commands(
        self,
        unit_id: int,
        *,
        view: ControllerView | None = None,
        action_records: _FrozenActionRecords | None = None,
    ) -> UnitCommandMenu:
        value = self.observe() if view is None else view
        staged = self._staged.get(unit_id)
        return unit_command_menu(
            value,
            unit_id,
            staged_action_id=None if staged is None else staged.action_id,
            action_records=action_records,
        )

    def plan_move(
        self,
        unit_id: int,
        destination_hex: int,
        *,
        request_id: str,
        movement_mode: str = "NORMAL",
        expected_window: int | None = None,
    ) -> ActionProposal:
        window = self.window()
        if (
            expected_window is not None
            and expected_window != window.decision_window_index
        ):
            raise CommandGatewayError(
                "stale_expected_step",
                f"expected {expected_window}, current "
                f"{window.decision_window_index}",
            )
        return self._gateway.plan_move(
            self.session_id,
            self.controller_id,
            request_id=request_id,
            expected_window=window.decision_window_index,
            unit_id=unit_id,
            destination_hex=destination_hex,
            movement_mode=movement_mode,
        )

    def plan_indirect_fire(
        self,
        shooter_id: int,
        weapon_id: int,
        target_hex: int,
        *,
        request_id: str,
        target_id: int | None = None,
        expected_window: int | None = None,
    ) -> ActionProposal:
        window = self.window()
        if (
            expected_window is not None
            and expected_window != window.decision_window_index
        ):
            raise CommandGatewayError(
                "stale_expected_step",
                f"expected {expected_window}, current "
                f"{window.decision_window_index}",
            )
        return self._gateway.plan_indirect_fire(
            self.session_id,
            self.controller_id,
            request_id=request_id,
            expected_window=window.decision_window_index,
            shooter_id=shooter_id,
            weapon_id=weapon_id,
            target_hex=target_hex,
            target_id=target_id,
        )

    def plan_direct_fire_intent(
        self,
        unit_id: int,
        weapon_id: int,
        target_id: int | None = None,
        *,
        request_id: str,
        target_hex: int | None = None,
        expected_window: int | None = None,
    ) -> ActionProposal:
        window = self.window()
        if (
            expected_window is not None
            and expected_window != window.decision_window_index
        ):
            raise CommandGatewayError(
                "stale_expected_step",
                f"expected {expected_window}, current "
                f"{window.decision_window_index}",
            )
        return self._gateway.plan_direct_fire_intent(
            self.session_id,
            self.controller_id,
            request_id=request_id,
            expected_window=window.decision_window_index,
            actor_id=unit_id,
            weapon_id=weapon_id,
            target_id=target_id,
            target_hex=target_hex,
        )

    def proposal(self, proposal_id: str) -> ActionProposal:
        return self._gateway.proposal(
            self.session_id, self.controller_id, proposal_id
        )

    def record_operation_preview(
        self,
        *,
        preview_token: str,
        operation_type: str,
        selected_unit_ids: tuple[int, ...],
        executable_count: int,
        rejected: tuple[Mapping[str, object], ...],
    ) -> None:
        self._gateway.record_product_preview(
            self.session_id,
            self.controller_id,
            preview_token=preview_token,
            operation_type=operation_type,
            selected_unit_ids=selected_unit_ids,
            executable_count=executable_count,
            rejected=rejected,
        )

    def stage_action(
        self,
        action_id: str,
        *,
        request_id: str | None = None,
        expected_window: int | None = None,
    ) -> tuple[StagedAction, ...]:
        window, view = self._gateway.controller_state(
            self.session_id, self.controller_id
        )
        with self._lock:
            index = window.decision_window_index
            self._sync_window(index)
            if expected_window is not None and expected_window != index:
                raise CommandGatewayError(
                    "stale_expected_step",
                    f"expected {expected_window}, current {index}",
                )
            signature = (index, str(action_id))
            if request_id is not None:
                cached = self._stage_requests.get(str(request_id))
                if cached is not None:
                    if cached[0] != signature:
                        raise CommandGatewayError(
                            "request_id_conflict",
                            "stage request_id was reused with another action",
                        )
                    return cached[1]
            catalog = view.action_catalog
            if catalog is None:
                raise CommandGatewayError(
                    "decision_core_unavailable",
                    "current ControllerView has no action catalog",
                )
            entry = next(
                (item for item in catalog.entries if item.action_id == action_id),
                None,
            )
            if entry is None:
                authorized = self._gateway.authorized_action(
                    self.session_id, self.controller_id, action_id,
                )
                if authorized is None:
                    raise CommandGatewayError(
                        "action_not_currently_valid",
                        f"action {action_id!r} is not authorized in this window",
                    )
                staged = StagedAction(
                    authorized.actor_id,
                    str(action_id),
                    authorized.action_type.value,
                )
            else:
                staged = StagedAction(
                    entry.actor_reference,
                    str(action_id),
                    entry.action_type,
                )
            self._staged[staged.actor_id] = staged
            result = tuple(self._staged.values())
            if request_id is not None:
                self._stage_requests[str(request_id)] = (signature, result)
            return result

    def unstage(self, unit_id: int | None) -> tuple[StagedAction, ...]:
        with self._lock:
            self._staged.pop(unit_id, None)
            return tuple(self._staged.values())

    def clear_staged(self) -> None:
        with self._lock:
            self._staged.clear()

    @property
    def staged_actions(self) -> tuple[StagedAction, ...]:
        with self._lock:
            return tuple(self._staged.values())

    def submit_batch(
        self,
        *,
        request_id: str,
        expected_window: int | None = None,
    ) -> CommandResult:
        window = self.window()
        index = window.decision_window_index
        if expected_window is not None and expected_window != index:
            raise CommandGatewayError(
                "stale_expected_step",
                f"expected {expected_window}, current {index}",
            )
        with self._lock:
            action_ids = [item.action_id for item in self._staged.values()]
        result = self._gateway.submit_batch(
            self.session_id,
            self.controller_id,
            request_id=request_id,
            expected_window=index,
            ordered_action_ids=action_ids,
        )
        with self._lock:
            self._staged.clear()
            if result.decision_window is not None:
                self._staged_window = result.decision_window.decision_window_index
        return result

    def _sync_window(self, value: int) -> None:
        if self._staged_window is None:
            self._staged_window = value
            return
        if value != self._staged_window:
            self._staged.clear()
            self._stage_requests.clear()
            self._staged_window = value
