"""Identity-bound, idempotent local command orchestration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from wargame_native_engine.core.action import EngineAction
from wargame_native_engine.gateway.models import (
    CommandGatewayError,
    CommandResult,
    ControllerView,
    SessionDescriptor,
    SessionPublication,
)
from wargame_native_engine.gateway.session_manager import SessionManager


@dataclass(slots=True)
class _GatewaySession:
    bindings: dict[str, str]
    pending: dict[str, EngineAction | None] = field(default_factory=dict)
    requests: dict[str, tuple[tuple[object, ...], CommandResult]] = field(
        default_factory=dict
    )
    lock: RLock = field(default_factory=RLock)


class CommandGateway:
    """The only local controller path into managed ``GameSession.step``."""

    def __init__(
        self,
        manager: SessionManager,
        *,
        delta_time_seconds: int | float = 20,
    ) -> None:
        if not isinstance(manager, SessionManager):
            raise TypeError("manager must be a SessionManager")
        if isinstance(delta_time_seconds, bool) or not isinstance(
            delta_time_seconds, (int, float)
        ) or delta_time_seconds <= 0:
            raise ValueError("delta_time_seconds must be a positive number")
        self.manager = manager
        self.delta_time_seconds = delta_time_seconds
        self._sessions: dict[str, _GatewaySession] = {}
        self._lock = RLock()

    def create_session(
        self,
        *,
        red_controller_id: str,
        blue_controller_id: str,
        seed: int = 42,
        record: bool = True,
    ) -> SessionDescriptor:
        red = _controller_id(red_controller_id, "red")
        blue = _controller_id(blue_controller_id, "blue")
        if red == blue:
            raise CommandGatewayError(
                "controller_binding_conflict",
                "RED and BLUE require distinct controller identities",
            )
        descriptor = self.manager.create_session(seed=seed, record=record)
        with self._lock:
            self._sessions[descriptor.session_id] = _GatewaySession({
                "red": red,
                "blue": blue,
            })
        return descriptor

    def view(self, session_id: str, controller_id: str) -> ControllerView:
        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            side = _bound_side(gateway_session, controller_id)
            return self.manager.controller_view(session_id, side)

    def submit(
        self,
        session_id: str,
        controller_id: str,
        *,
        request_id: str,
        expected_step: int,
        valid_action_id: str | None,
    ) -> CommandResult:
        if not isinstance(request_id, str) or not request_id.strip():
            raise CommandGatewayError(
                "invalid_request_id", "request_id must be a non-empty string"
            )
        request = request_id
        if type(expected_step) is not int or expected_step < 0:
            raise CommandGatewayError(
                "invalid_expected_step",
                "expected_step must be a non-negative exact int",
            )
        if valid_action_id is not None and (
            not isinstance(valid_action_id, str) or not valid_action_id.strip()
        ):
            raise CommandGatewayError(
                "invalid_action_id",
                "valid_action_id must be a non-empty string or null",
            )
        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            side = _bound_side(gateway_session, controller_id)
            signature = (
                str(controller_id), expected_step, valid_action_id,
            )
            cached = gateway_session.requests.get(request)
            if cached is not None:
                if cached[0] != signature:
                    raise CommandGatewayError(
                        "request_id_conflict",
                        f"request_id {request!r} was reused with another command",
                    )
                return deepcopy(cached[1])

            descriptor = self.manager.descriptor(session_id)
            if descriptor.step_index != expected_step:
                raise CommandGatewayError(
                    "stale_expected_step",
                    f"expected {expected_step}, current {descriptor.step_index}",
                )
            if descriptor.terminal:
                raise CommandGatewayError(
                    "session_terminal", "terminal session cannot advance"
                )
            if side in gateway_session.pending:
                raise CommandGatewayError(
                    "side_already_submitted",
                    f"{side.upper()} already submitted for step {expected_step}",
                )
            action = self._resolve_action(
                session_id,
                side,
                valid_action_id,
            )
            gateway_session.pending[side] = action
            pending = tuple(
                item.upper() for item in ("red", "blue")
                if item not in gateway_session.pending
            )
            advanced = not pending
            if advanced:
                actions = tuple(
                    value for item in ("red", "blue")
                    for value in (gateway_session.pending[item],)
                    if value is not None
                )
                self.manager.advance(
                    session_id,
                    actions,
                    delta_time_seconds=self.delta_time_seconds,
                )
                gateway_session.pending.clear()
            result = CommandResult(
                request_id=request,
                accepted=True,
                advanced=advanced,
                pending_perspectives=pending,
                view=self.manager.controller_view(session_id, side),
            )
            gateway_session.requests[request] = (signature, deepcopy(result))
            return deepcopy(result)

    def publish_session(
        self,
        session_id: str,
        controller_id: str,
        output_dir: str,
        *,
        truncated: bool = False,
        overwrite: bool = False,
    ) -> SessionPublication:
        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            _bound_side(gateway_session, controller_id)
            if gateway_session.pending:
                raise CommandGatewayError(
                    "pending_joint_action",
                    "cannot publish while one perspective is pending",
                )
            return self.manager.publish_session(
                session_id,
                output_dir,
                truncated=truncated,
                overwrite=overwrite,
            )

    def close_session(self, session_id: str) -> None:
        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            self.manager.close_session(session_id)
        with self._lock:
            self._sessions.pop(session_id, None)

    def _gateway_session(self, session_id: str) -> _GatewaySession:
        with self._lock:
            try:
                return self._sessions[str(session_id)]
            except KeyError as exc:
                raise CommandGatewayError(
                    "session_not_found", f"unknown session {session_id!r}"
                ) from exc

    def _resolve_action(
        self,
        session_id: str,
        side: str,
        action_id: str | None,
    ) -> EngineAction | None:
        if action_id is None:
            return None
        matches = tuple(
            value.action
            for value in self.manager.valid_actions(session_id, side)
            if value.action_id == action_id
        )
        if len(matches) != 1:
            raise CommandGatewayError(
                "action_not_currently_valid",
                f"action {action_id!r} is not a current {side.upper()} action",
            )
        action = matches[0]
        if action.side != side:
            raise CommandGatewayError(
                "action_side_mismatch",
                f"action {action_id!r} belongs to {action.side!r}",
            )
        return action


def _controller_id(value: object, side: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommandGatewayError(
            "invalid_controller_id",
            f"{side.upper()} controller_id must be a non-empty string",
        )
    return value


def _bound_side(session: _GatewaySession, controller_id: str) -> str:
    matches = tuple(
        side for side, identity in session.bindings.items()
        if identity == controller_id
    )
    if len(matches) != 1:
        raise CommandGatewayError(
            "controller_not_bound",
            f"controller {controller_id!r} is not bound to this session",
        )
    return matches[0]
