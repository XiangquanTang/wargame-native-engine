"""Fixed-tick, non-blocking RED/BLUE command orchestration.

This module owns command admission and tick scheduling, never adjudication.
Accepted :class:`SeatDecision` values are revalidated through the existing
product operation services at their assigned authoritative tick.  The two
Gateway submissions for that tick remain the only route to ``GameSession``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum
import sys
import tempfile
from threading import Condition, Event, RLock, Thread, current_thread, get_ident
from time import monotonic, monotonic_ns, sleep
import traceback
from typing import Any, Protocol

from wargame_native_engine._framed_history import (
    append_private_record,
    read_private_record,
)
from wargame_native_engine.agents.protocol import AgentResetContext
from wargame_native_engine.agents.seat import (
    AgentDecisionContext,
    SeatActionCapabilities,
    SeatDecision,
    SeatOperation,
)
from wargame_native_engine.gateway import CommandGatewayError, DeadlineMode
from wargame_native_engine.core.tactical_overlay import UnitTacticalOverlay
from wargame_native_engine.gateway.models import ControllerView
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.operations import ProductOperationService
from wargame_native_engine.operations.execution_lifecycle import (
    OperationExecutionAvailability,
    OperationExecutionPage,
    OperationExecutionRecord,
    OperationExecutionTracker,
)
from wargame_native_engine.operations.facade import MatchHandle
from wargame_native_engine.projection.decision_capability import (
    ActionCatalogSnapshot,
)
from wargame_native_engine.projection.decision_semantic import (
    SideDecisionSemanticProjection,
)


RealtimeMatchFactory = Callable[[int, int], MatchHandle]


class CommandStatus(str, Enum):
    PENDING = "PENDING"
    SUPERSEDED = "SUPERSEDED"
    APPLIED = "APPLIED"
    REJECTED_AT_SUBMIT = "REJECTED_AT_SUBMIT"
    REJECTED_AT_APPLY = "REJECTED_AT_APPLY"
    CANCELLED_ON_TERMINAL = "CANCELLED_ON_TERMINAL"
    CANCELLED_ON_TRUNCATION = "CANCELLED_ON_TRUNCATION"
    CANCELLED_ON_RUNNER_LIMIT = "CANCELLED_ON_RUNNER_LIMIT"
    CANCELLED_ON_USER_STOP = "CANCELLED_ON_USER_STOP"
    CANCELLED_ON_FAILURE = "CANCELLED_ON_FAILURE"


class RealtimeExecutionMode(str, Enum):
    MANUAL = "manual"
    BACKGROUND = "background"


class RealtimeLifecycleState(str, Enum):
    NEW = "NEW"
    ACCEPTING = "ACCEPTING"
    STOPPING = "STOPPING"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    CLOSED = "CLOSED"


class RealtimeCancellationReason(str, Enum):
    TERMINAL = "terminal"
    TRUNCATION = "truncation"
    RUNNER_LIMIT = "runner_limit"
    USER_STOP = "user_stop"
    FAILURE = "failure"


class RealtimeClock(Protocol):
    def monotonic_ns(self) -> int:
        """Return a monotonically increasing process-local nanosecond value."""


class SystemMonotonicClock:
    def monotonic_ns(self) -> int:
        return monotonic_ns()


class ManualRealtimeClock:
    """Explicit virtual clock for deterministic tests and replay."""

    def __init__(self, initial_ns: int = 0) -> None:
        self._value = _nonnegative_int(initial_ns, "initial_ns")
        self._lock = RLock()

    def monotonic_ns(self) -> int:
        with self._lock:
            return self._value

    def advance_ns(self, value: int) -> int:
        increment = _nonnegative_int(value, "value")
        with self._lock:
            self._value += increment
            return self._value

    def advance_ms(self, value: int) -> int:
        return self.advance_ns(_nonnegative_int(value, "value") * 1_000_000)


class RealtimeAgentSessionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}:{self.message}")


class RealtimeShutdownTimeoutError(RealtimeAgentSessionError):
    def __init__(self, phase: str, timeout_seconds: float | None) -> None:
        self.phase = str(phase)
        self.timeout_seconds = timeout_seconds
        super().__init__(
            "realtime_shutdown_timeout",
            f"phase={self.phase};timeout_seconds={timeout_seconds}",
        )


@dataclass(frozen=True, slots=True)
class RealtimeSeatBinding:
    binding_id: str
    seat: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "binding_id", _text(self.binding_id, "binding_id"))
        object.__setattr__(self, "seat", _side(self.seat))


@dataclass(frozen=True, slots=True)
class RealtimeSeatView:
    seat: str
    observation_version: int
    tick_index: int
    frame_index: int
    sim_time_ms: int
    observation: Mapping[str, Any]
    capabilities: SeatActionCapabilities
    terminated: bool
    truncated: bool
    decision_semantic: SideDecisionSemanticProjection | None = None
    action_catalog: ActionCatalogSnapshot | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "seat", _side(self.seat))
        _nonnegative_int(self.observation_version, "observation_version")
        _nonnegative_int(self.tick_index, "tick_index")
        _nonnegative_int(self.frame_index, "frame_index")
        _nonnegative_int(self.sim_time_ms, "sim_time_ms")
        _exact_bool(self.terminated, "terminated")
        _exact_bool(self.truncated, "truncated")
        object.__setattr__(self, "observation", json_clone(self.observation))
        object.__setattr__(self, "capabilities", deepcopy(self.capabilities))
        if (self.decision_semantic is None) != (self.action_catalog is None):
            raise ValueError("realtime decision semantic/catalog must be paired")
        if self.decision_semantic is not None:
            if self.decision_semantic.snapshot.side.value != self.seat:
                raise ValueError("realtime Decision side mismatch")
            if (
                self.decision_semantic.snapshot.decision_window_index
                != self.capabilities.decision_index
            ):
                raise ValueError("realtime Decision window mismatch")
            if self.decision_semantic.action_space != self.action_catalog.reference():
                raise ValueError("realtime Decision catalog mismatch")
            object.__setattr__(
                self, "decision_semantic", deepcopy(self.decision_semantic)
            )
            object.__setattr__(self, "action_catalog", deepcopy(self.action_catalog))

    def to_record(self) -> dict[str, object]:
        return {
            "seat": self.seat,
            "observation_version": self.observation_version,
            "tick_index": self.tick_index,
            "frame_index": self.frame_index,
            "sim_time_ms": self.sim_time_ms,
            "observation": json_clone(self.observation),
            "capabilities": self.capabilities.to_record(),
            "terminated": self.terminated,
            "truncated": self.truncated,
        }

    def decision_record(self) -> dict[str, object]:
        if self.decision_semantic is None or self.action_catalog is None:
            raise ValueError("legacy realtime seat view has no Decision core")
        return {
            "decision_semantic": self.decision_semantic.to_dict(),
            "action_catalog": self.action_catalog.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RealtimeResetResult:
    episode_id: str
    run_namespace: str
    tick_interval_ms: int
    red_binding: RealtimeSeatBinding
    blue_binding: RealtimeSeatBinding
    red: RealtimeSeatView
    blue: RealtimeSeatView

    def __post_init__(self) -> None:
        _text(self.episode_id, "episode_id")
        _text(self.run_namespace, "run_namespace")
        _positive_int(self.tick_interval_ms, "tick_interval_ms")
        object.__setattr__(self, "red_binding", deepcopy(self.red_binding))
        object.__setattr__(self, "blue_binding", deepcopy(self.blue_binding))
        object.__setattr__(self, "red", deepcopy(self.red))
        object.__setattr__(self, "blue", deepcopy(self.blue))


@dataclass(frozen=True, slots=True)
class SeatCommandEvent:
    episode_id: str
    command_sequence: int
    seat: str
    observation_version: int
    decision: SeatDecision
    accepted_tick: int
    target_tick: int | None
    status: CommandStatus
    received_monotonic_ns: int
    applied_frame_index: int | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _text(self.episode_id, "episode_id"))
        _nonnegative_int(self.command_sequence, "command_sequence")
        object.__setattr__(self, "seat", _side(self.seat))
        _nonnegative_int(self.observation_version, "observation_version")
        _nonnegative_int(self.accepted_tick, "accepted_tick")
        if self.target_tick is not None:
            _nonnegative_int(self.target_tick, "target_tick")
        if not isinstance(self.decision, SeatDecision):
            raise TypeError("decision must be a SeatDecision")
        object.__setattr__(self, "decision", deepcopy(self.decision))
        if not isinstance(self.status, CommandStatus):
            object.__setattr__(self, "status", CommandStatus(self.status))
        _nonnegative_int(self.received_monotonic_ns, "received_monotonic_ns")
        if self.applied_frame_index is not None:
            _nonnegative_int(self.applied_frame_index, "applied_frame_index")

    def to_record(self) -> dict[str, object]:
        return {
            "record_schema": "native-seat-command/1.1",
            "episode_id": self.episode_id,
            "command_sequence": self.command_sequence,
            "seat": self.seat,
            "observation_version": self.observation_version,
            "decision": self.decision.to_record(),
            "accepted_tick": self.accepted_tick,
            "target_tick": self.target_tick,
            "status": self.status.value,
            "received_monotonic_ns": self.received_monotonic_ns,
            "applied_frame_index": self.applied_frame_index,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class SeatCommandReceipt:
    command_sequence: int | None
    seat: str
    accepted: bool
    target_tick: int | None
    status: CommandStatus
    error_code: str | None
    execution_handles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.command_sequence is not None:
            _nonnegative_int(self.command_sequence, "command_sequence")
        object.__setattr__(self, "seat", _side(self.seat))
        _exact_bool(self.accepted, "accepted")
        if self.target_tick is not None:
            _nonnegative_int(self.target_tick, "target_tick")
        if not isinstance(self.status, CommandStatus):
            object.__setattr__(self, "status", CommandStatus(self.status))
        object.__setattr__(self, "execution_handles", tuple(self.execution_handles))


@dataclass(frozen=True, slots=True)
class RealtimeTickResult:
    tick_index: int
    frame_index: int
    sim_time_start_ms: int
    sim_time_end_ms: int
    red_command_sequences: tuple[int, ...]
    blue_command_sequences: tuple[int, ...]
    red: RealtimeSeatView
    blue: RealtimeSeatView
    terminated: bool
    truncated: bool
    winner: str | None
    outcome: str | None
    processing_duration_ns: int
    overrun_ns: int

    def __post_init__(self) -> None:
        _nonnegative_int(self.tick_index, "tick_index")
        _nonnegative_int(self.frame_index, "frame_index")
        _nonnegative_int(self.sim_time_start_ms, "sim_time_start_ms")
        _nonnegative_int(self.sim_time_end_ms, "sim_time_end_ms")
        _exact_bool(self.terminated, "terminated")
        _exact_bool(self.truncated, "truncated")
        _nonnegative_int(self.processing_duration_ns, "processing_duration_ns")
        _nonnegative_int(self.overrun_ns, "overrun_ns")
        object.__setattr__(
            self,
            "red_command_sequences",
            tuple(
                _nonnegative_int(value, "red_command_sequences")
                for value in self.red_command_sequences
            ),
        )
        object.__setattr__(
            self,
            "blue_command_sequences",
            tuple(
                _nonnegative_int(value, "blue_command_sequences")
                for value in self.blue_command_sequences
            ),
        )
        object.__setattr__(self, "red", deepcopy(self.red))
        object.__setattr__(self, "blue", deepcopy(self.blue))

    @property
    def red_observation(self) -> Mapping[str, Any]:
        return json_clone(self.red.observation)

    @property
    def blue_observation(self) -> Mapping[str, Any]:
        return json_clone(self.blue.observation)

    def to_record(self) -> dict[str, object]:
        return {
            "record_schema": "native-realtime-tick/1.0",
            "tick_index": self.tick_index,
            "frame_index": self.frame_index,
            "sim_time_start_ms": self.sim_time_start_ms,
            "sim_time_end_ms": self.sim_time_end_ms,
            "red_command_sequences": list(self.red_command_sequences),
            "blue_command_sequences": list(self.blue_command_sequences),
            "red_observation_version": self.red.observation_version,
            "blue_observation_version": self.blue.observation_version,
            "terminated": self.terminated,
            "truncated": self.truncated,
            "winner": self.winner,
            "outcome": self.outcome,
            "processing_duration_ns": self.processing_duration_ns,
            "overrun_ns": self.overrun_ns,
        }

    def authoritative_record(self) -> dict[str, object]:
        value = self.to_record()
        value.pop("processing_duration_ns")
        value.pop("overrun_ns")
        return value

    def lightweight_record(self) -> "RealtimeTickRecord":
        """Return the publication facts without retaining either side view."""

        return RealtimeTickRecord(
            tick_index=self.tick_index,
            frame_index=self.frame_index,
            sim_time_start_ms=self.sim_time_start_ms,
            sim_time_end_ms=self.sim_time_end_ms,
            red_command_sequences=self.red_command_sequences,
            blue_command_sequences=self.blue_command_sequences,
            red_observation_version=self.red.observation_version,
            blue_observation_version=self.blue.observation_version,
            terminated=self.terminated,
            truncated=self.truncated,
            winner=self.winner,
            outcome=self.outcome,
            processing_duration_ns=self.processing_duration_ns,
            overrun_ns=self.overrun_ns,
        )


@dataclass(frozen=True, slots=True)
class RealtimeTickRecord:
    """Lightweight retained evidence for one completed realtime tick."""

    tick_index: int
    frame_index: int
    sim_time_start_ms: int
    sim_time_end_ms: int
    red_command_sequences: tuple[int, ...]
    blue_command_sequences: tuple[int, ...]
    red_observation_version: int
    blue_observation_version: int
    terminated: bool
    truncated: bool
    winner: str | None
    outcome: str | None
    processing_duration_ns: int
    overrun_ns: int

    def __post_init__(self) -> None:
        for name in (
            "tick_index", "frame_index", "sim_time_start_ms", "sim_time_end_ms",
            "red_observation_version", "blue_observation_version",
            "processing_duration_ns", "overrun_ns",
        ):
            _nonnegative_int(getattr(self, name), name)
        _exact_bool(self.terminated, "terminated")
        _exact_bool(self.truncated, "truncated")
        for name in ("red_command_sequences", "blue_command_sequences"):
            object.__setattr__(self, name, tuple(
                _nonnegative_int(value, name) for value in getattr(self, name)
            ))

    def to_record(self) -> dict[str, object]:
        return {
            "record_schema": "native-realtime-tick/1.0",
            "tick_index": self.tick_index,
            "frame_index": self.frame_index,
            "sim_time_start_ms": self.sim_time_start_ms,
            "sim_time_end_ms": self.sim_time_end_ms,
            "red_command_sequences": list(self.red_command_sequences),
            "blue_command_sequences": list(self.blue_command_sequences),
            "red_observation_version": self.red_observation_version,
            "blue_observation_version": self.blue_observation_version,
            "terminated": self.terminated,
            "truncated": self.truncated,
            "winner": self.winner,
            "outcome": self.outcome,
            "processing_duration_ns": self.processing_duration_ns,
            "overrun_ns": self.overrun_ns,
        }

    def authoritative_record(self) -> dict[str, object]:
        value = self.to_record()
        value.pop("processing_duration_ns")
        value.pop("overrun_ns")
        return value


class RealtimeAgentSession:
    """One fixed-tick session with independently admitted seat commands."""

    def __init__(
        self,
        match_factory: RealtimeMatchFactory,
        *,
        run_namespace: str,
        tick_interval_ms: int,
        clock: RealtimeClock | None = None,
        observation_history_size: int = 64,
        execution_mode: RealtimeExecutionMode = RealtimeExecutionMode.MANUAL,
        max_catch_up_ticks_per_batch: int = 4,
    ) -> None:
        if not callable(match_factory):
            raise TypeError("match_factory must be callable")
        self.run_namespace = _text(run_namespace, "run_namespace")
        self.tick_interval_ms = _positive_int(tick_interval_ms, "tick_interval_ms")
        self._tick_interval_ns = self.tick_interval_ms * 1_000_000
        self._history_size = _positive_int(
            observation_history_size, "observation_history_size"
        )
        self._match_factory = match_factory
        self.clock = clock or SystemMonotonicClock()
        if not isinstance(execution_mode, RealtimeExecutionMode):
            execution_mode = RealtimeExecutionMode(execution_mode)
        self.execution_mode = execution_mode
        self.max_catch_up_ticks_per_batch = _positive_int(
            max_catch_up_ticks_per_batch, "max_catch_up_ticks_per_batch"
        )
        self._match: MatchHandle | None = None
        self._services: dict[str, ProductOperationService] = {}
        self._advisory_services: dict[str, ProductOperationService] = {}
        self._bindings: dict[str, RealtimeSeatBinding] = {}
        self._binding_by_id: dict[str, RealtimeSeatBinding] = {}
        self._latest: dict[str, RealtimeSeatView] = {}
        self._controller_views: dict[str, ControllerView] = {}
        self._tactical_overlay_cache: dict[
            tuple[str, int, int], UnitTacticalOverlay
        ] = {}
        self._issued: dict[str, deque[int]] = {
            "red": deque(), "blue": deque(),
        }
        self._version_owner: dict[int, str] = {}
        self._next_observation_version = 0
        self._events: dict[int, SeatCommandEvent] = {}
        self._pending: dict[tuple[int, str], int] = {}
        self._ticks: list[RealtimeTickRecord] = []
        self._recent_ticks: deque[RealtimeTickResult] = deque(
            maxlen=self._history_size
        )
        self._tick_history = None
        self._tick_history_offsets: list[tuple[int, int]] = []
        self._next_command_sequence = 0
        self._execution_tracker: OperationExecutionTracker | None = None
        self._command_execution_handles: dict[int, tuple[str, ...]] = {}
        self._current_tick = 0
        self._episode_sequence = 0
        self._episode_id: str | None = None
        self._seed: int | None = None
        self._anchor_ns: int | None = None
        self._processing = False
        self._processing_thread: int | None = None
        self._processing_phase: str | None = None
        self._publication_processing = False
        self._ended = False
        self._failed = False
        self._published = False
        self._closed = False
        self._background_error: BaseException | None = None
        self._background_failure_diagnostic: str | None = None
        self._background_tick_limit: int | None = None
        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._lifecycle = RealtimeLifecycleState.NEW
        self._stop_reason: RealtimeCancellationReason | None = None
        self._catch_up_yield_count = 0

    def reset(self, *, seed: int = 42) -> RealtimeResetResult:
        if type(seed) is not int:
            raise TypeError("seed must be an exact int")
        self.stop_realtime()
        with self._lock:
            self._require_open()
            old = self._match
            old_services = tuple(self._services.values()) + tuple(
                self._advisory_services.values()
            )
            self._match = None
            self._services.clear()
            self._advisory_services.clear()
        _close_product_services(old_services)
        if old is not None:
            old.close()
        self._episode_sequence += 1
        match = self._match_factory(seed, self._episode_sequence)
        if not isinstance(match, MatchHandle):
            raise TypeError("match_factory must return MatchHandle")
        services: dict[str, ProductOperationService] = {}
        advisory_services: dict[str, ProductOperationService] = {}
        try:
            self._validate_match(match)
            episode_id = realtime_episode_id(
                self.run_namespace,
                match.bundle.scenario_identity.sha256,
                seed,
                self._episode_sequence,
            )
            bindings = {
                side: RealtimeSeatBinding(
                    "realtime-seat-" + canonical_hash({
                        "episode_id": episode_id, "seat": side,
                    })[:32],
                    side,
                )
                for side in ("red", "blue")
            }
            services = {
                side: ProductOperationService(match.bind_controller(
                    side,
                    controller_id=(
                        match.red_controller_id
                        if side == "red" else match.blue_controller_id
                    ),
                ))
                for side in ("red", "blue")
            }
            advisory_services = {
                side: ProductOperationService(match.bind_controller(
                    side,
                    controller_id=(
                        match.red_controller_id
                        if side == "red" else match.blue_controller_id
                    ),
                ))
                for side in ("red", "blue")
            }
            anchor = self._clock_now()
            with self._lock:
                self._match = match
                self._services = services
                self._advisory_services = advisory_services
                self._bindings = bindings
                self._binding_by_id = {
                    value.binding_id: value for value in bindings.values()
                }
                self._latest.clear()
                self._controller_views.clear()
                self._tactical_overlay_cache.clear()
                self._issued = {"red": deque(), "blue": deque()}
                self._version_owner.clear()
                self._next_observation_version = 0
                self._events.clear()
                self._pending.clear()
                self._ticks.clear()
                self._recent_ticks.clear()
                self._reset_tick_history_locked()
                self._next_command_sequence = 0
                self._execution_tracker = OperationExecutionTracker(episode_id)
                self._command_execution_handles.clear()
                self._current_tick = 0
                self._episode_id = episode_id
                self._seed = seed
                self._anchor_ns = anchor
                self._processing = False
                self._processing_thread = None
                self._processing_phase = None
                self._publication_processing = False
                self._ended = match.status.terminal
                self._failed = False
                self._published = False
                self._lifecycle = RealtimeLifecycleState.ACCEPTING
                self._stop_reason = None
                self._background_error = None
                self._background_failure_diagnostic = None
                self._background_tick_limit = None
                self._catch_up_yield_count = 0
                self._stop.clear()
                self._issue_views_locked()
                return RealtimeResetResult(
                    episode_id,
                    self.run_namespace,
                    self.tick_interval_ms,
                    deepcopy(bindings["red"]),
                    deepcopy(bindings["blue"]),
                    deepcopy(self._latest["red"]),
                    deepcopy(self._latest["blue"]),
                )
        except BaseException:
            with self._lock:
                if self._match is match:
                    self._match = None
                    self._services.clear()
                    self._advisory_services.clear()
                    self._bindings.clear()
                    self._binding_by_id.clear()
                    self._latest.clear()
                    self._controller_views.clear()
                    self._tactical_overlay_cache.clear()
            _close_product_services(
                tuple(services.values()) + tuple(advisory_services.values())
            )
            match.close()
            raise

    def submit(
        self,
        binding: RealtimeSeatBinding,
        *,
        observation_version: int,
        decision: SeatDecision,
    ) -> SeatCommandReceipt:
        if not isinstance(binding, RealtimeSeatBinding):
            raise TypeError("binding must be a RealtimeSeatBinding")
        if not isinstance(decision, SeatDecision):
            raise TypeError("decision must be a SeatDecision")
        version = _nonnegative_int(observation_version, "observation_version")
        detached = deepcopy(decision)
        with self._lock:
            return self._submit_locked(
                binding,
                version=version,
                decision=detached,
                merge_operation=None,
            )

    def submit_operation(
        self,
        binding: RealtimeSeatBinding,
        *,
        observation_version: int,
        operation: "SeatOperation",
    ) -> SeatCommandReceipt:
        """Atomically append one browser operation to its target-tick decision.

        This is an orchestration convenience, not a second adjudication path.
        Disjoint actors already pending for the same seat/tick are retained;
        the newly selected actor replaces only its own older pending operation.
        The resulting complete ``SeatDecision`` is still revalidated through
        the normal product service at apply time.  ``submit()`` retains the
        existing full-decision overwrite contract used by Agents and replay.
        """

        if not isinstance(binding, RealtimeSeatBinding):
            raise TypeError("binding must be a RealtimeSeatBinding")
        if not isinstance(operation, SeatOperation):
            raise TypeError("operation must be a SeatOperation")
        version = _nonnegative_int(observation_version, "observation_version")
        detached = deepcopy(operation)
        with self._lock:
            return self._submit_locked(
                binding,
                version=version,
                decision=SeatDecision.single(detached),
                merge_operation=detached,
            )

    def _submit_locked(
        self,
        binding: RealtimeSeatBinding,
        *,
        version: int,
        decision: SeatDecision,
        merge_operation: SeatOperation | None,
    ) -> SeatCommandReceipt:
        authoritative = self._binding_by_id.get(binding.binding_id)
        if authoritative is None:
            raise RealtimeAgentSessionError(
                "realtime_binding_unknown", "seat binding is not active"
            )
        side = authoritative.seat
        if self._lifecycle is not RealtimeLifecycleState.ACCEPTING or self._ended:
            return SeatCommandReceipt(
                None,
                side,
                False,
                None,
                CommandStatus.REJECTED_AT_SUBMIT,
                self._submission_closed_code_locked(),
            )
        # This clock read is intentionally inside the same critical section
        # as target selection, sequence allocation, supersession and queue
        # insertion.  It is the live-command linearization point.
        now = self._clock_now()
        sequence = self._allocate_sequence_locked()
        target = self._target_tick_locked(now)
        if binding != authoritative:
            return self._reject_at_submit_locked(
                sequence,
                side,
                version,
                decision,
                now,
                target,
                "realtime_binding_side_mismatch",
            )
        owner = self._version_owner.get(version)
        if owner != side:
            code = (
                (
                    "observation_version_future"
                    if version >= self._next_observation_version
                    else "observation_version_expired_or_unknown"
                )
                if owner is None
                else "observation_version_side_mismatch"
            )
            return self._reject_at_submit_locked(
                sequence, side, version, decision, now, target, code,
            )
        old_sequence = self._pending.get((target, side))
        if old_sequence is not None and merge_operation is not None:
            decision = _merge_pending_operation(
                self._events[old_sequence].decision,
                merge_operation,
            )
        if old_sequence is not None:
            old = self._events[old_sequence]
            self._events[old_sequence] = replace(
                old, status=CommandStatus.SUPERSEDED,
                error_code="superseded_by_later_seat_decision",
            )
            self._sync_execution_command_locked(old_sequence)
        event = SeatCommandEvent(
            self._episode(), sequence, side, version, decision,
            self._current_tick, target, CommandStatus.PENDING, now,
        )
        self._events[sequence] = event
        self._pending[(target, side)] = sequence
        handles = self._track_command_locked(event)
        return SeatCommandReceipt(
            sequence, side, True, target, CommandStatus.PENDING, None, handles,
        )

    def submit_recorded(
        self,
        binding: RealtimeSeatBinding,
        *,
        command_sequence: int,
        observation_version: int,
        target_tick: int,
        decision: SeatDecision,
        received_monotonic_ns: int = 0,
    ) -> SeatCommandReceipt:
        """Admit one recorded command without recomputing its tick from wall time.

        This is an evidence-replay entry point, not a live-controller shortcut.
        Identity, observation ownership, sequence continuity and all apply-time
        product validation remain enforced.
        """

        if not isinstance(binding, RealtimeSeatBinding):
            raise TypeError("binding must be a RealtimeSeatBinding")
        if not isinstance(decision, SeatDecision):
            raise TypeError("decision must be a SeatDecision")
        sequence = _nonnegative_int(command_sequence, "command_sequence")
        version = _nonnegative_int(observation_version, "observation_version")
        target = _nonnegative_int(target_tick, "target_tick")
        received = _nonnegative_int(
            received_monotonic_ns, "received_monotonic_ns"
        )
        with self._lock:
            self._require_accepting()
            authoritative = self._resolve_binding_locked(binding)
            if sequence != self._next_command_sequence:
                raise RealtimeAgentSessionError(
                    "recorded_command_sequence_mismatch",
                    f"expected={self._next_command_sequence};actual={sequence}",
                )
            if target < self._current_tick + (1 if self._processing else 0):
                raise RealtimeAgentSessionError(
                    "recorded_target_tick_already_started",
                    f"target_tick={target};current_tick={self._current_tick}",
                )
            self._next_command_sequence += 1
            side = authoritative.seat
            if self._version_owner.get(version) != side:
                return self._reject_at_submit_locked(
                    sequence,
                    side,
                    version,
                    deepcopy(decision),
                    received,
                    target,
                    "observation_version_expired_or_unknown",
                )
            old_sequence = self._pending.get((target, side))
            if old_sequence is not None:
                self._events[old_sequence] = replace(
                    self._events[old_sequence],
                    status=CommandStatus.SUPERSEDED,
                    error_code="superseded_by_later_seat_decision",
                )
                self._sync_execution_command_locked(old_sequence)
            self._events[sequence] = SeatCommandEvent(
                self._episode(),
                sequence,
                side,
                version,
                decision,
                self._current_tick,
                target,
                CommandStatus.PENDING,
                received,
            )
            self._pending[(target, side)] = sequence
            handles = self._track_command_locked(self._events[sequence])
            return SeatCommandReceipt(
                sequence, side, True, target, CommandStatus.PENDING, None,
                handles,
            )

    def advance_tick(self) -> RealtimeTickResult:
        if self.execution_mode is not RealtimeExecutionMode.MANUAL:
            raise RealtimeAgentSessionError(
                "realtime_manual_advance_forbidden_while_background_active",
                "background sessions are advanced only by their realtime loop",
            )
        result = self._advance_tick(authority="manual")
        if result is None:  # pragma: no cover - background-only stop handoff
            raise AssertionError("manual tick unexpectedly skipped")
        return result

    def _advance_tick_from_background(self) -> RealtimeTickResult | None:
        if self.execution_mode is not RealtimeExecutionMode.BACKGROUND:
            raise RealtimeAgentSessionError(
                "realtime_background_advance_forbidden_in_manual_mode",
                "manual sessions are advanced only by advance_tick",
            )
        if self._thread is not current_thread():
            raise RealtimeAgentSessionError(
                "realtime_background_advance_wrong_thread",
                "only the registered realtime loop may advance a background session",
            )
        return self._advance_tick(authority="background")

    def _advance_tick(self, *, authority: str) -> RealtimeTickResult | None:
        frozen: dict[str, int | None] = {"red": None, "blue": None}
        with self._lock:
            # A stop may linearize after the loop's ACCEPTING check but before
            # this lock is acquired.  That handoff is a normal stop, not a
            # background engine failure, and must not start one more tick.
            if (
                authority == "background"
                and self._lifecycle is RealtimeLifecycleState.STOPPING
                and self._stop.is_set()
            ):
                return None
            self._require_accepting()
            if self._processing:
                raise RealtimeAgentSessionError(
                    "realtime_tick_already_processing",
                    "only one authoritative tick may advance at a time",
                )
            start_ns = self._clock_now()
            tick = self._current_tick
            match = self._require_match()
            window = match.gateway.window(
                match.session_id, match.red_controller_id
            )
            if window.decision_window_index != tick:
                self._failed = True
                self._lifecycle = RealtimeLifecycleState.FAILED
                self._stop_reason = RealtimeCancellationReason.FAILURE
                raise RealtimeAgentSessionError(
                    "realtime_decision_window_mismatch_reset_required",
                    f"expected={tick};actual={window.decision_window_index}",
                )
            frozen = {
                side: self._pending.pop((tick, side), None)
                for side in ("red", "blue")
            }
            self._processing = True
            self._processing_thread = get_ident()
            self._processing_phase = "product_preview"
            anchor = self._anchor()
        rejected: dict[str, tuple[int, str]] = {}
        applied: dict[str, int] = {}
        try:
            previews = {}
            correlations = {}
            decisions = {}
            for side in ("red", "blue"):
                sequence = frozen[side]
                decision = (
                    SeatDecision()
                    if sequence is None else deepcopy(self._events[sequence].decision)
                )
                decisions[side] = decision
                try:
                    previews[side] = self._services[side].preview_decision(
                        decision,
                        request_id=(
                            f"realtime:{self._episode()}:{tick}:{side}:preview:"
                            f"{sequence if sequence is not None else 'empty'}"
                        ),
                    )
                    correlations[side] = (
                        self._services[side]._decision_action_correlations(  # noqa: SLF001
                            previews[side].preview_token
                        )
                    )
                except CommandGatewayError as exc:
                    if sequence is None:
                        raise
                    rejected[side] = (sequence, exc.code)
                    decisions[side] = SeatDecision()
                    previews[side] = self._services[side].preview_decision(
                        decisions[side],
                        request_id=(
                            f"realtime:{self._episode()}:{tick}:{side}:"
                            f"rejected-empty:{sequence}"
                        ),
                    )
                    correlations[side] = ()
            with self._lock:
                tracker = self._require_execution_tracker_locked()
                for side, sequence in frozen.items():
                    if sequence is not None and side not in rejected:
                        tracker.bind_plan(
                            self._command_execution_handles.get(sequence, ()),
                            correlations[side],
                        )
            executions = {}
            with self._lock:
                self._processing_phase = "game_session_step"
            for side in ("red", "blue"):
                executions[side] = self._services[side].execute_decision(
                    previews[side].preview_token,
                    request_id=(
                        f"realtime:{self._episode()}:{tick}:{side}:execute:"
                        f"{frozen[side] if frozen[side] is not None else 'empty'}"
                    ),
                )
                if frozen[side] is not None and side not in rejected:
                    applied[side] = frozen[side]
            if executions["red"].advanced or not executions["blue"].advanced:
                raise RealtimeAgentSessionError(
                    "realtime_gateway_advance_contract_violation",
                    "tick must advance exactly on the second side submission",
                )
            advance = match.gateway.last_advance(
                match.session_id, match.red_controller_id
            )
            if (
                advance is None
                or advance.decision_window_index != tick
                or advance.generated_frame_count != 1
                or advance.sim_time_after_ms - advance.sim_time_before_ms
                != self.tick_interval_ms
            ):
                raise RealtimeAgentSessionError(
                    "realtime_tick_evidence_mismatch_reset_required",
                    "Gateway advance does not match one authoritative tick",
                )
            adjudication_results = {
                "red": match.gateway.last_action_results(
                    match.session_id, match.red_controller_id,
                ),
                "blue": match.gateway.last_action_results(
                    match.session_id, match.blue_controller_id,
                ),
            }
            for side, sequence in tuple(applied.items()):
                rejected_actions = [
                    value for value in adjudication_results[side]
                    if value.get("accepted") is False
                ]
                if rejected_actions:
                    code = str(rejected_actions[0].get("code") or "ACTION_REJECTED")
                    rejected[side] = (sequence, code)
                    applied.pop(side, None)
            with self._lock:
                for side, value in rejected.items():
                    sequence, code = value
                    self._events[sequence] = replace(
                        self._events[sequence],
                        status=CommandStatus.REJECTED_AT_APPLY,
                        error_code=code,
                    )
                    self._sync_execution_command_locked(sequence)
                for side, sequence in applied.items():
                    self._events[sequence] = replace(
                        self._events[sequence],
                        status=CommandStatus.APPLIED,
                        applied_frame_index=advance.last_frame_index,
                    )
                    self._sync_execution_command_locked(sequence)
                tracker = self._require_execution_tracker_locked()
                for side in ("red", "blue"):
                    fact_batch = match.gateway.manager._operation_execution_facts(  # noqa: SLF001
                        match.session_id,
                        side,
                        first_frame_index=advance.first_frame_index,
                        last_frame_index=advance.last_frame_index,
                    )
                    for facts in fact_batch.get("frames", ()):
                        tracker.consume(side, facts)
                self._prune_command_execution_handles_locked()
                self._current_tick += 1
                descriptor = match.status
                self._ended = descriptor.terminal
                self._issue_views_locked()
                if self._ended:
                    tracker.end_active(
                        frame_index=advance.last_frame_index,
                        sim_time_ms=advance.sim_time_after_ms,
                        reason_code=(
                            "SESSION_TERMINATED_WITHOUT_TERMINAL_EXECUTION_FACT"
                            if descriptor.terminated
                            else "SESSION_TRUNCATED_WITHOUT_TERMINAL_EXECUTION_FACT"
                        ),
                    )
                # _issue_views_locked captured the same post-advance frozen
                # frame.  Reuse its RED projection for terminal presentation
                # instead of asking the Gateway to clone it once more.
                final_view = self._controller_views["red"]
                end_ns = self._clock_now()
                deadline_ns = anchor + (tick + 1) * self._tick_interval_ns
                result = RealtimeTickResult(
                    tick,
                    advance.last_frame_index,
                    advance.sim_time_before_ms,
                    advance.sim_time_after_ms,
                    (() if "red" not in applied else (applied["red"],)),
                    (() if "blue" not in applied else (applied["blue"],)),
                    self._latest["red"],
                    self._latest["blue"],
                    descriptor.terminated,
                    descriptor.truncated,
                    final_view.winner,
                    final_view.outcome,
                    max(0, end_ns - start_ns),
                    max(0, start_ns - deadline_ns),
                )
                self._store_full_tick_locked(result)
                # World/RNG and authoritative facts have already committed.
                # Commit compatibility history before its lightweight index;
                # failure enters the existing reset-required lifecycle.
                self._ticks.append(result.lightweight_record())
                self._recent_ticks.append(result)
                if self._ended:
                    reason = (
                        RealtimeCancellationReason.TERMINAL
                        if descriptor.terminated
                        else RealtimeCancellationReason.TRUNCATION
                    )
                    self._begin_stopping_locked(reason)
                return deepcopy(result)
        except BaseException as exc:
            diagnostic = None
            with self._lock:
                if authority == "background":
                    diagnostic = self._capture_background_failure_locked(exc)
                self._failed = True
                self._lifecycle = RealtimeLifecycleState.FAILED
                self._stop_reason = RealtimeCancellationReason.FAILURE
                self._cancel_sequences_locked(
                    tuple(value for value in frozen.values() if value is not None),
                    RealtimeCancellationReason.FAILURE,
                )
                self._cancel_pending_locked(RealtimeCancellationReason.FAILURE)
            if diagnostic is not None:
                _emit_background_failure(diagnostic)
            raise
        finally:
            with self._lock:
                self._processing = False
                self._processing_thread = None
                self._processing_phase = None
                if self._lifecycle is RealtimeLifecycleState.STOPPING:
                    self._cancel_pending_locked(
                        self._stop_reason or RealtimeCancellationReason.USER_STOP
                    )
                self._condition.notify_all()

    def latest_view(self, binding: RealtimeSeatBinding) -> RealtimeSeatView:
        with self._lock:
            authoritative = self._resolve_binding_locked(binding)
            return deepcopy(self._latest[authoritative.seat])

    def controller_snapshot(
        self, binding: RealtimeSeatBinding,
    ) -> tuple[RealtimeSeatView, ControllerView]:
        """Return one coherent, detached side snapshot for presentation.

        The second value is the detached ``ControllerView`` captured when the
        observation version was issued.  Reusing that exact projection keeps
        a browser read coherent with its observation version and, critically,
        avoids rebuilding an expensive side projection while an authoritative
        Map-sized tick is in progress.  No mutable Session or WorldState is
        exposed.
        """

        with self._lock:
            authoritative = self._resolve_binding_locked(binding)
            return (
                deepcopy(self._latest[authoritative.seat]),
                deepcopy(self._controller_views[authoritative.seat]),
            )

    def operation_capabilities(
        self,
        binding: RealtimeSeatBinding,
        actor_ids: Sequence[int],
    ) -> dict[str, object]:
        """Return the existing side-only product capability summary."""

        with self._lock:
            authoritative = self._resolve_binding_locked(binding)
            self._require_accepting()
            service = self._advisory_services[authoritative.seat]
            seat_view = deepcopy(self._latest[authoritative.seat])
            cached = deepcopy(
                seat_view.capabilities.operation_summary.get("indirect_fire", {})
            )
            controller_view = deepcopy(
                self._controller_views[authoritative.seat]
            )
        return self._operation_capabilities_from_view(
            service, actor_ids, controller_view, cached,
        )

    def operation_capabilities_snapshot(
        self,
        binding: RealtimeSeatBinding,
        actor_ids: Sequence[int],
    ) -> tuple[RealtimeSeatView, ControllerView, dict[str, object]]:
        """Return capability data and its exact frozen observation version."""

        with self._lock:
            authoritative = self._resolve_binding_locked(binding)
            self._require_accepting()
            service = self._advisory_services[authoritative.seat]
            seat_view = deepcopy(self._latest[authoritative.seat])
            controller_view = deepcopy(
                self._controller_views[authoritative.seat]
            )
            cached = deepcopy(
                seat_view.capabilities.operation_summary.get(
                    "indirect_fire", {}
                )
            )
        result = self._operation_capabilities_from_view(
            service, actor_ids, controller_view, cached,
        )
        return seat_view, controller_view, result

    @staticmethod
    def _operation_capabilities_from_view(
        service: ProductOperationService,
        actor_ids: Sequence[int],
        controller_view: ControllerView,
        cached: object,
    ) -> dict[str, object]:
        result = deepcopy(service.capabilities(
            actor_ids, view=controller_view,
        ))
        actor_set = set(actor_ids)
        values = cached.get("capabilities", []) if isinstance(
            cached, Mapping
        ) else []
        filtered = [
            deepcopy(value)
            for value in values
            if isinstance(value, Mapping)
            and value.get("shooter_id") in actor_set
        ]
        result["indirect_fire"] = {
            "semantics": "server_validated_hex_target",
            "capabilities": filtered,
        }
        available = sum(
            1 for value in filtered if value.get("status") == "AVAILABLE"
        )
        operations = result.get("operations")
        if isinstance(operations, dict):
            entry = operations.get("INDIRECT_FIRE")
            if isinstance(entry, dict):
                entry["applicable_count"] = available
                if available:
                    entry["unavailable"] = []
        return result

    def tactical_overlay(
        self,
        binding: RealtimeSeatBinding,
        *,
        observation_version: int,
        unit_id: int,
    ) -> UnitTacticalOverlay:
        """Return the current side-bound tactical overlay without advancing."""

        version = _nonnegative_int(observation_version, "observation_version")
        actor = _nonnegative_int(unit_id, "unit_id")
        with self._lock:
            authoritative = self._resolve_binding_locked(binding)
            self._require_accepting()
            latest = self._latest[authoritative.seat]
            if version != latest.observation_version:
                owner = self._version_owner.get(version)
                code = (
                    "tactical_overlay_observation_side_mismatch"
                    if owner is not None and owner != authoritative.seat
                    else "tactical_overlay_observation_stale"
                )
                raise RealtimeAgentSessionError(
                    code,
                    "tactical overlay requires the latest observation version",
                )
            key = (authoritative.seat, version, actor)
            cached = self._tactical_overlay_cache.get(key)
            if cached is None:
                match = self._require_match()
                view = deepcopy(self._controller_views[authoritative.seat])
                try:
                    cached = (
                        match.gateway.manager
                        .query_unit_tactical_overlay_from_projection(
                            match.session_id,
                            authoritative.seat,
                            actor,
                            view,
                        )
                    )
                except ValueError as exc:
                    raise CommandGatewayError(
                        "tactical_overlay_query_rejected", str(exc)
                    ) from exc
                self._tactical_overlay_cache[key] = cached
            return deepcopy(cached)

    def preview_operation(
        self,
        binding: RealtimeSeatBinding,
        operation: "SeatOperation",
        *,
        observation_version: int,
        request_id: str,
    ):
        """Build one advisory product preview for a realtime seat command.

        The preview registry is deliberately separate from the registry used
        by authoritative tick application.  A slow browser preview therefore
        cannot supersede or invalidate the frozen decision being applied by
        the simulation thread.  The high-level operation is revalidated again
        at its target tick by ``_services``.
        """

        if not isinstance(operation, SeatOperation):
            raise TypeError("operation must be a SeatOperation")
        version = _nonnegative_int(observation_version, "observation_version")
        with self._lock:
            authoritative = self._resolve_binding_locked(binding)
            self._require_accepting()
            owner = self._version_owner.get(version)
            if owner != authoritative.seat:
                code = (
                    "observation_version_side_mismatch"
                    if owner is not None
                    else "observation_version_expired_or_unknown"
                )
                raise RealtimeAgentSessionError(
                    code,
                    "preview observation version is not active for this seat",
                )
            service = self._advisory_services[authoritative.seat]
        return service.preview_operation(
            **operation.preview_arguments(),
            request_id=request_id,
        )

    def decision_context(
        self, binding: RealtimeSeatBinding,
    ) -> AgentDecisionContext:
        view = self.latest_view(binding)
        return AgentDecisionContext(
            view.seat,
            view.capabilities.decision_index,
            view.frame_index,
            view.sim_time_ms,
            json_clone(view.observation),
            deepcopy(view.capabilities),
            deepcopy(view.decision_semantic),
            deepcopy(view.action_catalog),
        )

    def agent_reset_context(
        self,
        binding: RealtimeSeatBinding,
        *,
        agent_id: str,
    ) -> AgentResetContext:
        """Return only the existing detached reset contract for one seat."""

        with self._lock:
            authoritative = self._resolve_binding_locked(binding)
            match = self._require_match()
            return deepcopy(match.gateway.manager.agent_reset_context(
                match.session_id,
                authoritative.seat,
                _text(agent_id, "agent_id"),
            ))

    def start_realtime(self, *, max_ticks: int | None = None) -> None:
        if max_ticks is not None:
            _positive_int(max_ticks, "max_ticks")
        with self._lock:
            if self.execution_mode is not RealtimeExecutionMode.BACKGROUND:
                raise RealtimeAgentSessionError(
                    "realtime_background_start_forbidden_after_manual_advance",
                    "session execution mode is manual",
                )
            self._require_accepting()
            if self._thread is not None and self._thread.is_alive():
                raise RealtimeAgentSessionError(
                    "realtime_loop_already_running", "realtime loop is active"
                )
            self._stop.clear()
            self._background_tick_limit = max_ticks
            self._thread = Thread(
                target=self._run_loop,
                name=f"wargame-realtime-{self._episode()}",
                daemon=True,
            )
            self._thread.start()

    def stop_accepting(
        self,
        reason: RealtimeCancellationReason = RealtimeCancellationReason.USER_STOP,
    ) -> None:
        if not isinstance(reason, RealtimeCancellationReason):
            reason = RealtimeCancellationReason(reason)
        with self._lock:
            if self._lifecycle in {
                RealtimeLifecycleState.NEW,
                RealtimeLifecycleState.PUBLISHING,
                RealtimeLifecycleState.PUBLISHED,
                RealtimeLifecycleState.FAILED,
                RealtimeLifecycleState.CLOSED,
            }:
                self._stop.set()
                return
            self._begin_stopping_locked(reason)

    def stop_realtime(
        self,
        *,
        timeout_seconds: float | None = None,
        cancellation_reason: RealtimeCancellationReason = (
            RealtimeCancellationReason.USER_STOP
        ),
    ) -> None:
        if timeout_seconds is not None:
            if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
                raise ValueError("timeout_seconds must be positive or None")
        self.stop_accepting(cancellation_reason)
        started = monotonic()
        with self._lock:
            thread = self._thread
        if thread is None or thread is current_thread():
            thread = None
        if thread is not None:
            thread.join(timeout_seconds)
            if thread.is_alive():
                with self._lock:
                    phase = self._shutdown_phase_locked()
                raise RealtimeShutdownTimeoutError(phase, timeout_seconds)
        with self._condition:
            remaining = _remaining_timeout(started, timeout_seconds)
            while self._processing or self._publication_processing:
                if remaining is not None and remaining <= 0:
                    raise RealtimeShutdownTimeoutError(
                        self._shutdown_phase_locked(), timeout_seconds
                    )
                self._condition.wait(remaining)
                remaining = _remaining_timeout(started, timeout_seconds)
            if self._thread is thread:
                self._thread = None
            if self._lifecycle is RealtimeLifecycleState.STOPPING:
                self._cancel_pending_locked(
                    self._stop_reason or RealtimeCancellationReason.USER_STOP
                )

    def _run_loop(self) -> None:
        catch_up_count = 0
        try:
            while not self._stop.is_set():
                with self._lock:
                    if self._lifecycle is not RealtimeLifecycleState.ACCEPTING:
                        return
                    if (
                        self._background_tick_limit is not None
                        and self._current_tick >= self._background_tick_limit
                    ):
                        self._begin_stopping_locked(
                            RealtimeCancellationReason.RUNNER_LIMIT
                        )
                        return
                    deadline = (
                        self._anchor()
                        + (self._current_tick + 1) * self._tick_interval_ns
                    )
                remaining = deadline - self._clock_now()
                if remaining > 0:
                    self._stop.wait(remaining / 1_000_000_000)
                    catch_up_count = 0
                    continue
                if self._advance_tick_from_background() is None:
                    return
                catch_up_count += 1
                if catch_up_count >= self.max_catch_up_ticks_per_batch:
                    with self._lock:
                        self._catch_up_yield_count += 1
                    sleep(0)
                    catch_up_count = 0
        except BaseException as exc:
            diagnostic = None
            with self._lock:
                diagnostic = self._capture_background_failure_locked(exc)
                self._failed = True
                self._lifecycle = RealtimeLifecycleState.FAILED
                self._stop_reason = RealtimeCancellationReason.FAILURE
                self._cancel_pending_locked(RealtimeCancellationReason.FAILURE)
            if diagnostic is not None:
                _emit_background_failure(diagnostic)
            self._stop.set()
        finally:
            with self._condition:
                self._condition.notify_all()

    def publish(
        self,
        output_dir,
        *,
        truncated: bool | None = None,
        overwrite: bool = False,
        shutdown_timeout_seconds: float | None = None,
        cancellation_reason: RealtimeCancellationReason | None = None,
    ):
        if truncated is not None:
            _exact_bool(truncated, "truncated")
        with self._lock:
            self._require_reset()
            self._raise_if_failed_publication_locked()
            match = self._require_match()
            if cancellation_reason is None:
                if match.status.terminated:
                    cancellation_reason = RealtimeCancellationReason.TERMINAL
                elif match.status.truncated:
                    cancellation_reason = RealtimeCancellationReason.TRUNCATION
                elif truncated is not False:
                    cancellation_reason = RealtimeCancellationReason.TRUNCATION
                else:
                    cancellation_reason = RealtimeCancellationReason.USER_STOP
        self.stop_realtime(
            timeout_seconds=shutdown_timeout_seconds,
            cancellation_reason=cancellation_reason,
        )
        with self._lock:
            self._require_reset()
            # A background failure can occur after the entry check while
            # stop_realtime() is waiting for the in-flight tick to finish.
            self._raise_if_failed_publication_locked()
            if self._processing:
                raise RealtimeAgentSessionError(
                    "realtime_tick_processing", "cannot publish during a tick"
                )
            match = self._require_match()
            descriptor = match.status
            native_truncated = descriptor.truncated
            requested = (
                not descriptor.terminated and not descriptor.truncated
                if truncated is None else truncated
            )
            value = native_truncated or requested
            _exact_bool(value, "truncated")
            if descriptor.terminated:
                cancellation_reason = RealtimeCancellationReason.TERMINAL
            elif descriptor.truncated:
                cancellation_reason = RealtimeCancellationReason.TRUNCATION
            if self._lifecycle is not RealtimeLifecycleState.STOPPING:
                raise RealtimeAgentSessionError(
                    "realtime_publication_invalid_lifecycle",
                    f"lifecycle={self._lifecycle.value}",
                )
            self._cancel_pending_locked(cancellation_reason)
            # Closing admission and taking the immutable evidence snapshot are
            # one lock transaction.  No accepted command can fall between them.
            self._lifecycle = RealtimeLifecycleState.PUBLISHING
            self._publication_processing = True
            events = deepcopy(
                tuple(self._events[key] for key in sorted(self._events))
            )
            ticks = tuple(self._ticks)
        from wargame_native_engine.agents.realtime_publication import (
            publish_realtime_episode,
        )
        try:
            publication = publish_realtime_episode(
                match,
                output_dir,
                run_namespace=self.run_namespace,
                episode_id=self._episode(),
                tick_interval_ms=self.tick_interval_ms,
                command_events=events,
                tick_results=ticks,
                truncated=value,
                overwrite=overwrite,
            )
        except BaseException:
            # Match publication finalizes the underlying Session before the
            # sidecar package is verified.  A failed package therefore cannot
            # safely resume command admission; only reset may recover it.
            with self._lock:
                self._failed = True
                self._lifecycle = RealtimeLifecycleState.FAILED
                self._stop_reason = RealtimeCancellationReason.FAILURE
                self._publication_processing = False
                self._condition.notify_all()
            raise
        with self._lock:
            self._published = True
            self._lifecycle = RealtimeLifecycleState.PUBLISHED
            self._publication_processing = False
            self._condition.notify_all()
        return publication

    def execution_status(
        self,
        binding: RealtimeSeatBinding,
        execution_handle: str,
    ) -> OperationExecutionRecord:
        """Read one detached side-bound execution record without advancing."""

        with self._lock:
            authoritative = self._resolve_binding_locked(binding)
            tracker = self._require_execution_tracker_locked()
            return tracker.get(authoritative.seat, execution_handle)

    def current_executions(
        self,
        binding: RealtimeSeatBinding,
        *,
        limit: int = 100,
    ) -> tuple[OperationExecutionRecord, ...]:
        with self._lock:
            authoritative = self._resolve_binding_locked(binding)
            tracker = self._require_execution_tracker_locked()
            return tracker.current(authoritative.seat, limit=limit)

    def execution_changes(
        self,
        binding: RealtimeSeatBinding,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> OperationExecutionPage:
        with self._lock:
            authoritative = self._resolve_binding_locked(binding)
            tracker = self._require_execution_tracker_locked()
            return tracker.changes(
                authoritative.seat, cursor=cursor, limit=limit,
            )

    @property
    def execution_tracking_available(self) -> bool:
        """Compatibility aggregate; prefer side-bound tracking status."""

        with self._lock:
            return self._require_execution_tracker_locked().available

    def execution_tracking_status(
        self,
        binding: RealtimeSeatBinding,
    ) -> OperationExecutionAvailability:
        with self._lock:
            authoritative = self._resolve_binding_locked(binding)
            return self._require_execution_tracker_locked().tracking_status(
                authoritative.seat
            )

    @property
    def command_events(self) -> tuple[SeatCommandEvent, ...]:
        with self._lock:
            return deepcopy(tuple(self._events[key] for key in sorted(self._events)))

    @property
    def tick_results(self) -> tuple[RealtimeTickResult, ...]:
        """Materialize the compatible full-view history on explicit request.

        Production publication and runners use the lightweight ledger and
        bounded incremental reads instead.  Calling this property is therefore
        intentionally proportional to the complete episode history.
        """

        with self._lock:
            return tuple(
                self._load_full_tick_locked(index)
                for index in range(len(self._ticks))
            )

    @property
    def tick_records(self) -> tuple[RealtimeTickRecord, ...]:
        with self._lock:
            return tuple(self._ticks)

    @property
    def tick_count(self) -> int:
        with self._lock:
            return len(self._ticks)

    @property
    def retained_full_tick_count(self) -> int:
        with self._lock:
            return len(self._recent_ticks)

    def tick_results_since(self, start: int) -> tuple[RealtimeTickResult, ...]:
        """Return detached results from *start* without loading earlier ticks."""

        _nonnegative_int(start, "start")
        with self._lock:
            count = len(self._ticks)
            if start > count:
                raise IndexError("tick result start is beyond current history")
            if start == count:
                return ()
            recent_start = count - len(self._recent_ticks)
            if start >= recent_start:
                offset = start - recent_start
                return deepcopy(tuple(self._recent_ticks)[offset:])
            return tuple(
                self._load_full_tick_locked(index)
                for index in range(start, count)
            )

    def _reset_tick_history_locked(self) -> None:
        if self._tick_history is not None:
            self._tick_history.close()
        self._tick_history = tempfile.TemporaryFile(
            mode="w+b", prefix="wargame-realtime-history."
        )
        self._tick_history_offsets.clear()

    def _store_full_tick_locked(self, value: RealtimeTickResult) -> None:
        if self._tick_history is None:
            raise RuntimeError("realtime tick history is not initialized")
        offset = append_private_record(self._tick_history, value)
        self._tick_history_offsets.append(offset)

    def _load_full_tick_locked(self, index: int) -> RealtimeTickResult:
        if self._tick_history is None:
            raise RuntimeError("realtime tick history is unavailable")
        offset, expected_size = self._tick_history_offsets[index]
        value = read_private_record(
            self._tick_history, offset, expected_size
        )
        if not isinstance(value, RealtimeTickResult):
            raise RuntimeError("realtime tick history has an invalid record")
        return value

    @property
    def current_tick(self) -> int:
        with self._lock:
            return self._current_tick

    @property
    def pending_command_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def terminal(self) -> bool:
        with self._lock:
            if self._match is None:
                return False
            return self._ended and self._match.status.terminated

    @property
    def truncated(self) -> bool:
        with self._lock:
            if self._match is None:
                return False
            return self._ended and self._match.status.truncated

    @property
    def episode_id(self) -> str:
        with self._lock:
            return self._episode()

    @property
    def background_error(self) -> BaseException | None:
        with self._lock:
            return self._background_error

    @property
    def lifecycle(self) -> RealtimeLifecycleState:
        with self._lock:
            return self._lifecycle

    @property
    def catch_up_yield_count(self) -> int:
        with self._lock:
            return self._catch_up_yield_count

    def close(self, *, timeout_seconds: float | None = None) -> None:
        with self._lock:
            if self._lifecycle is RealtimeLifecycleState.CLOSED:
                return
        self.stop_realtime(timeout_seconds=timeout_seconds)
        with self._lock:
            if self._processing:
                raise RealtimeAgentSessionError(
                    "realtime_tick_processing", "cannot close during a tick"
                )
            match = self._match
            if self._execution_tracker is not None and self._latest:
                latest = self._latest["red"]
                self._execution_tracker.end_active(
                    frame_index=latest.frame_index,
                    sim_time_ms=latest.sim_time_ms,
                    reason_code="TRACKING_CLOSED_WITHOUT_TERMINAL_EXECUTION_FACT",
                )
            services = tuple(self._services.values()) + tuple(
                self._advisory_services.values()
            )
            self._match = None
            self._services.clear()
            self._advisory_services.clear()
            self._latest.clear()
            self._controller_views.clear()
            self._tactical_overlay_cache.clear()
            self._recent_ticks.clear()
            self._ticks.clear()
            self._tick_history_offsets.clear()
            if self._tick_history is not None:
                self._tick_history.close()
                self._tick_history = None
            self._closed = True
            self._lifecycle = RealtimeLifecycleState.CLOSED
        _close_product_services(services)
        if match is not None:
            match.close()

    def _validate_match(self, match: MatchHandle) -> None:
        if match.gateway.deadline_mode is not DeadlineMode.SYNCHRONOUS:
            raise RealtimeAgentSessionError(
                "realtime_gateway_requires_synchronous_internal_windows",
                "wall deadlines belong to the realtime coordinator",
            )
        schedule = match.gateway.schedule
        if (
            schedule.decision_interval_ms != self.tick_interval_ms
            or schedule.frame_interval_ms != self.tick_interval_ms
        ):
            raise RealtimeAgentSessionError(
                "realtime_tick_schedule_mismatch",
                "decision and frame intervals must equal tick_interval_ms",
            )

    def _issue_views_locked(self) -> None:
        match = self._require_match()
        self._tactical_overlay_cache.clear()
        for side in ("red", "blue"):
            controller_id = (
                match.red_controller_id if side == "red" else match.blue_controller_id
            )
            window, view = match.gateway.controller_state(
                match.session_id, controller_id
            )
            # Keep the exact side projection paired with the issued realtime
            # observation.  ControllerView is already detached at the Gateway
            # boundary; callers receive another deep copy via
            # controller_snapshot().
            self._controller_views[side] = view
            actors = () if view.terminal else _friendly_actor_ids(view.observation)
            summary = (
                _empty_capability_summary()
                if view.terminal
                else self._services[side].capabilities(actors, view=view)
            )
            if not view.terminal:
                indirect = (
                    match.gateway.manager.get_indirect_fire_capabilities(
                        match.session_id,
                        side,
                    )
                )
                summary["indirect_fire"] = {
                    "semantics": "server_validated_hex_target",
                    "capabilities": [
                        value.to_record() for value in indirect
                    ],
                    "active_areas": [
                        value.to_record()
                        for value in (
                            match.gateway.manager.get_indirect_fire_active_areas(
                                match.session_id,
                                side,
                            )
                        )
                    ],
                    "mission_areas": [
                        value.to_record()
                        for value in (
                            match.gateway.manager.get_indirect_fire_mission_areas(
                                match.session_id,
                                side,
                            )
                        )
                    ],
                }
                available = sum(
                    1 for value in indirect if value.status.value == "AVAILABLE"
                )
                operation = summary.get("operations", {}).get(
                    "INDIRECT_FIRE"
                )
                if isinstance(operation, dict):
                    operation["applicable_count"] = available
                    if available:
                        operation["unavailable"] = []
            operations = summary.get("operations", {})
            operation_types = tuple(
                kind for kind, value in operations.items()
                if isinstance(value, Mapping)
                and int(value.get("applicable_count", 0)) > 0
            )
            capability = SeatActionCapabilities(
                side,
                window.decision_window_index,
                actors,
                operation_types,
                () if view.terminal else view.valid_actions,
                summary,
            )
            version = self._next_observation_version
            self._next_observation_version += 1
            history = self._issued[side]
            history.append(version)
            self._version_owner[version] = side
            while len(history) > self._history_size:
                expired = history.popleft()
                self._version_owner.pop(expired, None)
            self._latest[side] = RealtimeSeatView(
                side,
                version,
                self._current_tick,
                view.frame_index,
                view.sim_time_ms,
                view.observation,
                capability,
                view.terminated,
                view.truncated,
                view.decision_semantic,
                view.action_catalog,
            )

    def _target_tick_locked(self, now_ns: int) -> int:
        if self._processing:
            return self._current_tick + 1
        current_deadline = (
            self._anchor() + (self._current_tick + 1) * self._tick_interval_ns
        )
        return (
            self._current_tick
            if now_ns < current_deadline
            else self._current_tick + 1
        )

    def _reject_at_submit_locked(
        self,
        sequence: int,
        side: str,
        version: int,
        decision: SeatDecision,
        received_ns: int,
        target_tick: int,
        code: str,
    ) -> SeatCommandReceipt:
        self._events[sequence] = SeatCommandEvent(
            self._episode(), sequence, side, version, decision,
            self._current_tick, target_tick, CommandStatus.REJECTED_AT_SUBMIT,
            received_ns, error_code=code,
        )
        return SeatCommandReceipt(
            sequence, side, False, None, CommandStatus.REJECTED_AT_SUBMIT, code,
        )

    def _track_command_locked(
        self, event: SeatCommandEvent,
    ) -> tuple[str, ...]:
        tracker = self._require_execution_tracker_locked()
        view = self._latest[event.seat]
        handles = tracker.submit(
            event.seat,
            event.decision,
            decision_window_index=(
                event.accepted_tick
                if event.target_tick is None else event.target_tick
            ),
            frame_index=view.frame_index,
            sim_time_ms=view.sim_time_ms,
            command_status=event.status.value,
            command_sequence=event.command_sequence,
        )
        self._command_execution_handles[event.command_sequence] = handles
        return handles

    def _sync_execution_command_locked(self, sequence: int) -> None:
        tracker = self._execution_tracker
        event = self._events.get(sequence)
        if tracker is None or event is None:
            return
        view = self._latest.get(event.seat)
        tracker.set_command_status(
            self._command_execution_handles.get(sequence, ()),
            event.status.value,
            reason_code=event.error_code,
            frame_index=(
                event.applied_frame_index
                if event.applied_frame_index is not None
                else None if view is None else view.frame_index
            ),
            sim_time_ms=None if view is None else view.sim_time_ms,
        )
        self._prune_command_execution_handles_locked()

    def _prune_command_execution_handles_locked(self) -> None:
        tracker = self._execution_tracker
        if tracker is None:
            return
        for sequence, handles in tuple(self._command_execution_handles.items()):
            if not handles or not any(tracker.retains(handle) for handle in handles):
                self._command_execution_handles.pop(sequence, None)

    def _require_execution_tracker_locked(self) -> OperationExecutionTracker:
        if self._execution_tracker is None:
            raise RealtimeAgentSessionError(
                "realtime_not_reset", "call reset before execution queries",
            )
        return self._execution_tracker

    def _allocate_sequence_locked(self) -> int:
        value = self._next_command_sequence
        self._next_command_sequence += 1
        return value

    def _begin_stopping_locked(self, reason: RealtimeCancellationReason) -> None:
        if not isinstance(reason, RealtimeCancellationReason):
            reason = RealtimeCancellationReason(reason)
        if self._lifecycle is RealtimeLifecycleState.ACCEPTING:
            self._lifecycle = RealtimeLifecycleState.STOPPING
            self._stop_reason = reason
        elif self._lifecycle is RealtimeLifecycleState.STOPPING:
            # Native terminal/truncation is more authoritative than an earlier
            # generic user/runner stop request.
            if reason in {
                RealtimeCancellationReason.TERMINAL,
                RealtimeCancellationReason.TRUNCATION,
                RealtimeCancellationReason.FAILURE,
            }:
                self._stop_reason = reason
        self._stop.set()
        if not self._processing:
            self._cancel_pending_locked(self._stop_reason or reason)

    def _cancel_sequences_locked(
        self,
        sequences: Sequence[int],
        reason: RealtimeCancellationReason,
    ) -> None:
        status, code = _cancellation_status(reason)
        for sequence in sequences:
            event = self._events.get(sequence)
            if event is not None and event.status is CommandStatus.PENDING:
                self._events[sequence] = replace(
                    event, status=status, error_code=code
                )
                self._sync_execution_command_locked(sequence)

    def _cancel_pending_locked(
        self, reason: RealtimeCancellationReason
    ) -> None:
        status, code = _cancellation_status(reason)
        for sequence in tuple(self._pending.values()):
            event = self._events[sequence]
            if event.status is CommandStatus.PENDING:
                self._events[sequence] = replace(
                    event,
                    status=status,
                    error_code=code,
                )
                self._sync_execution_command_locked(sequence)
        self._pending.clear()

    def _submission_closed_code_locked(self) -> str:
        if self._ended:
            return "realtime_session_ended"
        return f"realtime_session_{self._lifecycle.value.lower()}"

    def _shutdown_phase_locked(self) -> str:
        if self._publication_processing:
            return "publication"
        if self._processing:
            return self._processing_phase or "game_session_step"
        if self._thread is not None and self._thread.is_alive():
            return "realtime_loop"
        return "shutdown"

    def _capture_background_failure_locked(
        self, exc: BaseException,
    ) -> str | None:
        """Save and format only the first background failure.

        This is called before tick cleanup clears ``_processing_phase``.  The
        diagnostic deliberately contains no state, observations, actions,
        RNG values, or traceback locals.
        """

        if self._background_error is not None:
            return None
        self._background_error = exc
        exception_type = f"{type(exc).__module__}.{type(exc).__qualname__}"
        phase = self._processing_phase or "realtime_loop"
        header = (
            "[wargame-realtime-background-failure] "
            f"exception_type={exception_type} "
            f"tick={self._current_tick} "
            f"lifecycle={self._lifecycle.value} "
            f"phase={phase}\n"
        )
        try:
            original_traceback = "".join(
                traceback.TracebackException.from_exception(
                    exc, capture_locals=False
                ).format()
            )
        except BaseException as formatting_error:
            original_traceback = (
                f"{exception_type}: traceback formatting failed "
                f"({type(formatting_error).__name__})\n"
            )
        diagnostic = header + original_traceback
        self._background_failure_diagnostic = diagnostic
        return diagnostic

    def _raise_if_failed_publication_locked(self) -> None:
        if self._lifecycle is not RealtimeLifecycleState.FAILED and not self._failed:
            return
        error = RealtimeAgentSessionError(
            "realtime_session_failed_reset_required",
            "failed session cannot publish; reset is required",
        )
        if self._background_error is None:
            raise error
        raise error from self._background_error

    def _resolve_binding_locked(
        self, binding: RealtimeSeatBinding,
    ) -> RealtimeSeatBinding:
        if not isinstance(binding, RealtimeSeatBinding):
            raise TypeError("binding must be a RealtimeSeatBinding")
        authoritative = self._binding_by_id.get(binding.binding_id)
        if authoritative is None or authoritative != binding:
            raise RealtimeAgentSessionError(
                "realtime_binding_invalid", "seat binding is not authoritative"
            )
        return authoritative

    def _clock_now(self) -> int:
        value = self.clock.monotonic_ns()
        return _nonnegative_int(value, "clock.monotonic_ns")

    def _anchor(self) -> int:
        if self._anchor_ns is None:
            raise RealtimeAgentSessionError(
                "realtime_not_reset", "reset must be called first"
            )
        return self._anchor_ns

    def _episode(self) -> str:
        if self._episode_id is None:
            raise RealtimeAgentSessionError(
                "realtime_not_reset", "reset must be called first"
            )
        return self._episode_id

    def _require_match(self) -> MatchHandle:
        if self._match is None:
            raise RealtimeAgentSessionError(
                "realtime_not_reset", "reset must be called first"
            )
        return self._match

    def _require_reset(self) -> None:
        self._require_match()
        self._episode()

    def _require_open(self) -> None:
        if self._lifecycle is RealtimeLifecycleState.CLOSED or self._closed:
            raise RealtimeAgentSessionError(
                "realtime_session_closed", "session is closed"
            )

    def _require_accepting(self) -> None:
        self._require_open()
        self._require_reset()
        if self._lifecycle is RealtimeLifecycleState.FAILED or self._failed:
            raise RealtimeAgentSessionError(
                "realtime_session_failed_reset_required",
                "reset is required after a coordinator failure",
            )
        if self._lifecycle in {
            RealtimeLifecycleState.PUBLISHING,
            RealtimeLifecycleState.PUBLISHED,
        } or self._published:
            raise RealtimeAgentSessionError(
                "realtime_session_published",
                "published session cannot accept or advance",
            )
        if self._ended:
            raise RealtimeAgentSessionError(
                "realtime_session_ended", "terminal session cannot accept or advance",
            )
        if self._lifecycle is RealtimeLifecycleState.STOPPING:
            raise RealtimeAgentSessionError(
                "realtime_session_stopping",
                "stopping session cannot accept or advance",
            )
        if self._lifecycle is not RealtimeLifecycleState.ACCEPTING:
            raise RealtimeAgentSessionError(
                "realtime_session_not_accepting",
                f"lifecycle={self._lifecycle.value}",
            )


def _emit_background_failure(diagnostic: str) -> None:
    """Best-effort local evidence emission that cannot replace the failure."""

    try:
        sys.stderr.write(diagnostic)
        sys.stderr.flush()
    except BaseException:
        # stderr itself may be closed or resource constrained.  The original
        # exception remains available through background_error and chaining.
        pass


def _friendly_actor_ids(observation: Mapping[str, Any]) -> tuple[int, ...]:
    values = observation.get("friendly_operators", ())
    result = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        actor = value.get("operator_id")
        if type(actor) is int and actor >= 0:
            result.append(actor)
    return tuple(sorted(set(result)))


def _merge_pending_operation(
    pending: SeatDecision,
    incoming: SeatOperation,
) -> SeatDecision:
    """Compose browser operations without changing Agent overwrite semantics."""

    if incoming.whole_side_wait:
        return SeatDecision.single(incoming)
    incoming_actors = set(incoming.actor_ids)
    retained: list[SeatOperation] = []
    for operation in pending.operations:
        if operation.whole_side_wait:
            continue
        remaining = tuple(
            actor_id for actor_id in operation.actor_ids
            if actor_id not in incoming_actors
        )
        if not remaining:
            continue
        choices = tuple(
            (actor_id, weapon_id)
            for actor_id, weapon_id in operation.weapon_choices
            if actor_id in remaining
        )
        retained.append(replace(
            operation,
            actor_ids=remaining,
            weapon_choices=choices,
        ))
    retained.append(incoming)
    return SeatDecision(tuple(retained))


def realtime_episode_id(
    run_namespace: str,
    scenario_hash: str,
    seed: int,
    episode_sequence: int,
) -> str:
    namespace = _text(run_namespace, "run_namespace")
    scenario = _text(scenario_hash, "scenario_hash")
    if type(seed) is not int:
        raise TypeError("seed must be an exact int")
    sequence = _positive_int(episode_sequence, "episode_sequence")
    return "native-realtime-" + canonical_hash({
        "run_namespace": namespace,
        "scenario_hash": scenario,
        "seed": seed,
        "episode_sequence": sequence,
    })[:32]


def _empty_capability_summary() -> dict[str, object]:
    return {
        "selected_count": 0,
        "selected_units": [],
        "operations": {},
        "shoot_target_ids": [],
    }


def _cancellation_status(
    reason: RealtimeCancellationReason,
) -> tuple[CommandStatus, str]:
    values = {
        RealtimeCancellationReason.TERMINAL: (
            CommandStatus.CANCELLED_ON_TERMINAL,
            "session_terminal_before_target_tick",
        ),
        RealtimeCancellationReason.TRUNCATION: (
            CommandStatus.CANCELLED_ON_TRUNCATION,
            "session_truncated_before_target_tick",
        ),
        RealtimeCancellationReason.RUNNER_LIMIT: (
            CommandStatus.CANCELLED_ON_RUNNER_LIMIT,
            "runner_limit_before_target_tick",
        ),
        RealtimeCancellationReason.USER_STOP: (
            CommandStatus.CANCELLED_ON_USER_STOP,
            "user_stop_before_target_tick",
        ),
        RealtimeCancellationReason.FAILURE: (
            CommandStatus.CANCELLED_ON_FAILURE,
            "coordinator_failure_before_target_tick",
        ),
    }
    return values[RealtimeCancellationReason(reason)]


def _remaining_timeout(
    started: float, timeout_seconds: float | None
) -> float | None:
    if timeout_seconds is None:
        return None
    return max(0.0, timeout_seconds - (monotonic() - started))


def _close_product_services(
    services: Sequence[ProductOperationService],
) -> None:
    for service in services:
        try:
            service.close()
        except Exception:
            # Session/match close still owns authoritative resources.  A
            # best-effort temporary-history close must not mask that outcome.
            pass


def _side(value: object) -> str:
    result = _text(value, "seat").lower()
    if result not in {"red", "blue"}:
        raise ValueError("seat must be RED or BLUE")
    return result


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field} must not contain surrounding whitespace")
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise TypeError(f"{field} must be a positive exact int")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise TypeError(f"{field} must be a non-negative exact int")
    return value


def _exact_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field} must be an exact bool")
    return value
