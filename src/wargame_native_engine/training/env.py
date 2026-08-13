"""Minimum pure-Python training environment over the local CommandGateway."""

from __future__ import annotations

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
)
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
    """One controlled side, one injected local opponent, one Gateway step."""

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
        )
        self._session_id: str | None = None
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
        descriptor = self._gateway.create_session(
            red_controller_id=self._controller_ids["red"],
            blue_controller_id=self._controller_ids["blue"],
            seed=seed,
            record=True,
        )
        self._session_id = descriptor.session_id
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
        self._require_active()
        before = self._controlled_view()
        space = self._action_space(before)
        controlled_action_id = self.action_encoder.resolve(space, action_id)
        opponent_action_id = self._opponent_action_id()
        by_side = {
            self.controlled_side: controlled_action_id,
            self.opponent_side: opponent_action_id,
        }
        try:
            for side in ("red", "blue"):
                self._gateway.submit(
                    self._require_session_id(),
                    self._controller_ids[side],
                    request_id=(
                        f"training:{self._episode_sequence}:"
                        f"{self._step_count}:{side}"
                    ),
                    expected_step=self._step_count,
                    valid_action_id=by_side[side],
                )
        except BaseException:
            self._failed = True
            raise
        self._step_count += 1
        view = self._controlled_view()
        terminated = view.terminal
        truncated = bool(
            not terminated
            and self.max_steps is not None
            and self._step_count >= self.max_steps
        )
        self._ended = terminated or truncated
        return TrainingStep(
            self.observation_encoder.encode(view),
            self._reward(view),
            terminated,
            truncated,
            self._info(view, truncated=truncated),
            self._action_space(view),
        )

    @property
    def valid_action_mask(self) -> tuple[bool, ...]:
        self._require_active()
        return tuple(self._action_space(self._controlled_view()).mask)

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
        finally:
            try:
                self.opponent.close()
            finally:
                if self._temporary_storage is not None:
                    self._temporary_storage.cleanup()
                self._closed = True

    def _opponent_action_id(self) -> str | None:
        session_id = self._require_session_id()
        try:
            observation, valid_actions, context = self._manager.agent_step_input(
                session_id,
                self.opponent_side,
                self.opponent.agent_id,
            )
            selected = self.opponent.act(
                observation=deepcopy(observation),
                valid_actions=deepcopy(valid_actions),
                context=deepcopy(context),
            )
            return _validate_opponent_action(
                self.opponent_side,
                selected,
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
        return self._gateway.view(
            self._require_session_id(),
            self._controller_ids[self.controlled_side],
        )

    def _action_space(self, view: ControllerView) -> EncodedActionSpace:
        return self.action_encoder.encode(view.valid_actions)

    def _reward(self, view: ControllerView) -> float:
        if not view.terminal or view.winner is None:
            return 0.0
        return 1.0 if view.winner == self.controlled_side else -1.0

    def _info(
        self,
        view: ControllerView,
        *,
        truncated: bool = False,
    ) -> Mapping[str, Any]:
        return {
            "session_id": view.session_id,
            "perspective": view.perspective,
            "step_index": view.step_index,
            "winner": view.winner,
            "outcome": view.outcome,
            "truncation_source": "training_max_steps" if truncated else None,
            "referee_labels_exposed": False,
        }

    def _require_session_id(self) -> str:
        if self._session_id is None:
            raise NativeTrainingError("training_env_not_reset")
        return self._session_id

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


def _validate_opponent_action(
    side: str,
    selected: AgentAction | None,
    valid_actions,
) -> str | None:
    if selected is None:
        return None
    if not isinstance(selected, AgentAction):
        raise TypeError("opponent Agent.act must return AgentAction or None")
    if selected.action.side != side:
        raise ValueError(f"opponent_action_side_mismatch:{side}")
    if selected.valid_action_id != selected.action.action_id:
        raise ValueError("opponent valid-action identity mismatch")
    legal = {item.action_id for item in valid_actions}
    if selected.valid_action_id not in legal:
        raise ValueError(f"opponent_action_not_valid:{side}")
    return selected.valid_action_id
