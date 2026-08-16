"""Independent local seat workers over :mod:`agents.realtime`."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, RLock, Thread
from time import monotonic, monotonic_ns
from wargame_native_engine.agents.realtime import (
    RealtimeCancellationReason,
    RealtimeAgentSession,
    RealtimeAgentSessionError,
    RealtimeExecutionMode,
    RealtimeSeatBinding,
    RealtimeSeatView,
    RealtimeShutdownTimeoutError,
)
from wargame_native_engine.agents.realtime_publication import (
    RealtimeEpisodePublication,
)
from wargame_native_engine.agents.seat import (
    AgentDecisionContext,
    LocalSeatAgent,
    SeatDecision,
)


@dataclass(frozen=True, slots=True)
class RealtimeAgentFailure:
    seat: str
    agent_id: str
    observation_version: int
    error_type: str
    error_message: str


@dataclass(frozen=True, slots=True)
class RealtimeLocalAgentResult:
    publication: RealtimeEpisodePublication
    agent_failures: tuple[RealtimeAgentFailure, ...]
    agent_inference_duration_ns: dict[str, tuple[int, ...]]
    agent_submit_duration_ns: dict[str, tuple[int, ...]]
    reset_duration_ns: int
    shutdown_duration_ns: int
    publication_duration_ns: int


class RealtimeLocalAgentRunner:
    """Run RED/BLUE inference independently while the tick loop never waits."""

    def __init__(
        self,
        session: RealtimeAgentSession,
        *,
        red_agent: LocalSeatAgent,
        blue_agent: LocalSeatAgent,
    ) -> None:
        if not isinstance(session, RealtimeAgentSession):
            raise TypeError("session must be a RealtimeAgentSession")
        if session.execution_mode is not RealtimeExecutionMode.BACKGROUND:
            raise ValueError("realtime_local_agent_runner_requires_background_mode")
        self.session = session
        self.agents = {"red": red_agent, "blue": blue_agent}
        for side, agent in self.agents.items():
            agent_id = getattr(agent, "agent_id", None)
            if not isinstance(agent_id, str) or not agent_id.strip():
                raise ValueError(f"{side} agent_id must be a non-empty string")
        self._queues: dict[str, Queue[RealtimeSeatView | None]] = {
            "red": Queue(maxsize=1), "blue": Queue(maxsize=1),
        }
        self._workers: list[Thread] = []
        self._bindings: dict[str, RealtimeSeatBinding] = {}
        self._stop = Event()
        self._failures: list[RealtimeAgentFailure] = []
        self._inference_durations: dict[str, list[int]] = {
            "red": [], "blue": [],
        }
        self._submit_durations: dict[str, list[int]] = {
            "red": [], "blue": [],
        }
        self._ran = False
        self._lock = RLock()

    def run(
        self,
        output_dir: str | Path,
        *,
        seed: int = 42,
        max_ticks: int,
        timeout_seconds: float = 30.0,
        shutdown_timeout_seconds: float | None = 30.0,
        overwrite: bool = False,
    ) -> RealtimeLocalAgentResult:
        if type(max_ticks) is not int or max_ticks <= 0:
            raise ValueError("max_ticks must be a positive exact int")
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if shutdown_timeout_seconds is not None and (
            isinstance(shutdown_timeout_seconds, bool)
            or shutdown_timeout_seconds <= 0
        ):
            raise ValueError("shutdown_timeout_seconds must be positive or None")
        if self._ran:
            raise RuntimeError("realtime_local_agent_runner_is_single_use")
        self._ran = True
        delivered_ticks = 0
        deadline = monotonic() + float(timeout_seconds)
        workers_started = False
        workers_stop_attempted = False
        workers_stopped = False
        session_stopped = False
        completed = False
        stop_reason = RealtimeCancellationReason.FAILURE
        try:
            reset_started = monotonic_ns()
            reset = self.session.reset(seed=seed)
            reset_duration_ns = monotonic_ns() - reset_started
            self._bindings = {
                "red": reset.red_binding,
                "blue": reset.blue_binding,
            }
            self._reset_agents()
            self._stop.clear()
            self._start_workers()
            workers_started = True
            self._offer("red", reset.red)
            self._offer("blue", reset.blue)
            self.session.start_realtime(max_ticks=max_ticks)
            while len(self.session.tick_results) < max_ticks:
                if self.session.terminal or self.session.truncated:
                    break
                if self.session.background_error is not None:
                    raise RealtimeAgentSessionError(
                        "realtime_background_loop_failed",
                        str(self.session.background_error),
                    )
                ticks = self.session.tick_results
                while delivered_ticks < len(ticks):
                    tick = ticks[delivered_ticks]
                    delivered_ticks += 1
                    if (
                        delivered_ticks < max_ticks
                        and not (tick.terminated or tick.truncated)
                    ):
                        self._offer("red", tick.red)
                        self._offer("blue", tick.blue)
                if monotonic() >= deadline:
                    raise TimeoutError("realtime_local_agent_runner_timeout")
                self._stop.wait(0.001)
            stop_reason = (
                RealtimeCancellationReason.TERMINAL
                if self.session.terminal
                else (
                    RealtimeCancellationReason.TRUNCATION
                    if self.session.truncated
                    else RealtimeCancellationReason.RUNNER_LIMIT
                )
            )
            # Close admission before signalling workers.  An in-flight Agent
            # may return afterwards, but its decision can no longer be accepted.
            shutdown_started = monotonic_ns()
            self.session.stop_accepting(stop_reason)
            workers_stop_attempted = True
            self._stop_workers(timeout_seconds=shutdown_timeout_seconds)
            workers_stopped = True
            self.session.stop_realtime(
                timeout_seconds=shutdown_timeout_seconds,
                cancellation_reason=stop_reason,
            )
            session_stopped = True
            shutdown_duration_ns = monotonic_ns() - shutdown_started
            publication_started = monotonic_ns()
            publication = self.session.publish(
                output_dir,
                truncated=not self.session.terminal,
                overwrite=overwrite,
                shutdown_timeout_seconds=shutdown_timeout_seconds,
                cancellation_reason=stop_reason,
            )
            publication_duration_ns = monotonic_ns() - publication_started
            completed = True
            return RealtimeLocalAgentResult(
                publication,
                self.agent_failures,
                self.agent_inference_duration_ns,
                self.agent_submit_duration_ns,
                reset_duration_ns,
                shutdown_duration_ns,
                publication_duration_ns,
            )
        finally:
            try:
                if not completed:
                    self.session.stop_accepting(RealtimeCancellationReason.FAILURE)
                if (
                    workers_started
                    and not workers_stopped
                    and not workers_stop_attempted
                ):
                    workers_stop_attempted = True
                    self._stop_workers(timeout_seconds=shutdown_timeout_seconds)
            finally:
                try:
                    if not session_stopped:
                        self.session.stop_realtime(
                            timeout_seconds=shutdown_timeout_seconds,
                            cancellation_reason=(
                                stop_reason
                                if completed else RealtimeCancellationReason.FAILURE
                            ),
                        )
                finally:
                    for agent in self.agents.values():
                        agent.close()

    @property
    def agent_failures(self) -> tuple[RealtimeAgentFailure, ...]:
        with self._lock:
            return deepcopy(tuple(self._failures))

    @property
    def agent_inference_duration_ns(self) -> dict[str, tuple[int, ...]]:
        with self._lock:
            return {
                side: tuple(values)
                for side, values in self._inference_durations.items()
            }

    @property
    def agent_submit_duration_ns(self) -> dict[str, tuple[int, ...]]:
        with self._lock:
            return {
                side: tuple(values)
                for side, values in self._submit_durations.items()
            }

    def _reset_agents(self) -> None:
        for side in ("red", "blue"):
            agent = self.agents[side]
            agent.reset(self.session.agent_reset_context(
                self._bindings[side], agent_id=agent.agent_id,
            ))

    def _start_workers(self) -> None:
        if self._workers:
            raise RuntimeError("realtime_agent_workers_already_started")
        for side in ("red", "blue"):
            thread = Thread(
                target=self._worker,
                args=(side,),
                name=f"wargame-agent-{side}-{self.agents[side].agent_id}",
                daemon=True,
            )
            self._workers.append(thread)
            thread.start()

    def _stop_workers(self, *, timeout_seconds: float | None) -> None:
        self._stop.set()
        for side in ("red", "blue"):
            self._offer(side, None)
        started = monotonic()
        for thread in self._workers:
            remaining = (
                None
                if timeout_seconds is None
                else max(0.0, timeout_seconds - (monotonic() - started))
            )
            thread.join(remaining)
            if thread.is_alive():
                raise RealtimeShutdownTimeoutError(
                    "agent_worker", timeout_seconds
                )
        self._workers.clear()

    def _worker(self, side: str) -> None:
        queue = self._queues[side]
        agent = self.agents[side]
        binding = self._bindings[side]
        while not self._stop.is_set():
            try:
                view = queue.get(timeout=0.1)
            except Empty:
                continue
            if view is None:
                return
            inference_started: int | None = None
            try:
                context = AgentDecisionContext(
                    side,
                    view.tick_index,
                    view.frame_index,
                    view.sim_time_ms,
                    deepcopy(view.observation),
                    deepcopy(view.capabilities),
                )
                inference_started = monotonic_ns()
                decision = agent.act(context=deepcopy(context))
                if self._stop.is_set():
                    return
                if not isinstance(decision, SeatDecision):
                    raise TypeError("LocalSeatAgent.act must return SeatDecision")
                submit_started = monotonic_ns()
                try:
                    receipt = self.session.submit(
                        binding,
                        observation_version=view.observation_version,
                        decision=deepcopy(decision),
                    )
                finally:
                    with self._lock:
                        self._submit_durations[side].append(
                            monotonic_ns() - submit_started
                        )
                if not receipt.accepted and self._stop.is_set():
                    return
            except RealtimeAgentSessionError as exc:
                if exc.code in {"realtime_session_ended", "realtime_session_closed"}:
                    return
                self._record_failure(side, agent.agent_id, view, exc)
            except BaseException as exc:
                self._record_failure(side, agent.agent_id, view, exc)
            finally:
                if inference_started is not None:
                    with self._lock:
                        self._inference_durations[side].append(
                            monotonic_ns() - inference_started
                        )

    def _record_failure(
        self,
        side: str,
        agent_id: str,
        view: RealtimeSeatView,
        exc: BaseException,
    ) -> None:
        with self._lock:
            self._failures.append(RealtimeAgentFailure(
                side,
                agent_id,
                view.observation_version,
                type(exc).__name__,
                str(exc),
            ))

    def _offer(self, side: str, view: RealtimeSeatView | None) -> None:
        queue = self._queues[side]
        while True:
            try:
                queue.put_nowait(None if view is None else deepcopy(view))
                return
            except Full:
                try:
                    queue.get_nowait()
                except Empty:
                    continue
