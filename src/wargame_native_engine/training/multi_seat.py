"""Recommended project-native RED/BLUE decision-window environment."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from wargame_native_engine.agents.seat import (
    AgentDecisionContext,
    LocalSeatAgent,
    SeatActionCapabilities,
    SeatDecision,
)
from wargame_native_engine.gateway import CommandGatewayError, SessionPublication
from wargame_native_engine.io.serialization import json_clone
from wargame_native_engine.operations import ProductOperationService
from wargame_native_engine.operations.facade import MatchHandle
from wargame_native_engine.training.models import (
    AgentResetResult,
    AgentStepResult,
    DecisionTransition,
    SingleSeatResetResult,
    SingleSeatStepResult,
)
from wargame_native_engine.training.transitions import (
    write_decision_transitions_jsonl,
)


MatchFactory = Callable[[int, int], MatchHandle]


class NativeMultiSeatError(RuntimeError):
    """Stable lifecycle or atomic decision execution failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        side: str | None = None,
        phase: str | None = None,
    ) -> None:
        self.code = str(code)
        self.message = str(message)
        self.side = None if side is None else str(side)
        self.phase = None if phase is None else str(phase)
        super().__init__(f"{self.code}:{self.message}")


class NativeDecisionSession:
    """Shared decision executor used by Agent runners and training adapters.

    It owns no Engine or WorldState and advances only through the product
    operation services attached to one existing ``MatchHandle``.
    """

    def __init__(
        self,
        match: MatchHandle,
        *,
        episode_id: str,
        seed: int,
    ) -> None:
        if not isinstance(match, MatchHandle):
            raise TypeError("match must be a MatchHandle")
        if not isinstance(episode_id, str) or not episode_id:
            raise ValueError("episode_id must be non-empty")
        if type(seed) is not int:
            raise TypeError("seed must be an exact int")
        self.match = match
        self.episode_id = episode_id
        self.seed = seed
        self.controllers = {
            "red": match.bind_controller(
                "red", controller_id=match.red_controller_id
            ),
            "blue": match.bind_controller(
                "blue", controller_id=match.blue_controller_id
            ),
        }
        self.product_services = {
            side: ProductOperationService(controller)
            for side, controller in self.controllers.items()
        }
        self._failed = False

    def reset_result(self) -> AgentResetResult:
        self._require_synchronized()
        views = self._views()
        capabilities = self._capabilities(views)
        descriptor = self.match.status
        return AgentResetResult(
            self.episode_id,
            _observations(views),
            capabilities,
            self._decision_index(),
            descriptor.frame_index,
            descriptor.sim_time_ms,
            self.seed,
            self.match.bundle.scenario_identity.scenario_id,
            self.match.bundle.scenario_identity.sha256,
            self.match.bundle.map_identity.map_id,
        )

    def decision_context(self, side: str) -> AgentDecisionContext:
        self._require_synchronized()
        normalized = _side(side)
        view = self.controllers[normalized].observe()
        capabilities = self._capability(normalized, view)
        return AgentDecisionContext(
            normalized,
            capabilities.decision_index,
            view.frame_index,
            view.sim_time_ms,
            json_clone(view.observation),
            deepcopy(capabilities),
        )

    def agent_reset_context(self, side: str, agent_id: str):
        return self.match.gateway.manager.agent_reset_context(
            self.match.session_id, _side(side), agent_id
        )

    def step(
        self,
        red_decision: SeatDecision,
        blue_decision: SeatDecision,
    ) -> AgentStepResult:
        self._require_synchronized()
        decisions = {
            "red": _decision(red_decision),
            "blue": _decision(blue_decision),
        }
        before_views = self._views()
        before_capabilities = self._capabilities(before_views)
        decision_index = self._decision_index()
        if any(
            value.decision_index != decision_index
            for value in before_capabilities.values()
        ):
            raise NativeMultiSeatError(
                "decision_snapshot_mismatch",
                "side capability snapshots do not share one window",
            )

        previews = {}
        for side in ("red", "blue"):
            try:
                previews[side] = self.product_services[side].preview_decision(
                    decisions[side],
                    request_id=(
                        f"native-agent:{self.episode_id}:"
                        f"{decision_index}:{side}:preview"
                    ),
                )
            except BaseException as exc:
                raise NativeMultiSeatError(
                    "decision_preview_failed",
                    f"{type(exc).__name__}:{exc}",
                    side=side,
                    phase="preview",
                ) from exc

        executions = {}
        submitted: list[str] = []
        for side in ("red", "blue"):
            try:
                executions[side] = self.product_services[side].execute_decision(
                    previews[side].preview_token,
                    request_id=(
                        f"native-agent:{self.episode_id}:"
                        f"{decision_index}:{side}:execute"
                    ),
                )
                submitted.append(side)
            except BaseException as exc:
                self._failed = True
                raise NativeMultiSeatError(
                    "decision_execution_failed_reset_required",
                    f"{type(exc).__name__}:{exc};submitted={','.join(submitted)}",
                    side=side,
                    phase="execute",
                ) from exc
            if side == "red" and executions[side].advanced:
                self._failed = True
                raise NativeMultiSeatError(
                    "red_submission_advanced",
                    "RED advanced before BLUE submission",
                    side=side,
                    phase="execute",
                )
            if side == "blue" and not executions[side].advanced:
                self._failed = True
                raise NativeMultiSeatError(
                    "joint_window_not_advanced",
                    "complete decision window did not advance",
                    side=side,
                    phase="execute",
                )

        advance = self.match.gateway.last_advance(
            self.match.session_id, self.match.red_controller_id
        )
        if advance is None or advance.decision_window_index != decision_index:
            self._failed = True
            raise NativeMultiSeatError(
                "decision_advance_evidence_missing",
                "Gateway lacks timing evidence for the completed window",
                phase="advance",
            )
        after_views = self._views()
        after_capabilities = self._capabilities(after_views)
        terminated = bool(after_views["red"].terminated)
        truncated = False if terminated else bool(after_views["red"].truncated)
        rewards = _rewards(
            terminated=terminated,
            truncated=truncated,
            winner=after_views["red"].winner,
        )
        transitions = tuple(
            DecisionTransition(
                self.episode_id,
                decision_index,
                side,
                json_clone(before_views[side].observation),
                deepcopy(before_capabilities[side]),
                decisions[side],
                rewards[side],
                json_clone(after_views[side].observation),
                deepcopy(after_capabilities[side]),
                terminated,
                truncated,
                self.seed,
                self.match.bundle.scenario_identity.scenario_id,
                self.match.bundle.scenario_identity.sha256,
                self.match.bundle.map_identity.map_id,
                advance.first_frame_index,
                advance.last_frame_index,
                advance.sim_time_before_ms,
                advance.sim_time_after_ms,
            )
            for side in ("red", "blue")
        )
        next_index = (
            decision_index
            if terminated or truncated
            else self._decision_index()
        )
        return AgentStepResult(
            self.episode_id,
            _observations(after_views),
            after_capabilities,
            rewards,
            terminated,
            truncated,
            decision_index,
            next_index,
            advance.first_frame_index,
            advance.last_frame_index,
            advance.sim_time_before_ms,
            advance.sim_time_after_ms,
            after_views["red"].winner,
            after_views["red"].outcome,
            transitions,
            {
                "decision_window_index": decision_index,
                "generated_frame_count": advance.generated_frame_count,
                "fact_frame_range_contract": "closed",
                "truncation_source": "native" if truncated else None,
                "referee_labels_exposed": False,
            },
        )

    def _decision_index(self) -> int:
        return self.controllers["red"].window().decision_window_index

    def _views(self):
        return {
            side: self.controllers[side].observe()
            for side in ("red", "blue")
        }

    def _capabilities(self, views) -> dict[str, SeatActionCapabilities]:
        return {
            side: self._capability(side, views[side])
            for side in ("red", "blue")
        }

    def _capability(self, side: str, view) -> SeatActionCapabilities:
        index = view.step_index
        actors = () if view.terminal else _friendly_actor_ids(view.observation)
        summary = (
            _empty_capability_summary()
            if view.terminal
            else self.product_services[side].capabilities(actors)
        )
        operations = summary.get("operations", {})
        operation_types = tuple(
            kind
            for kind, value in operations.items()
            if isinstance(value, Mapping)
            and int(value.get("applicable_count", 0)) > 0
        )
        return SeatActionCapabilities(
            side,
            index,
            actors,
            operation_types,
            () if view.terminal else view.valid_actions,
            summary,
        )

    def _require_synchronized(self) -> None:
        if self._failed:
            raise NativeMultiSeatError(
                "native_multi_seat_failed_reset_required",
                "decision execution failed; create or reset the episode",
            )


class NativeMultiSeatEnv:
    """Recommended project-native two-seat Agent environment."""

    def __init__(
        self,
        match_factory: MatchFactory,
        *,
        max_decision_windows: int | None = None,
    ) -> None:
        if not callable(match_factory):
            raise TypeError("match_factory must be callable")
        if max_decision_windows is not None and (
            type(max_decision_windows) is not int
            or max_decision_windows <= 0
        ):
            raise ValueError("max_decision_windows must be positive or None")
        self._match_factory = match_factory
        self.max_decision_windows = max_decision_windows
        self._match: MatchHandle | None = None
        self._session: NativeDecisionSession | None = None
        self._transitions: list[DecisionTransition] = []
        self._decision_count = 0
        self._episode_sequence = 0
        self._ended = False
        self._closed = False

    def reset(self, seed: int = 42) -> AgentResetResult:
        self._require_open()
        if type(seed) is not int:
            raise TypeError("seed must be an exact int")
        self._close_match()
        self._episode_sequence += 1
        match = self._match_factory(seed, self._episode_sequence)
        if not isinstance(match, MatchHandle):
            raise TypeError("match_factory must return MatchHandle")
        self._match = match
        episode_id = (
            f"{match.session_id}-episode-{self._episode_sequence:06d}"
        )
        self._session = NativeDecisionSession(
            match, episode_id=episode_id, seed=seed
        )
        self._transitions.clear()
        self._decision_count = 0
        self._ended = bool(match.status.terminal)
        return self._session.reset_result()

    def step(
        self,
        red_decision: SeatDecision,
        blue_decision: SeatDecision,
    ) -> AgentStepResult:
        self._require_active()
        result = self._require_session().step(red_decision, blue_decision)
        self._decision_count += 1
        training_truncated = bool(
            not result.terminated
            and not result.truncated
            and self.max_decision_windows is not None
            and self._decision_count >= self.max_decision_windows
        )
        if training_truncated:
            transitions = tuple(
                replace(value, truncated=True, reward=0.0)
                for value in result.transitions
            )
            info = json_clone(result.info)
            info["truncation_source"] = "training_max_decision_windows"
            result = replace(
                result,
                truncated=True,
                rewards={"red": 0.0, "blue": 0.0},
                transitions=transitions,
                info=info,
            )
        self._transitions.extend(deepcopy(result.transitions))
        self._ended = result.terminated or result.truncated
        return deepcopy(result)

    def decision_context(self, side: str) -> AgentDecisionContext:
        self._require_active()
        return deepcopy(self._require_session().decision_context(side))

    def agent_reset_context(self, side: str, agent_id: str):
        self._require_initialized()
        return deepcopy(
            self._require_session().agent_reset_context(side, agent_id)
        )

    @property
    def transitions(self) -> tuple[DecisionTransition, ...]:
        self._require_initialized()
        return deepcopy(tuple(self._transitions))

    @property
    def decision_window_count(self) -> int:
        return self._decision_count

    @property
    def match(self) -> MatchHandle:
        self._require_initialized()
        assert self._match is not None
        return self._match

    def write_transitions(self, path: str | Path) -> Path:
        return write_decision_transitions_jsonl(path, self.transitions)

    def publish(
        self,
        output_dir: str | Path,
        *,
        overwrite: bool = False,
    ) -> SessionPublication:
        self._require_initialized()
        if not self._ended:
            raise NativeMultiSeatError(
                "native_multi_seat_active", "active episode cannot be published"
            )
        return self.match.publish(
            output_dir,
            truncated=not self.match.status.terminal,
            overwrite=overwrite,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._close_match()
        self._closed = True

    def _close_match(self) -> None:
        if self._match is not None:
            self._match.close()
        self._match = None
        self._session = None

    def _require_session(self) -> NativeDecisionSession:
        if self._session is None:
            raise NativeMultiSeatError(
                "native_multi_seat_not_reset", "call reset before use"
            )
        return self._session

    def _require_initialized(self) -> None:
        self._require_open()
        self._require_session()

    def _require_active(self) -> None:
        self._require_initialized()
        if self._ended:
            raise NativeMultiSeatError(
                "native_multi_seat_ended_reset_required",
                "episode ended; call reset before another step",
            )

    def _require_open(self) -> None:
        if self._closed:
            raise NativeMultiSeatError(
                "native_multi_seat_closed", "environment is closed"
            )


class SingleSeatTrainingAdapter:
    """One controlled seat over the recommended two-seat environment."""

    def __init__(
        self,
        environment: NativeMultiSeatEnv,
        *,
        controlled_side: str,
        opponent: LocalSeatAgent,
    ) -> None:
        if not isinstance(environment, NativeMultiSeatEnv):
            raise TypeError("environment must be NativeMultiSeatEnv")
        self.environment = environment
        self.controlled_side = _side(controlled_side)
        self.opponent_side = (
            "blue" if self.controlled_side == "red" else "red"
        )
        self.opponent = opponent
        self._reset = False
        self._opponent_decision_index: int | None = None
        self._opponent_decision: SeatDecision | None = None

    def reset(self, seed: int = 42) -> SingleSeatResetResult:
        result = self.environment.reset(seed)
        try:
            self.opponent.reset(self.environment.agent_reset_context(
                self.opponent_side, self.opponent.agent_id
            ))
        except BaseException as exc:
            raise NativeMultiSeatError(
                "single_seat_opponent_reset_failed",
                f"{type(exc).__name__}:{exc}",
                side=self.opponent_side,
                phase="reset",
            ) from exc
        self._reset = True
        self._opponent_decision_index = None
        self._opponent_decision = None
        return SingleSeatResetResult(
            json_clone(result.observations[self.controlled_side]),
            deepcopy(result.capabilities[self.controlled_side]),
            {
                "episode_id": result.episode_id,
                "decision_index": result.decision_index,
                "fact_frame_index": result.fact_frame_index,
                "sim_time_ms": result.sim_time_ms,
                "side": self.controlled_side,
            },
        )

    def step(self, decision: SeatDecision) -> SingleSeatStepResult:
        if not self._reset:
            raise NativeMultiSeatError(
                "single_seat_not_reset", "call reset before use"
            )
        controlled_decision = _decision(decision)
        context = self.environment.decision_context(self.opponent_side)
        if self._opponent_decision_index == context.decision_index:
            assert self._opponent_decision is not None
            opponent_decision = deepcopy(self._opponent_decision)
        else:
            try:
                opponent_decision = self.opponent.act(context=deepcopy(context))
                opponent_decision = _decision(opponent_decision)
            except BaseException as exc:
                self._reset = False
                raise NativeMultiSeatError(
                    "single_seat_opponent_act_failed_reset_required",
                    f"{type(exc).__name__}:{exc}",
                    side=self.opponent_side,
                    phase="act",
                ) from exc
            self._opponent_decision_index = context.decision_index
            self._opponent_decision = deepcopy(opponent_decision)
        decisions = {
            self.controlled_side: controlled_decision,
            self.opponent_side: opponent_decision,
        }
        result = self.environment.step(decisions["red"], decisions["blue"])
        self._opponent_decision_index = None
        self._opponent_decision = None
        transition = next(
            value
            for value in result.transitions
            if value.side == self.controlled_side
        )
        return SingleSeatStepResult(
            json_clone(result.observations[self.controlled_side]),
            deepcopy(result.capabilities[self.controlled_side]),
            result.rewards[self.controlled_side],
            result.terminated,
            result.truncated,
            {
                **json_clone(result.info),
                "episode_id": result.episode_id,
                "decision_index": result.decision_index,
                "fact_frame_range": [
                    result.fact_frame_start, result.fact_frame_end
                ],
                "sim_time_range_ms": [
                    result.sim_time_start_ms, result.sim_time_end_ms
                ],
                "winner": result.winner,
                "outcome": result.outcome,
                "side": self.controlled_side,
            },
            deepcopy(transition),
        )

    def close(self) -> None:
        try:
            self.opponent.close()
        finally:
            self.environment.close()


def mark_transition_pair_truncated(
    values: Sequence[DecisionTransition],
) -> tuple[DecisionTransition, ...]:
    """Mark a runner-level final decision as a non-Native truncation."""

    if not values:
        return ()
    return tuple(replace(value, truncated=True, reward=0.0) for value in values)


def _side(value: str) -> str:
    side = str(value).lower()
    if side not in {"red", "blue"}:
        raise ValueError("side must be RED or BLUE")
    return side


def _decision(value: SeatDecision) -> SeatDecision:
    if not isinstance(value, SeatDecision):
        raise TypeError("Agent decision must be a SeatDecision")
    return deepcopy(value)


def _friendly_actor_ids(observation: Mapping[str, Any]) -> tuple[int, ...]:
    values = observation.get("friendly_operators", ())
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise RuntimeError("side observation friendly_operators is invalid")
    result = []
    for value in values:
        if not isinstance(value, Mapping):
            raise RuntimeError("side observation friendly operator is invalid")
        unit_id = value.get("operator_id")
        if type(unit_id) is not int or unit_id < 0:
            raise RuntimeError("side observation operator_id is invalid")
        result.append(unit_id)
    return tuple(sorted(result))


def _empty_capability_summary() -> dict[str, object]:
    return {
        "selected_count": 0,
        "selected_units": [],
        "operations": {},
        "shoot_target_ids": [],
    }


def _observations(views) -> dict[str, Mapping[str, Any]]:
    return {
        side: json_clone(views[side].observation)
        for side in ("red", "blue")
    }


def _rewards(
    *,
    terminated: bool,
    truncated: bool,
    winner: str | None,
) -> dict[str, float]:
    if not terminated or truncated or winner not in {"red", "blue"}:
        return {"red": 0.0, "blue": 0.0}
    loser = "blue" if winner == "red" else "red"
    return {winner: 1.0, loser: -1.0}
