"""Minimum pure-Python training environment over the local CommandGateway."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
import tempfile
from typing import Any, Mapping

from wargame_native_engine.agents.protocol import Agent, AgentAction
from wargame_native_engine.gateway import (
    CommandGateway,
    ControllerView,
    SessionManager,
    SessionPublication,
    SimulationAdvance,
)
from wargame_native_engine.operations.controller import SideController
from wargame_native_engine.operations.proposals import ActionProposal
from wargame_native_engine.training.action_encoder import NativeActionEncoder
from wargame_native_engine.training.models import (
    EncodedActionSpace,
    TrainingReset,
    TrainingStep,
)
from wargame_native_engine.training.observation_encoder import (
    SideObservationEncoder,
)


class NativeTrainingError(RuntimeError):
    """Stable lifecycle or opponent failure at the training boundary."""


class NativeTrainingEnv:
    """One controlled side, one opponent and one Gateway decision window."""

    def __init__(
        self,
        environment_factory,
        *,
        controlled_side: str,
        opponent: Agent,
        storage_root: str | Path | None = None,
        max_actions: int = 256,
        max_steps: int | None = None,
        delta_time_seconds: int | float = 20,
        decision_interval_ms: int | None = None,
        frame_interval_ms: int | None = None,
    ) -> None:
        side = str(controlled_side).lower()
        if side not in {"red", "blue"}:
            raise ValueError("controlled_side must be RED or BLUE")
        if not isinstance(opponent.agent_id, str) or not opponent.agent_id.strip():
            raise ValueError("opponent agent_id must be a non-empty string")
        if max_steps is not None and (
            type(max_steps) is not int or max_steps <= 0
        ):
            raise ValueError("max_steps must be a positive exact int or None")
        if isinstance(delta_time_seconds, bool) or not isinstance(
            delta_time_seconds, (int, float)
        ) or delta_time_seconds <= 0:
            raise ValueError("delta_time_seconds must be a positive number")
        self.controlled_side = side
        self.opponent_side = "blue" if side == "red" else "red"
        self.opponent = opponent
        self.max_steps = max_steps
        self.action_encoder = NativeActionEncoder(max_actions)
        self.observation_encoder = SideObservationEncoder()
        if storage_root is None:
            self._temporary_storage = tempfile.TemporaryDirectory(
                prefix="wargame-native-training."
            )
            storage = Path(self._temporary_storage.name)
        else:
            self._temporary_storage = None
            storage = Path(storage_root)
        self._manager = SessionManager(environment_factory, storage_root=storage)
        self._gateway = CommandGateway(
            self._manager,
            delta_time_seconds=delta_time_seconds,
            decision_interval_ms=decision_interval_ms,
            frame_interval_ms=frame_interval_ms,
        )
        self._session_id: str | None = None
        self._controller: SideController | None = None
        self._controller_ids = {
            "red": "native-training-red",
            "blue": "native-training-blue",
        }
        self._step_count = 0
        self._ended = False
        self._failed = False
        self._closed = False
        self._episode_sequence = 0

    def reset(self, seed: int = 42) -> TrainingReset:
        self._require_open()
        if type(seed) is not int:
            raise TypeError("seed must be an exact int")
        if self._session_id is not None:
            self._gateway.close_session(self._session_id)
            self._session_id = None
            self._controller = None
        descriptor = self._gateway.create_session(
            red_controller_id=self._controller_ids["red"],
            blue_controller_id=self._controller_ids["blue"],
            seed=seed,
            record=True,
        )
        self._session_id = descriptor.session_id
        self._controller = SideController(
            self._gateway,
            descriptor.session_id,
            self._controller_ids[self.controlled_side],
        )
        self._step_count = 0
        self._ended = descriptor.terminal
        self._failed = False
        self._episode_sequence += 1
        try:
            context = self._manager.agent_reset_context(
                descriptor.session_id,
                self.opponent_side,
                self.opponent.agent_id,
            )
            self.opponent.reset(deepcopy(context))
        except BaseException as exc:
            self._failed = True
            raise NativeTrainingError(
                "training_opponent_reset_failed:"
                f"{self.opponent.agent_id}:{type(exc).__name__}:{exc}"
            ) from exc
        view = self._controlled_view()
        return TrainingReset(
            self.observation_encoder.encode(view),
            self._action_space(view),
            self._info(view),
        )

    def step(self, action_id: int | None) -> TrainingStep:
        """Compatibility adapter selecting zero or one action this window."""

        return self.step_batch(() if action_id is None else (action_id,))

    def step_batch(self, action_ids: Sequence[int]) -> TrainingStep:
        """Advance one decision window using an ordered controlled-side batch."""

        self._require_active()
        before = self._controlled_view()
        space = self._action_space(before)
        controlled_action_ids = self.action_encoder.resolve_batch(
            space, action_ids
        )
        opponent_action_ids = self._opponent_action_ids()
        by_side = {
            self.controlled_side: controlled_action_ids,
            self.opponent_side: opponent_action_ids,
        }
        window_index = before.step_index
        try:
            for side in ("red", "blue"):
                request_id = (
                    f"training:{self._episode_sequence}:"
                    f"{window_index}:{side}"
                )
                if side == self.controlled_side:
                    controller = self._require_controller()
                    controller.clear_staged()
                    for action_id in by_side[side]:
                        controller.stage_action(
                            action_id, expected_window=window_index
                        )
                    controller.submit_batch(
                        request_id=request_id,
                        expected_window=window_index,
                    )
                else:
                    self._gateway.submit_batch(
                        self._require_session_id(),
                        self._controller_ids[side],
                        request_id=request_id,
                        expected_window=window_index,
                        ordered_action_ids=by_side[side],
                    )
        except BaseException:
            self._failed = True
            raise
        self._step_count += 1
        view = self._controlled_view()
        advance = self._gateway.last_advance(
            self._require_session_id(),
            self._controller_ids[self.controlled_side],
        )
        if advance is None or advance.decision_window_index != window_index:
            self._failed = True
            raise NativeTrainingError("training_window_advance_evidence_missing")
        terminated = bool(view.terminated)
        training_truncated = bool(
            not terminated
            and not view.truncated
            and self.max_steps is not None
            and self._step_count >= self.max_steps
        )
        truncated = False if terminated else bool(
            view.truncated or training_truncated
        )
        self._ended = terminated or truncated
        return TrainingStep(
            self.observation_encoder.encode(view),
            self._reward(view),
            terminated,
            truncated,
            self._info(
                view,
                training_truncated=training_truncated,
                advance=advance,
            ),
            self._action_space(view),
        )

    @property
    def valid_action_mask(self) -> tuple[bool, ...]:
        self._require_active()
        return tuple(self._action_space(self._controlled_view()).mask)

    @property
    def current_action_space(self) -> EncodedActionSpace:
        """Current frozen action IDs, including server route proposals."""

        self._require_active()
        return self._action_space(self._controlled_view())

    def plan_move(
        self,
        unit_id: int,
        destination_hex: int,
        *,
        request_id: str,
        movement_mode: str = "NORMAL",
    ) -> ActionProposal:
        """Optional destination adapter using the same Operations planner."""

        self._require_active()
        return self._require_controller().plan_move(
            unit_id,
            destination_hex,
            request_id=request_id,
            movement_mode=movement_mode,
        )

    def publish(
        self,
        output_dir: str | Path,
        *,
        overwrite: bool = False,
    ) -> SessionPublication:
        self._require_initialized()
        if self._failed:
            raise NativeTrainingError("training_episode_failed")
        view = self._controlled_view()
        publication = self._gateway.publish_session(
            self._require_session_id(),
            self._controller_ids[self.controlled_side],
            str(output_dir),
            truncated=not view.terminal,
            overwrite=overwrite,
        )
        self._ended = True
        return publication

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._session_id is not None:
                self._gateway.close_session(self._session_id)
                self._session_id = None
                self._controller = None
        finally:
            try:
                self.opponent.close()
            finally:
                if self._temporary_storage is not None:
                    self._temporary_storage.cleanup()
                self._closed = True

    def _opponent_action_ids(self) -> tuple[str, ...]:
        session_id = self._require_session_id()
        try:
            observation, valid_actions, context = self._manager.agent_step_input(
                session_id,
                self.opponent_side,
                self.opponent.agent_id,
            )
            act_batch = getattr(self.opponent, "act_batch", None)
            if callable(act_batch):
                selected = act_batch(
                    observation=deepcopy(observation),
                    valid_actions=deepcopy(valid_actions),
                    context=deepcopy(context),
                )
                selections = _require_agent_action_sequence(selected)
            else:
                selection = self.opponent.act(
                    observation=deepcopy(observation),
                    valid_actions=deepcopy(valid_actions),
                    context=deepcopy(context),
                )
                selections = () if selection is None else (selection,)
            return _validate_opponent_actions(
                self.opponent_side,
                selections,
                valid_actions,
            )
        except BaseException as exc:
            self._failed = True
            raise NativeTrainingError(
                "training_opponent_act_failed:"
                f"{self.opponent.agent_id}:{self._step_count}:"
                f"{type(exc).__name__}:{exc}"
            ) from exc

    def _controlled_view(self) -> ControllerView:
        return self._require_controller().observe()

    def _action_space(self, view: ControllerView) -> EncodedActionSpace:
        return self.action_encoder.encode(view.valid_actions)

    def _reward(self, view: ControllerView) -> float:
        if not view.terminated or view.winner is None:
            return 0.0
        return 1.0 if view.winner == self.controlled_side else -1.0

    def _info(
        self,
        view: ControllerView,
        *,
        training_truncated: bool = False,
        advance: SimulationAdvance | None = None,
    ) -> Mapping[str, Any]:
        return {
            "session_id": view.session_id,
            "perspective": view.perspective,
            "step_index": view.step_index,
            "decision_window_index": view.step_index,
            "processed_decision_window_index": (
                None if advance is None else advance.decision_window_index
            ),
            "frame_range": (
                None
                if advance is None
                else [advance.first_frame_index, advance.last_frame_index]
            ),
            "generated_frame_count": (
                0 if advance is None else advance.generated_frame_count
            ),
            "sim_time_range_ms": (
                None
                if advance is None
                else [advance.sim_time_before_ms, advance.sim_time_after_ms]
            ),
            "frame_index": view.frame_index,
            "sim_time_ms": view.sim_time_ms,
            "winner": view.winner,
            "outcome": view.outcome,
            "truncation_source": (
                "native"
                if view.truncated
                else "training_max_steps" if training_truncated else None
            ),
            "referee_labels_exposed": False,
        }

    def _require_session_id(self) -> str:
        if self._session_id is None:
            raise NativeTrainingError("training_env_not_reset")
        return self._session_id

    def _require_controller(self) -> SideController:
        if self._controller is None:
            raise NativeTrainingError("training_env_not_reset")
        return self._controller

    def _require_initialized(self) -> None:
        self._require_open()
        self._require_session_id()

    def _require_active(self) -> None:
        self._require_initialized()
        if self._failed:
            raise NativeTrainingError("training_episode_failed")
        if self._ended:
            raise NativeTrainingError("training_episode_ended_reset_required")

    def _require_open(self) -> None:
        if self._closed:
            raise NativeTrainingError("training_env_closed")


def _require_agent_action_sequence(value) -> tuple[AgentAction, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("opponent act_batch must return a sequence")
    return tuple(value)


def _validate_opponent_actions(
    side: str,
    selected: Sequence[AgentAction],
    valid_actions,
) -> tuple[str, ...]:
    legal = {item.action_id: item for item in valid_actions}
    result: list[str] = []
    seen: set[str] = set()
    for value in selected:
        if not isinstance(value, AgentAction):
            raise TypeError(
                "opponent Agent selections must contain AgentAction values"
            )
        if value.action.side != side:
            raise ValueError(f"opponent_action_side_mismatch:{side}")
        action_id = value.valid_action_id
        if action_id not in legal:
            raise ValueError(f"opponent_action_not_valid:{side}")
        if action_id in seen:
            raise ValueError(f"opponent_action_duplicate:{side}:{action_id}")
        seen.add(action_id)
        result.append(action_id)
    return tuple(result)
