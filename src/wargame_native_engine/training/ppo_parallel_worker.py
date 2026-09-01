"""Spawn-safe environment-only worker for synchronous Native PPO collection."""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import sys
import time
import traceback
from typing import Callable, Mapping, Sequence

from wargame_native_engine.agents.baselines import NoOpAgent
from wargame_native_engine.training.env import NativeTrainingEnv
from wargame_native_engine.training.learner_preprocessor import (
    LearnerObservationPreprocessor, ProcessedLearnerObservation,
)
from wargame_native_engine.training.ppo_parallel_protocol import (
    PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION,
    CoordinatorActionCommand,
    WorkerDecisionEnvelope,
    WorkerEpisodeResult,
    WorkerError,
    WorkerInitRequest,
    WorkerReady,
    WorkerResetRequest,
    WorkerResetResult,
    WorkerShutdown,
    WorkerShutdownAck,
    WorkerTransitionResult,
    build_worker_decision_envelope,
)
from wargame_native_engine.training.reward_replay import write_reward_records_jsonl
from wargame_native_engine.training.training_seed import TrainingSeedStream


def parallel_environment_worker_main(connection, environment_factory: Callable[[], NativeTrainingEnv]) -> None:
    """Top-level spawn target. It never imports PPO core, model, optimizer or torch."""

    worker_index = env_index = -1
    run_id = "UNINITIALIZED"
    env: NativeTrainingEnv | None = None
    try:
        init = connection.recv()
        if not isinstance(init, WorkerInitRequest):
            raise _WorkerContract("INIT_MESSAGE_INVALID", "expected WorkerInitRequest")
        run_id, worker_index, env_index = init.run_id, init.worker_index, init.env_index
        _validate_init(init)
        env = environment_factory()
        if not isinstance(env, NativeTrainingEnv):
            raise _WorkerContract("ENV_FACTORY_INVALID", "factory did not return NativeTrainingEnv")
        state = _WorkerState(init, env)
        ready = state.initialize()
        connection.send(ready)
        while True:
            message = connection.recv()
            if isinstance(message, WorkerShutdown):
                state.validate_common(message.protocol_version, message.run_id)
                connection.send(WorkerShutdownAck(
                    PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION,
                    run_id, worker_index, env_index,
                ))
                break
            try:
                if isinstance(message, CoordinatorActionCommand):
                    connection.send(state.act(message))
                elif isinstance(message, WorkerResetRequest):
                    connection.send(state.reset(message))
                else:
                    raise _WorkerContract("MESSAGE_TYPE_INVALID", type(message).__name__)
            except _WorkerContract as exc:
                connection.send(WorkerError(
                    PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION, run_id,
                    worker_index, env_index,
                    getattr(message, "request_id", None), exc.code, exc.detail,
                ))
    except EOFError:
        pass
    except BaseException as exc:
        try:
            connection.send(WorkerError(
                PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION, run_id,
                worker_index, env_index, None,
                "WORKER_UNHANDLED_EXCEPTION",
                f"{type(exc).__name__}:{exc}\n{traceback.format_exc(limit=8)}",
            ))
        except BaseException:
            pass
    finally:
        if env is not None:
            try:
                env.close()
            except BaseException:
                pass
        try:
            connection.close()
        except BaseException:
            pass


class _WorkerState:
    def __init__(self, init: WorkerInitRequest, env: NativeTrainingEnv) -> None:
        self.init = init
        self.env = env
        self.preprocessor = LearnerObservationPreprocessor()
        self.episode_index = init.episode_index
        self.evaluation = init.evaluation
        self.environment_seed = 0
        self.decision_sequence = 0
        self.pending_binding = None
        self.pending_decision: WorkerDecisionEnvelope | None = None
        self.last_consumed_token: str | None = None
        self.episode_id: str | None = None
        self.episode_base = 0.0
        self.episode_shaping = 0.0
        self.episode_actions: list[str] = []
        self.episode_rewards: list[str] = []

    def initialize(self) -> WorkerReady:
        if self.preprocessor.spec.semantic_hash != self.init.expected_preprocessor_hash:
            raise _WorkerContract("PREPROCESSOR_AUTHORITY_MISMATCH", "worker preprocessor hash")
        self._validate_env_authority()
        decision = self._reset_environment(self.episode_index, self.evaluation)
        runtime = dict(self.env.runtime_authority)
        reward = self._reward_authority()
        return WorkerReady(
            PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION, self.init.run_id,
            self.init.worker_index, self.init.env_index, os.getpid(),
            runtime, reward, self.init.seed_config.content_hash,
            self.preprocessor.spec.semantic_hash, self.env.scenario_id,
            "torch" in sys.modules,
            f"pid:{os.getpid()}:session:{self.env._session_id}", decision,
        )

    def act(self, command: CoordinatorActionCommand) -> WorkerTransitionResult:
        self.validate_common(command.protocol_version, command.run_id)
        if command.worker_index != self.init.worker_index or command.env_index != self.init.env_index:
            raise _WorkerContract("WORKER_ENV_INDEX_MISMATCH", "command routed to wrong worker")
        if self.pending_decision is None or self.pending_binding is None:
            if command.binding_token == self.last_consumed_token:
                raise _WorkerContract("DUPLICATE_ACTION_COMMAND", "binding token already consumed")
            raise _WorkerContract("NO_PENDING_DECISION", "worker requires reset/decision")
        expected = self.pending_decision
        if command.binding_token == self.last_consumed_token:
            raise _WorkerContract("DUPLICATE_ACTION_COMMAND", "binding token already consumed")
        if command.request_id != expected.request_id or command.episode_index != self.episode_index:
            raise _WorkerContract("STALE_ACTION_COMMAND", "request or episode is stale")
        if command.binding_token != expected.binding_token:
            raise _WorkerContract("BINDING_TOKEN_MISMATCH", "live binding token mismatch")
        if type(command.action_index) is not int or not 0 <= command.action_index < 256 or (
            not expected.action_mask[command.action_index]
        ):
            raise _WorkerContract("MASKED_ACTION_INDEX", "action is not in worker catalog")
        if not isinstance(command.operational_delay_seconds, (int, float)) or (
            command.operational_delay_seconds < 0
        ):
            raise _WorkerContract("OPERATIONAL_DELAY_INVALID", "delay must be non-negative")
        binding = self.pending_binding
        self.pending_binding = None
        self.pending_decision = None
        self.last_consumed_token = expected.binding_token
        task_started = time.perf_counter()
        engine_started = time.perf_counter()
        # Probe-only delay is part of the observed worker critical-path
        # interval. It never changes transition semantics or any semantic
        # identity; it only makes process overlap directly measurable.
        if command.operational_delay_seconds:
            time.sleep(command.operational_delay_seconds)
        try:
            step = self.env.step_bound_action(command.action_index, binding)
        except BaseException as exc:
            raise _WorkerContract(
                "ENVIRONMENT_STEP_FAILED", f"{type(exc).__name__}:{exc}"
            ) from exc
        engine_finished = time.perf_counter()
        preprocess_started = time.perf_counter()
        next_binding = self.env.current_decision_binding
        next_processed = self.preprocessor.preprocess(next_binding.observation)
        next_envelope = None if step.terminated else self._envelope(
            next_binding, next_processed, post_truncation=bool(step.truncated),
        )
        preprocessing_seconds = time.perf_counter() - preprocess_started
        record = step.reward_record
        identity = record.transition_identity
        if (
            identity.decision_before_hash != expected.decision_hash
            or identity.action_catalog_hash != expected.catalog_hash
            or binding.action_space.action_ids[command.action_index]
            not in (identity.submitted_action_ids or ())
            or identity.decision_after_hash
            != next_binding.observation.decision_semantic_hash
        ):
            raise _WorkerContract("TRANSITION_BINDING_MISMATCH", "reward/action/Decision join")
        action_id = binding.action_space.action_ids[command.action_index]
        base, shaping = _reward_components(record.components)
        self.episode_id = identity.episode_id
        self.episode_base += base
        self.episode_shaping += shaping
        self.episode_actions.append(action_id)
        self.episode_rewards.append(record.semantic_hash)
        replay_seconds = 0.0
        episode_result = None
        if step.terminated or step.truncated:
            replay_started = time.perf_counter()
            episode_result = self._finish_episode(step)
            replay_seconds = time.perf_counter() - replay_started
        elif next_envelope is not None:
            self.pending_binding = next_binding
            self.pending_decision = next_envelope
        delta_ms = identity.sim_time_after_ms - identity.sim_time_before_ms
        gamma = self.env.discount_config.effective_gamma(delta_time_ms=delta_ms)
        return WorkerTransitionResult(
            PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION, self.init.run_id,
            self.init.worker_index, self.init.env_index, command.request_id,
            self.episode_index, identity.episode_id, expected.binding_token, command.action_index,
            action_id, float(step.reward), record.semantic_hash, base, shaping,
            step.terminated, step.truncated, record.truncation.source.value,
            gamma, identity.decision_after_hash, next_processed.values,
            next_envelope,
            not step.terminated, bool(step.terminated or step.truncated),
            episode_result, engine_finished - engine_started,
            preprocessing_seconds, replay_seconds,
            time.perf_counter() - task_started, engine_started, engine_finished,
        )

    def reset(self, request: WorkerResetRequest) -> WorkerResetResult:
        self.validate_common(request.protocol_version, request.run_id)
        if request.worker_index != self.init.worker_index or request.env_index != self.init.env_index:
            raise _WorkerContract("WORKER_ENV_INDEX_MISMATCH", "reset routed to wrong worker")
        if self.pending_decision is not None:
            raise _WorkerContract("RESET_WHILE_ACTIVE", "cannot reset active episode")
        if request.episode_index != self.episode_index + 1:
            raise _WorkerContract("RESET_EPISODE_SEQUENCE", "episode index must increment once")
        self.episode_index = request.episode_index
        self.evaluation = request.evaluation
        decision = self._reset_environment(self.episode_index, self.evaluation)
        return WorkerResetResult(
            PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION, self.init.run_id,
            self.init.worker_index, self.init.env_index, request.request_id,
            self.episode_index, self.environment_seed, decision,
        )

    def validate_common(self, protocol: str, run_id: str) -> None:
        if protocol != PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION:
            raise _WorkerContract("PROTOCOL_MISMATCH", "protocol version")
        if run_id != self.init.run_id:
            raise _WorkerContract("RUN_ID_MISMATCH", "run identity")

    def _reset_environment(self, episode_index: int, evaluation: bool) -> WorkerDecisionEnvelope:
        stream = TrainingSeedStream.EVALUATION if evaluation else TrainingSeedStream.ENVIRONMENT
        self.environment_seed = self.init.seed_config.derive_seed(
            stream, worker_index=self.init.base_worker_index,
            env_index=self.init.base_env_index + self.init.env_index,
            episode_index=episode_index,
        )
        result = self.env.reset(self.environment_seed)
        if result.info.get("training_interface_version") != "decision-v1":
            raise _WorkerContract("DECISION_INTERFACE_MISMATCH", "worker requires Decision-v1")
        self.episode_id = None
        self.episode_base = self.episode_shaping = 0.0
        self.episode_actions.clear()
        self.episode_rewards.clear()
        binding = self.env.current_decision_binding
        started = time.perf_counter()
        processed = self.preprocessor.preprocess(binding.observation)
        _ = time.perf_counter() - started
        envelope = self._envelope(binding, processed, post_truncation=False)
        self.pending_binding = binding
        self.pending_decision = envelope
        self.last_consumed_token = None
        return envelope

    def _envelope(self, binding, processed: ProcessedLearnerObservation, *, post_truncation: bool) -> WorkerDecisionEnvelope:
        request_id = self.decision_sequence
        self.decision_sequence += 1
        return build_worker_decision_envelope(
            protocol_version=PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION,
            run_id=self.init.run_id, worker_index=self.init.worker_index,
            env_index=self.init.env_index, request_id=request_id,
            episode_index=self.episode_index, side=self.env.controlled_side,
            decision_window_index=binding.action_space.decision_window_index,
            frame_index=binding.action_space.frame_index,
            sim_time_ms=binding.action_space.sim_time_ms,
            decision_hash=binding.observation.decision_semantic_hash,
            catalog_hash=binding.action_space.catalog_hash,
            action_encoder_hash=binding.action_space.encoder_contract_hash,
            preprocessor_hash=processed.preprocessor_hash,
            observation=processed.values,
            action_mask=tuple(binding.action_space.mask),
            learner_tensor_hash=processed.learner_tensor_hash,
            scenario_id=self.env.scenario_id,
            post_truncation_observation=post_truncation,
        )

    def _finish_episode(self, step) -> WorkerEpisodeResult:
        if self.episode_id is None:
            raise _WorkerContract("EPISODE_ID_MISSING", "completed episode identity")
        verification = self.env.verify_reward_rederivation()
        if not verification.matches:
            raise _WorkerContract("REWARD_REDERIVATION_FAILED", "reward sequence")
        winner = step.info.get("winner")
        winner = None if winner is None else str(winner).lower()
        outcome = (
            "TRUNCATED" if step.truncated else
            "WIN" if winner == self.env.controlled_side else
            "LOSS" if winner in {"red", "blue"} else "DRAW"
        )
        replay_path = None
        if self.init.artifact_root is not None:
            root = (
                Path(self.init.artifact_root) / f"env-{self.init.env_index:03d}"
                / f"episode-{self.episode_index:06d}"
            )
            publication = self.env.publish(root, overwrite=False)
            write_reward_records_jsonl(
                root / "joint_reward_records.native.jsonl", self.env.reward_records
            )
            replay_path = str(publication.replay_path)
        record = step.reward_record
        return WorkerEpisodeResult(
            self.episode_id, self.episode_index, self.environment_seed,
            outcome, winner, record.terminal_reason_code,
            step.terminated, step.truncated, record.truncation.source.value,
            len(self.episode_actions), int(step.info["sim_time_ms"]),
            self.episode_base, self.episode_shaping,
            self.episode_base + self.episode_shaping,
            tuple(self.episode_actions), tuple(self.episode_rewards), True,
            replay_path,
        )

    def _validate_env_authority(self) -> None:
        env = self.env
        if env.controlled_side.upper() != self.init.trained_side:
            raise _WorkerContract("TRAINED_SIDE_MISMATCH", "worker side")
        if not isinstance(env.opponent, NoOpAgent) or env.opponent.agent_id != "native-no-op-fixed-opponent-v1":
            raise _WorkerContract("OPPONENT_AUTHORITY_MISMATCH", "worker fixed opponent")
        if env.action_encoder.contract_hash != self.init.expected_action_encoder_hash:
            raise _WorkerContract("ACTION_ENCODER_MISMATCH", "worker action encoder")
        if env.discount_config.content_hash != self.init.expected_discount_hash:
            raise _WorkerContract("DISCOUNT_AUTHORITY_MISMATCH", "worker discount")
        composite = self.init.reward_mode == "COMPOSITE_EIGHT_FACTOR"
        if (env.shaping_policy is not None) != composite:
            raise _WorkerContract("REWARD_MODE_MISMATCH", "worker reward mode")
        if composite:
            assert env.shaping_policy is not None
            if env.shaping_policy.analyzer.model_hash != self.init.expected_analyzer_hash:
                raise _WorkerContract("ANALYZER_AUTHORITY_MISMATCH", "worker analyzer")
            if env.shaping_policy.content_hash != self.init.expected_shaping_hash:
                raise _WorkerContract("SHAPING_AUTHORITY_MISMATCH", "worker shaping")

    def _reward_authority(self) -> dict[str, object]:
        shaping = self.env.shaping_policy
        return {
            "mode": self.init.reward_mode,
            "base": self.env.base_reward_policy.identity.to_dict(),
            "composite": None if shaping is None else self.env.composite_reward_policy.to_dict(),
            "shaping": None if shaping is None else shaping.to_dict(),
            "discount": self.env.discount_config.to_dict(),
        }


class _WorkerContract(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}:{detail}")
        self.code = code
        self.detail = detail


def _validate_init(value: WorkerInitRequest) -> None:
    if value.protocol_version != PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION:
        raise _WorkerContract("PROTOCOL_MISMATCH", "init protocol")
    if value.worker_index != value.env_index or value.worker_index < 0:
        raise _WorkerContract("WORKER_ENV_MAPPING", "v1 requires worker_index == env_index")
    if value.trained_side not in {"RED", "BLUE"}:
        raise _WorkerContract("TRAINED_SIDE_INVALID", value.trained_side)
    if value.reward_mode not in {"BASE_ONLY", "COMPOSITE_EIGHT_FACTOR"}:
        raise _WorkerContract("REWARD_MODE_INVALID", value.reward_mode)


def _reward_components(components: Sequence[object]) -> tuple[float, float]:
    base = shaping = None
    for component in components:
        key = (component.namespace, component.code)
        if key == ("base", "TERMINAL_OUTCOME"):
            base = float(component.amount)
        elif key == ("shaping", "EIGHT_FACTOR_POTENTIAL"):
            shaping = float(component.amount)
        else:
            raise _WorkerContract("REWARD_COMPONENT_INVALID", str(key))
    if base is None:
        raise _WorkerContract("BASE_REWARD_MISSING", "TERMINAL_OUTCOME")
    return base, 0.0 if shaping is None else shaping


__all__ = ["parallel_environment_worker_main"]
