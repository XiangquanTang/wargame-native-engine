"""Identity-bound, idempotent local command orchestration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from threading import RLock

from wargame_native_engine.core.action import (
    ACTION_PRIORITY,
    ActionType,
    EngineAction,
    ValidAction,
)
from wargame_native_engine.gateway.control_audit import ControlAuditLog
from wargame_native_engine.gateway.deadline import (
    DeadlineMode,
    MonotonicClock,
    SystemMonotonicClock,
    require_deadline_seconds,
)
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
from wargame_native_engine.movement.models import MovementMode
from wargame_native_engine.operations.proposals import (
    ActionProposal,
    ActionProposalRegistry,
    OperationError,
)
from wargame_native_engine.session.scheduler import (
    SimulationAdvance,
    SimulationSchedule,
    SimulationScheduler,
)


@dataclass(slots=True)
class _GatewaySession:
    bindings: dict[str, str]
    window: DecisionWindow
    audit: ControlAuditLog
    last_advance: SimulationAdvance | None = None
    requests: dict[str, tuple[tuple[object, ...], CommandResult]] = field(
        default_factory=dict
    )
    lock: RLock = field(default_factory=RLock)
    proposals: ActionProposalRegistry | None = None


class CommandGateway:
    """The only local controller path into managed ``GameSession.step``."""

    def __init__(
        self,
        manager: SessionManager,
        *,
        delta_time_seconds: int | float = 20,
        decision_interval_ms: int | None = None,
        frame_interval_ms: int | None = None,
        deadline_mode: DeadlineMode | str = DeadlineMode.SYNCHRONOUS,
        deadline_seconds: int | float | None = None,
        clock: MonotonicClock | None = None,
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
        try:
            self.deadline_mode = DeadlineMode(deadline_mode)
        except ValueError as exc:
            raise ValueError(f"unsupported deadline_mode: {deadline_mode!r}") from exc
        if self.deadline_mode is DeadlineMode.SYNCHRONOUS:
            if deadline_seconds is not None:
                raise ValueError(
                    "deadline_seconds is only valid for server_monotonic mode"
                )
            self.deadline_seconds = None
        else:
            self.deadline_seconds = require_deadline_seconds(deadline_seconds)
        self.clock = clock or SystemMonotonicClock()
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
        window = self._open_window(descriptor.session_id, 0)
        audit = ControlAuditLog(descriptor.session_id)
        _record_window_open(audit, window)
        with self._lock:
            gateway_session = _GatewaySession(
                {"red": red, "blue": blue},
                window,
                audit,
            )
            gateway_session.proposals = ActionProposalRegistry(
                descriptor.session_id, window.decision_window_index
            )
            self._sessions[descriptor.session_id] = gateway_session
        return descriptor

    def plan_move(
        self,
        session_id: str,
        controller_id: str,
        *,
        request_id: str,
        expected_window: int,
        unit_id: int,
        destination_hex: int,
        movement_mode: str = "NORMAL",
    ) -> ActionProposal:
        """Create one immutable destination proposal in the active window."""

        if not isinstance(request_id, str) or not request_id.strip():
            raise CommandGatewayError(
                "invalid_request_id", "request_id must be a non-empty string"
            )
        if type(expected_window) is not int or expected_window < 0:
            raise CommandGatewayError(
                "invalid_expected_step", "expected_window must be non-negative"
            )
        if type(unit_id) is not int or unit_id < 0:
            raise CommandGatewayError("invalid_unit_id", "unit_id must be non-negative")
        if type(destination_hex) is not int or destination_hex < 0:
            raise CommandGatewayError(
                "invalid_destination", "destination_hex must be non-negative"
            )
        try:
            mode = MovementMode(str(movement_mode).upper())
        except ValueError as exc:
            raise CommandGatewayError(
                "invalid_movement_mode", str(movement_mode)
            ) from exc
        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            side = _bound_side(gateway_session, controller_id)
            window = gateway_session.window
            if window.decision_window_index != expected_window:
                raise CommandGatewayError(
                    "stale_expected_step",
                    f"expected {expected_window}, current {window.decision_window_index}",
                )
            if window.lifecycle_state is not DecisionWindowState.OPEN:
                raise CommandGatewayError(
                    "decision_window_closed", window.lifecycle_state.value
                )
            signature = (
                str(controller_id), expected_window, unit_id,
                destination_hex, mode.value,
            )
            registry = gateway_session.proposals
            if registry is None:
                raise RuntimeError("active window lacks proposal registry")
            try:
                cached = registry.cached(request_id, signature)
            except OperationError as exc:
                raise CommandGatewayError(exc.code, exc.message) from exc
            if cached is not None:
                return cached
            try:
                route, state_hash = self.manager.plan_move(
                    session_id, side, unit_id, destination_hex, mode
                )
            except (TypeError, ValueError) as exc:
                raise CommandGatewayError(
                    "move_plan_rejected", str(exc)
                ) from exc
            action = None
            valid = None
            if route.reachable:
                action = EngineAction(
                    side,
                    unit_id,
                    ActionType.MOVE,
                    parameters={
                        "path": list(route.ordered_path),
                        "movement_mode": mode.value,
                    },
                )
                valid = ValidAction(
                    action,
                    ACTION_PRIORITY[ActionType.MOVE],
                    "operation_move_proposal",
                )
            proposal = ActionProposal.from_route(
                session_id=session_id,
                decision_window_index=expected_window,
                side=side,
                route=route,
                state_hash=state_hash,
                action=action,
            )
            try:
                registered = registry.register(
                    request_id=request_id,
                    signature=signature,
                    proposal=proposal,
                    valid_action=valid,
                )
            except OperationError as exc:
                raise CommandGatewayError(exc.code, exc.message) from exc
            if valid is not None:
                window.authorize_proposal(side, valid)
            return registered

    def proposal(
        self,
        session_id: str,
        controller_id: str,
        proposal_id: str,
    ) -> ActionProposal:
        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            side = _bound_side(gateway_session, controller_id)
            registry = gateway_session.proposals
            if registry is None:
                raise CommandGatewayError(
                    "proposal_expired", "active window has no matching proposal"
                )
            try:
                return registry.get(proposal_id, side=side)
            except OperationError as exc:
                raise CommandGatewayError(exc.code, exc.message) from exc

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
            received_at = self._server_now()
            signature = (
                str(controller_id), expected_window, action_ids,
            )
            cached = gateway_session.requests.get(request)
            if cached is not None:
                if cached[0] != signature:
                    gateway_session.audit.append(
                        "submission_rejected",
                        decision_window_index=expected_window,
                        controller_id=str(controller_id),
                        side=side,
                        request_id=request,
                        expected_window=expected_window,
                        ordered_action_ids=action_ids,
                        server_received_at=received_at,
                        accepted=False,
                        reject_reason="request_id_conflict",
                        timeout=False,
                        retry=True,
                    )
                    raise CommandGatewayError(
                        "request_id_conflict",
                        f"request_id {request!r} was reused with another command",
                    )
                gateway_session.audit.append(
                    "submission_retry",
                    decision_window_index=expected_window,
                    controller_id=str(controller_id),
                    side=side,
                    request_id=request,
                    expected_window=expected_window,
                    ordered_action_ids=action_ids,
                    server_received_at=received_at,
                    accepted=True,
                    reject_reason=None,
                    timeout=False,
                    retry=True,
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
            if self._deadline_reached(window, received_at):
                self._expire_and_advance(gateway_session)
                gateway_session.audit.append(
                    "submission_rejected",
                    decision_window_index=expected_window,
                    controller_id=str(controller_id),
                    side=side,
                    request_id=request,
                    expected_window=expected_window,
                    ordered_action_ids=action_ids,
                    server_received_at=received_at,
                    accepted=False,
                    reject_reason="decision_window_expired",
                    timeout=True,
                    retry=False,
                )
                raise CommandGatewayError(
                    "decision_window_expired",
                    f"window {expected_window} deadline has elapsed",
                )
            window.submit(
                side=side,
                request_id=request,
                ordered_action_ids=action_ids,
            )
            gateway_session.audit.append(
                "submission_accepted",
                decision_window_index=expected_window,
                controller_id=str(controller_id),
                side=side,
                request_id=request,
                expected_window=expected_window,
                ordered_action_ids=action_ids,
                server_received_at=received_at,
                accepted=True,
                reject_reason=None,
                timeout=False,
                retry=False,
            )
            pending = tuple(
                item.upper() for item in ("red", "blue")
                if item not in window.submissions
            )
            advanced = not pending
            if advanced:
                window.close()
                self._advance_closed_window(gateway_session)
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
            gateway_session.audit.append(
                "publication_requested",
                decision_window_index=(
                    gateway_session.window.decision_window_index
                ),
                controller_id=str(controller_id),
                side=_bound_side(gateway_session, controller_id),
                request_id=None,
                expected_window=None,
                ordered_action_ids=[],
                server_received_at=self._server_now(),
                accepted=True,
                reject_reason=None,
                timeout=False,
                retry=False,
            )
            return self.manager.publish_session(
                session_id,
                output_dir,
                truncated=truncated,
                overwrite=overwrite,
                control_audit_payload=gateway_session.audit.jsonl_bytes(),
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

    def poll_window(
        self, session_id: str, controller_id: str,
    ) -> DecisionWindowView:
        """Return current status, closing a reached wall deadline if needed."""

        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            side = _bound_side(gateway_session, controller_id)
            self._poll_deadline_locked(gateway_session)
            return self._window_view(gateway_session.window, side)

    def controller_state(
        self, session_id: str, controller_id: str,
    ) -> tuple[DecisionWindowView, ControllerView]:
        """Atomically pair one side view with its current window snapshot."""

        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            side = _bound_side(gateway_session, controller_id)
            self._poll_deadline_locked(gateway_session)
            window = gateway_session.window
            return (
                self._window_view(window, side),
                self._controller_view(session_id, side, window),
            )

    def request_result(
        self,
        session_id: str,
        controller_id: str,
        request_id: str,
    ) -> CommandResult | None:
        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            _bound_side(gateway_session, controller_id)
            cached = gateway_session.requests.get(str(request_id))
            if cached is None:
                return None
            if cached[0][0] != str(controller_id):
                raise CommandGatewayError(
                    "request_not_owned", "request belongs to another controller"
                )
            return deepcopy(cached[1])

    def control_audit(
        self, session_id: str, controller_id: str,
    ) -> tuple[object, ...]:
        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            _bound_side(gateway_session, controller_id)
            return tuple(gateway_session.audit.records)

    def last_advance(
        self, session_id: str, controller_id: str,
    ) -> SimulationAdvance | None:
        """Return detached timing evidence for the last completed window."""

        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            _bound_side(gateway_session, controller_id)
            return deepcopy(gateway_session.last_advance)

    def expire_window(self, session_id: str) -> SimulationAdvance:
        """Close a wall-deadline window and substitute empty missing batches."""

        gateway_session = self._gateway_session(session_id)
        with gateway_session.lock:
            if self.deadline_mode is not DeadlineMode.SERVER_MONOTONIC:
                raise CommandGatewayError(
                    "deadline_not_enabled",
                    "synchronous windows require explicit side submissions",
                )
            now = self._server_now()
            if not self._deadline_reached(gateway_session.window, now):
                raise CommandGatewayError(
                    "deadline_not_reached", "decision window is still open"
                )
            return self._expire_and_advance(gateway_session)

    def _open_window(
        self, session_id: str, decision_window_index: int,
    ) -> DecisionWindow:
        opened_at = self._server_now()
        deadline_at = (
            None
            if opened_at is None
            else opened_at + float(self.deadline_seconds)
        )
        return DecisionWindow.open(
            session_id=session_id,
            decision_window_index=decision_window_index,
            opened_at_sim_time_ms=self.manager.sim_time_ms(session_id),
            red_valid_actions=self.manager.valid_actions(session_id, "red"),
            blue_valid_actions=self.manager.valid_actions(session_id, "blue"),
            deadline_policy=self.deadline_mode.value,
            opened_at_server_monotonic=opened_at,
            deadline_at_server_monotonic=deadline_at,
        )

    def _advance_closed_window(
        self, gateway_session: _GatewaySession,
    ) -> SimulationAdvance:
        window = gateway_session.window
        gateway_session.audit.append(
            "window_closed",
            decision_window_index=window.decision_window_index,
            controller_id=None,
            side=None,
            request_id=None,
            expected_window=window.decision_window_index,
            ordered_action_ids={
                side: list(submission.ordered_action_ids)
                for side, submission in sorted(window.submissions.items())
            },
            merged_action_ids=[
                action.action_id for action in window.merged_action_batch
            ],
            server_received_at=self._server_now(),
            accepted=True,
            reject_reason=None,
            timeout=window.close_reason == "deadline",
            retry=False,
            close_reason=window.close_reason,
        )
        window.mark_advancing()
        result = self.scheduler.advance_window(
            window.session_id,
            decision_window_index=window.decision_window_index,
            ordered_actions=window.merged_action_batch,
        )
        gateway_session.last_advance = result
        window.mark_completed()
        descriptor = self.manager.descriptor(window.session_id)
        if not descriptor.terminal:
            gateway_session.window = self._open_window(
                window.session_id, window.decision_window_index + 1,
            )
            gateway_session.proposals = ActionProposalRegistry(
                window.session_id,
                gateway_session.window.decision_window_index,
            )
            _record_window_open(gateway_session.audit, gateway_session.window)
        return result

    def _expire_and_advance(
        self, gateway_session: _GatewaySession,
    ) -> SimulationAdvance:
        window = gateway_session.window
        for side in ("red", "blue"):
            if side in window.submissions:
                continue
            request_id = f"timeout:{window.decision_window_index}:{side}"
            window.submit(
                side=side,
                request_id=request_id,
                ordered_action_ids=(),
            )
            gateway_session.audit.append(
                "submission_timeout",
                decision_window_index=window.decision_window_index,
                controller_id=gateway_session.bindings[side],
                side=side,
                request_id=request_id,
                expected_window=window.decision_window_index,
                ordered_action_ids=[],
                server_received_at=self._server_now(),
                accepted=True,
                reject_reason=None,
                timeout=True,
                retry=False,
            )
        window.expire()
        return self._advance_closed_window(gateway_session)

    def _server_now(self) -> float | None:
        if self.deadline_mode is DeadlineMode.SYNCHRONOUS:
            return None
        return float(self.clock.now())

    def _poll_deadline_locked(
        self, gateway_session: _GatewaySession,
    ) -> None:
        descriptor = self.manager.descriptor(gateway_session.window.session_id)
        now = self._server_now()
        if (
            not descriptor.terminal
            and gateway_session.window.lifecycle_state
            is DecisionWindowState.OPEN
            and self._deadline_reached(gateway_session.window, now)
        ):
            self._expire_and_advance(gateway_session)

    def _deadline_reached(
        self, window: DecisionWindow, now: float | None,
    ) -> bool:
        if self.deadline_mode is DeadlineMode.SYNCHRONOUS:
            return False
        if now is None or window.deadline_at_server_monotonic is None:
            raise RuntimeError("wall deadline window lacks server clock evidence")
        return now >= window.deadline_at_server_monotonic

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
            opened_at_server_monotonic=window.opened_at_server_monotonic,
            deadline_at_server_monotonic=window.deadline_at_server_monotonic,
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


def _record_window_open(audit: ControlAuditLog, window: DecisionWindow) -> None:
    audit.append(
        "window_opened",
        decision_window_index=window.decision_window_index,
        controller_id=None,
        side=None,
        request_id=None,
        expected_window=window.decision_window_index,
        ordered_action_ids=[],
        server_received_at=window.opened_at_server_monotonic,
        accepted=True,
        reject_reason=None,
        timeout=False,
        retry=False,
        opened_at_sim_time_ms=window.opened_at_sim_time_ms,
        deadline_policy=window.deadline_policy,
        deadline_at_server_monotonic=window.deadline_at_server_monotonic,
    )
