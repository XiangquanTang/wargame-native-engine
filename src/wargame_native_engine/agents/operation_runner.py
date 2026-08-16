"""Local RED/BLUE seat runner over the product Operations Facade."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from wargame_native_engine.agents.seat import (
    AgentDecisionContext,
    LocalSeatAgent,
    SeatDecision,
)
from wargame_native_engine.gateway import CommandGatewayError
from wargame_native_engine.io.serialization import json_clone
from wargame_native_engine.operations.facade import MatchHandle
from wargame_native_engine.training.models import DecisionTransition
from wargame_native_engine.training.multi_seat import (
    NativeDecisionSession,
    NativeMultiSeatError,
    mark_transition_pair_truncated,
    training_episode_id,
)
from wargame_native_engine.training.publication import (
    publish_training_episode,
)


MATCH_SUMMARY_SCHEMA = "native-local-seat-match-summary/1.2"


class LocalSeatMatchError(RuntimeError):
    """A seat match stopped before synchronized evidence was published."""

    def __init__(self, status: str, detail: Mapping[str, Any]) -> None:
        self.status = str(status)
        self.detail = json_clone(detail)
        super().__init__(f"local_seat_match_failed:{self.status}")


class SeatAgentExecutionError(LocalSeatMatchError):
    """One local seat Agent failed during reset or decision generation."""

    def __init__(
        self,
        *,
        agent_id: str,
        side: str,
        decision_index: int,
        phase: str,
        cause: BaseException,
        called_agent_ids: Sequence[str],
        submitted_sides: Sequence[str],
    ) -> None:
        self.agent_id = str(agent_id)
        self.side = str(side).lower()
        self.decision_index = int(decision_index)
        self.phase = str(phase)
        self.cause_type = type(cause).__name__
        self.cause_message = str(cause)
        self.called_agent_ids = tuple(called_agent_ids)
        self.submitted_sides = tuple(submitted_sides)
        super().__init__("agent_error", {
            "agent_id": self.agent_id,
            "perspective": self.side.upper(),
            "decision_index": self.decision_index,
            "phase": self.phase,
            "cause_type": self.cause_type,
            "cause_message": self.cause_message,
            "called_agent_ids": list(self.called_agent_ids),
            "submitted_sides": [item.upper() for item in self.submitted_sides],
        })
        self.args = (
            "seat_agent_execution_error:"
            f"{self.agent_id}:{self.side.upper()}:{self.decision_index}:"
            f"{self.phase}:{self.cause_type}:{self.cause_message}",
        )


class SeatOperationError(LocalSeatMatchError):
    """The shared product operation layer rejected an Agent decision."""

    def __init__(
        self,
        *,
        agent_id: str,
        side: str,
        decision_index: int,
        cause: CommandGatewayError,
        submitted_sides: Sequence[str],
    ) -> None:
        self.agent_id = str(agent_id)
        self.side = str(side).lower()
        self.decision_index = int(decision_index)
        self.operation_error_code = cause.code
        self.submitted_sides = tuple(submitted_sides)
        super().__init__("invalid_agent_operation", {
            "agent_id": self.agent_id,
            "perspective": self.side.upper(),
            "decision_index": self.decision_index,
            "operation_error_code": cause.code,
            "operation_error_message": cause.message,
            "submitted_sides": [item.upper() for item in self.submitted_sides],
        })


@dataclass(frozen=True, slots=True)
class LocalSeatMatchResult:
    output_dir: Path
    replay_path: Path
    analysis_path: Path
    transition_path: Path
    summary_path: Path
    terminal: bool
    truncated: bool
    decision_window_count: int
    fact_frame_count: int
    step_count: int
    transition_count: int
    winner: str | None
    outcome: str | None
    final_state_hash: str
    strict_replay_match: bool
    online_offline_analysis_match: bool
    run_namespace: str
    episode_id: str

    @property
    def frame_count(self) -> int:
        """Compatibility alias; facts are not decision windows."""

        return self.fact_frame_count


class LocalSeatMatchRunner:
    """Drive local seat Agents through product preview/execute and Gateway."""

    def __init__(
        self,
        match: MatchHandle,
        *,
        red_agent: LocalSeatAgent,
        blue_agent: LocalSeatAgent,
        run_namespace: str,
    ) -> None:
        if not isinstance(match, MatchHandle):
            raise TypeError("match must be a MatchHandle")
        self.match = match
        self.agents = {"red": red_agent, "blue": blue_agent}
        for side, agent in self.agents.items():
            agent_id = getattr(agent, "agent_id", None)
            if not isinstance(agent_id, str) or not agent_id.strip():
                raise ValueError(f"{side} agent_id must be a non-empty string")
        seed = match.gateway.manager.agent_reset_context(
            match.session_id, "red", red_agent.agent_id
        ).seed
        self.run_namespace = _nonempty_namespace(run_namespace)
        episode_id = training_episode_id(
            self.run_namespace,
            match.bundle.scenario_identity.sha256,
            seed,
            1,
        )
        self.decision_session = NativeDecisionSession(
            match,
            episode_id=episode_id,
            run_namespace=self.run_namespace,
            seed=seed,
        )
        self.decision_session.reset_result()
        self.controllers = self.decision_session.controllers
        self.product_services = self.decision_session.product_services
        self._transitions: list[DecisionTransition] = []
        self._ran = False

    def run(
        self,
        output_dir: str | Path,
        *,
        max_decision_windows: int | None = None,
        overwrite: bool = False,
    ) -> LocalSeatMatchResult:
        if self._ran:
            raise RuntimeError("local_seat_match_runner_is_single_use")
        self._ran = True
        if max_decision_windows is not None and (
            type(max_decision_windows) is not int
            or max_decision_windows <= 0
        ):
            raise ValueError("max_decision_windows must be positive or None")
        if type(overwrite) is not bool:
            raise TypeError("overwrite must be bool")
        target = Path(output_dir)

        try:
            self._reset_agents()
            processed = self._run_windows(max_decision_windows)
            final_view = self.controllers["red"].observe()
            truncated = not final_view.terminal
            transitions = tuple(self._transitions)
            if truncated and transitions:
                transitions = (
                    *transitions[:-2],
                    *mark_transition_pair_truncated(transitions[-2:]),
                )
            publication = publish_training_episode(
                self.match,
                target,
                run_namespace=self.run_namespace,
                episode_id=self.decision_session.episode_id,
                transitions=transitions,
                decision_window_count=processed,
                truncated=truncated,
                outcome=final_view.outcome,
                summary_schema=MATCH_SUMMARY_SCHEMA,
                summary_extra={
                "red_agent_id": self.agents["red"].agent_id,
                "blue_agent_id": self.agents["blue"].agent_id,
                "error": None,
                },
                overwrite=overwrite,
            )
            return LocalSeatMatchResult(
                publication.output_dir,
                publication.replay_path,
                publication.analysis_path,
                publication.transition_path,
                publication.summary_path,
                publication.terminal,
                publication.truncated,
                processed,
                publication.fact_frame_count,
                publication.step_count,
                len(transitions),
                publication.winner,
                final_view.outcome,
                publication.final_state_hash,
                True,
                True,
                self.run_namespace,
                self.decision_session.episode_id,
            )
        finally:
            self._close_agents()

    def _reset_agents(self) -> None:
        called: list[str] = []
        manager = self.match.gateway.manager
        for side in ("red", "blue"):
            agent = self.agents[side]
            called.append(agent.agent_id)
            context = manager.agent_reset_context(
                self.match.session_id, side, agent.agent_id
            )
            try:
                agent.reset(deepcopy(context))
            except BaseException as exc:
                raise SeatAgentExecutionError(
                    agent_id=agent.agent_id,
                    side=side,
                    decision_index=0,
                    phase="reset",
                    cause=exc,
                    called_agent_ids=called,
                    submitted_sides=(),
                ) from exc

    def _run_windows(self, maximum: int | None) -> int:
        processed = 0
        while not self.match.status.terminal:
            if maximum is not None and processed >= maximum:
                break
            expected_window = self.controllers[
                "red"
            ].window().decision_window_index
            called: list[str] = []
            decisions: dict[str, SeatDecision] = {}
            for side in ("red", "blue"):
                agent = self.agents[side]
                called.append(agent.agent_id)
                context = self._decision_context(side, expected_window)
                try:
                    decision = agent.act(context=deepcopy(context))
                    if not isinstance(decision, SeatDecision):
                        raise TypeError(
                            "LocalSeatAgent.act must return SeatDecision"
                        )
                    decisions[side] = decision
                except BaseException as exc:
                    raise SeatAgentExecutionError(
                        agent_id=agent.agent_id,
                        side=side,
                        decision_index=expected_window,
                        phase="act",
                        cause=exc,
                        called_agent_ids=called,
                        submitted_sides=(),
                    ) from exc

            try:
                step_result = self.decision_session.step(
                    decisions["red"], decisions["blue"]
                )
            except NativeMultiSeatError as exc:
                cause = exc.__cause__
                if (
                    exc.code == "decision_preview_failed"
                    and isinstance(cause, CommandGatewayError)
                ):
                    side = exc.side or "red"
                    raise SeatOperationError(
                        agent_id=self.agents[side].agent_id,
                        side=side,
                        decision_index=expected_window,
                        cause=cause,
                        submitted_sides=(),
                    ) from exc
                if self._analysis_desynchronized():
                    raise LocalSeatMatchError(
                        "analysis_desynchronized",
                        {
                            "perspective": (exc.side or "blue").upper(),
                            "decision_index": expected_window,
                            "cause_type": type(cause or exc).__name__,
                            "cause_message": str(cause or exc),
                            "environment_already_advanced": True,
                        },
                    ) from exc
                raise
            self._transitions.extend(deepcopy(step_result.transitions))
            processed += 1
        return processed

    def _decision_context(
        self,
        side: str,
        expected_window: int,
    ) -> AgentDecisionContext:
        context = self.decision_session.decision_context(side)
        if context.decision_index != expected_window:
            raise RuntimeError("controller observation/window mismatch")
        return context

    def _analysis_desynchronized(self) -> bool:
        try:
            self.match.gateway.manager.descriptor(self.match.session_id)
        except CommandGatewayError as exc:
            return (
                exc.code == "session_failed"
                and "analysis_desynchronized" in exc.message
            )
        return False

    def _close_agents(self) -> None:
        for side in ("red", "blue"):
            try:
                self.agents[side].close()
            except Exception:
                pass


OperationAgentMatchRunner = LocalSeatMatchRunner
OperationAgentMatchResult = LocalSeatMatchResult


def run_local_agent_match(
    match: MatchHandle,
    *,
    red_agent: LocalSeatAgent,
    blue_agent: LocalSeatAgent,
    run_namespace: str,
    output_dir: str | Path,
    max_decision_windows: int | None = None,
    overwrite: bool = False,
) -> LocalSeatMatchResult:
    """Run one product-operation match without HTTP or browser dependencies."""

    return LocalSeatMatchRunner(
        match,
        red_agent=red_agent,
        blue_agent=blue_agent,
        run_namespace=run_namespace,
    ).run(
        output_dir,
        max_decision_windows=max_decision_windows,
        overwrite=overwrite,
    )


def _nonempty_namespace(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("run_namespace must be a non-empty string")
    if value != value.strip():
        raise ValueError("run_namespace must not contain surrounding whitespace")
    return value
