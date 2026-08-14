"""Application-level side controller over the existing CommandGateway."""

from __future__ import annotations

from dataclasses import dataclass

from wargame_native_engine.gateway.command_gateway import CommandGateway
from wargame_native_engine.gateway.models import (
    CommandGatewayError,
    CommandResult,
    ControllerView,
    DecisionWindowView,
)
from wargame_native_engine.operations.commands import (
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

    def observe(self) -> ControllerView:
        window, view = self._gateway.controller_state(
            self.session_id, self.controller_id
        )
        self._sync_window(window.decision_window_index)
        return view

    def window(self) -> DecisionWindowView:
        value = self._gateway.poll_window(self.session_id, self.controller_id)
        self._sync_window(value.decision_window_index)
        return value

    def get_unit_commands(self, unit_id: int) -> UnitCommandMenu:
        view = self.observe()
        staged = self._staged.get(unit_id)
        return unit_command_menu(
            view,
            unit_id,
            staged_action_id=None if staged is None else staged.action_id,
        )

    def plan_move(
        self,
        unit_id: int,
        destination_hex: int,
        *,
        request_id: str,
        movement_mode: str = "NORMAL",
    ) -> ActionProposal:
        window = self.window()
        return self._gateway.plan_move(
            self.session_id,
            self.controller_id,
            request_id=request_id,
            expected_window=window.decision_window_index,
            unit_id=unit_id,
            destination_hex=destination_hex,
            movement_mode=movement_mode,
        )

    def proposal(self, proposal_id: str) -> ActionProposal:
        return self._gateway.proposal(
            self.session_id, self.controller_id, proposal_id
        )

    def stage_action(self, action_id: str) -> tuple[StagedAction, ...]:
        window, view = self._gateway.controller_state(
            self.session_id, self.controller_id
        )
        self._sync_window(window.decision_window_index)
        raw = next(
            (
                item
                for item in view.valid_actions
                if item.get("action_id") == action_id
            ),
            None,
        )
        if raw is None:
            raise CommandGatewayError(
                "action_not_currently_valid",
                f"action {action_id!r} is not in the current side snapshot",
            )
        action = raw.get("action")
        if not isinstance(action, dict):
            raise RuntimeError("valid action record lacks action")
        actor = action.get("actor_id")
        if actor is not None and type(actor) is not int:
            raise RuntimeError("valid action actor_id is not an exact int")
        staged = StagedAction(
            actor,
            str(action_id),
            str(action.get("action_type", "UNKNOWN")),
        )
        self._staged[actor] = staged
        return self.staged_actions

    def unstage(self, unit_id: int | None) -> tuple[StagedAction, ...]:
        self._staged.pop(unit_id, None)
        return self.staged_actions

    def clear_staged(self) -> None:
        self._staged.clear()

    @property
    def staged_actions(self) -> tuple[StagedAction, ...]:
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
        result = self._gateway.submit_batch(
            self.session_id,
            self.controller_id,
            request_id=request_id,
            expected_window=index,
            ordered_action_ids=[item.action_id for item in self._staged.values()],
        )
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
            self._staged_window = value
