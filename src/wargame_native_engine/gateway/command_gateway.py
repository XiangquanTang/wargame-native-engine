"""Identity-bound, idempotent local command orchestration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from threading import RLock

from wargame_native_engine.gateway.models import (
    CommandGatewayError,
    CommandResult,
    ControllerView,
    DecisionWindowView,
    SessionDescriptor,
    SessionPublication,
)
from wargame_native_engine.gateway.decision_window import (
    DecisionWindow,
    DecisionWindowState,
)
from wargame_native_engine.gateway.session_manager import SessionManager
from wargame_native_engine.session.scheduler import (
    SimulationAdvance,
    SimulationSchedule,
    SimulationScheduler,
)


@dataclass(slots=True)
class _GatewaySession:
    bindings: dict[str, str]
    window: DecisionWindow
    last_advance: SimulationAdvance | None = None
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
        decision_interval_ms: int | None = None,
        frame_interval_ms: int | None = None,
    ) -> None:
        if not isinstance(manager, SessionManager):
            raise TypeError("manager must be a SessionManager")
        if isinstance(delta_time_seconds, bool) or not isinstance(
            delta_time_seconds, (int, float)
        ) or delta_time_seconds <= 0:
            raise ValueError("delta_time_seconds must be a positive number")
        legacy_interval_ms = _seconds_to_milliseconds(delta_time_seconds)
        decision_ms = (
            legacy_interval_ms
            if decision_interval_ms is None
            else decision_interval_ms
        )
        frame_ms = decision_ms if frame_interval_ms is None else frame_interval_ms
        self.manager = manager
        self.schedule = SimulationSchedule(decision_ms, frame_ms)
        self.scheduler = SimulationScheduler(manager, self.schedule)
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
            self._sessions[descriptor.session_id] = _GatewaySession(
                {"red": red, "blue": blue},
                self._open_window(descriptor.session_id, 0),
            )
        return descriptor

    def view(self, session_id: str, controller_id: str) -> ControllerView:
        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            side = _bound_side(gateway_session, controller_id)
            return self._controller_view(session_id, side, gateway_session.window)

    def submit(
        self,
        session_id: str,
        controller_id: str,
        *,
        request_id: str,
        expected_step: int,
        valid_action_id: str | None,
    ) -> CommandResult:
        return self.submit_batch(
            session_id,
            controller_id,
            request_id=request_id,
            expected_window=expected_step,
            ordered_action_ids=(
                () if valid_action_id is None else (valid_action_id,)
            ),
        )

    def submit_batch(
        self,
        session_id: str,
        controller_id: str,
        *,
        request_id: str,
        expected_window: int,
        ordered_action_ids: tuple[str, ...] | list[str],
    ) -> CommandResult:
        if not isinstance(request_id, str) or not request_id.strip():
            raise CommandGatewayError(
                "invalid_request_id", "request_id must be a non-empty string"
            )
        request = request_id
        if type(expected_window) is not int or expected_window < 0:
            raise CommandGatewayError(
                "invalid_expected_step",
                "expected_window must be a non-negative exact int",
            )
        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            side = _bound_side(gateway_session, controller_id)
            action_ids = tuple(ordered_action_ids)
            signature = (
                str(controller_id), expected_window, action_ids,
            )
            cached = gateway_session.requests.get(request)
            if cached is not None:
                if cached[0] != signature:
                    raise CommandGatewayError(
                        "request_id_conflict",
                        f"request_id {request!r} was reused with another command",
                    )
                return deepcopy(cached[1])

            window = gateway_session.window
            if window.decision_window_index != expected_window:
                raise CommandGatewayError(
                    "stale_expected_step",
                    f"expected {expected_window}, current "
                    f"{window.decision_window_index}",
                )
            descriptor = self.manager.descriptor(session_id)
            if descriptor.terminal:
                raise CommandGatewayError(
                    "session_terminal", "terminal session cannot advance"
                )
            if descriptor.finalized:
                raise CommandGatewayError(
                    "session_finalized", "published session cannot advance"
                )
            window.submit(
                side=side,
                request_id=request,
                ordered_action_ids=action_ids,
            )
            pending = tuple(
                item.upper() for item in ("red", "blue")
                if item not in window.submissions
            )
            advanced = not pending
            if advanced:
                window.close()
                window.mark_advancing()
                gateway_session.last_advance = self.scheduler.advance_window(
                    session_id,
                    decision_window_index=window.decision_window_index,
                    ordered_actions=window.merged_action_batch,
                )
                window.mark_completed()
                descriptor = self.manager.descriptor(session_id)
                if not descriptor.terminal:
                    gateway_session.window = self._open_window(
                        session_id, window.decision_window_index + 1,
                    )
            active_window = gateway_session.window
            result = CommandResult(
                request_id=request,
                accepted=True,
                advanced=advanced,
                pending_perspectives=pending,
                view=self._controller_view(session_id, side, active_window),
                decision_window=self._window_view(active_window, side),
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
            if gateway_session.window.submissions and (
                gateway_session.window.lifecycle_state
                is DecisionWindowState.OPEN
            ):
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

    def window(
        self, session_id: str, controller_id: str,
    ) -> DecisionWindowView:
        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            side = _bound_side(gateway_session, controller_id)
            return self._window_view(gateway_session.window, side)

    def _open_window(
        self, session_id: str, decision_window_index: int,
    ) -> DecisionWindow:
        return DecisionWindow.open(
            session_id=session_id,
            decision_window_index=decision_window_index,
            opened_at_sim_time_ms=self.manager.sim_time_ms(session_id),
            red_valid_actions=self.manager.valid_actions(session_id, "red"),
            blue_valid_actions=self.manager.valid_actions(session_id, "blue"),
        )

    def _controller_view(
        self, session_id: str, side: str, window: DecisionWindow,
    ) -> ControllerView:
        view = self.manager.controller_view(session_id, side)
        return replace(
            view,
            step_index=window.decision_window_index,
            valid_actions=window.action_records(side),
        )

    @staticmethod
    def _window_view(
        window: DecisionWindow, side: str,
    ) -> DecisionWindowView:
        pending = tuple(
            value.upper() for value in ("red", "blue")
            if value not in window.submissions
        )
        return DecisionWindowView(
            session_id=window.session_id,
            decision_window_index=window.decision_window_index,
            opened_at_sim_time_ms=window.opened_at_sim_time_ms,
            closes_at_sim_time_ms=window.closes_at_sim_time_ms,
            deadline_policy=window.deadline_policy,
            lifecycle_state=window.lifecycle_state.value,
            close_reason=window.close_reason,
            submitted=side in window.submissions,
            pending_perspectives=pending,
            valid_actions=window.action_records(side),
        )


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


def _seconds_to_milliseconds(value: int | float) -> int:
    milliseconds = value * 1000
    if not isinstance(milliseconds, (int, float)) or not float(
        milliseconds
    ).is_integer():
        raise ValueError(
            "delta_time_seconds must resolve to positive integer milliseconds"
        )
    return int(milliseconds)
