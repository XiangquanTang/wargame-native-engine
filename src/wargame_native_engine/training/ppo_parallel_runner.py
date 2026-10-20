"""Spawn-based synchronous parallel Native environment PPO coordinator."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import multiprocessing
from multiprocessing.connection import Connection, wait
from pathlib import Path
import pickle
import time
from typing import Callable, Mapping, Sequence

from wargame_native_engine.training.eight_factor_shaping_contract import (
    load_eight_factor_shaping_policy,
)
from wargame_native_engine.training.learner_preprocessor import (
    load_learner_observation_preprocessor_spec,
)
from wargame_native_engine.training.ppo_core import (
    LearnerTransition, MaskedCategorical, NativeMaskedPPO, PPORewardIdentity,
    PPORolloutBatch, PPOUpdateMetrics, PPO_LEARNER_TRANSITION_SCHEMA_VERSION,
    masked_argmax, torch,
)
from wargame_native_engine.training.ppo_parallel_protocol import (
    PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION,
    CoordinatorActionCommand, WorkerDecisionEnvelope, WorkerError,
    WorkerInitRequest, WorkerReady, WorkerResetRequest, WorkerResetResult,
    WorkerShutdown, WorkerShutdownAck, WorkerTransitionResult,
)
from wargame_native_engine.training.ppo_parallel_runner_config import (
    PPOParallelRunnerConfig, load_ppo_parallel_runner_config,
)
from wargame_native_engine.training.ppo_parallel_worker import (
    parallel_environment_worker_main,
)
from wargame_native_engine.training.ppo_runner import (
    PPOEpisodeMetrics, PPO_EPISODE_METRICS_SCHEMA_VERSION,
)
from wargame_native_engine.training.ppo_runner_config import PPORunnerConfig, PPORewardMode
from wargame_native_engine.training.ppo_vector_runner import (
    PPOVectorEpisodeMetrics, PPOVectorEvaluationReport, PPOVectorRollout,
    VectorTransitionRecord, PPO_VECTOR_EPISODE_METRICS_SCHEMA_VERSION,
    PPO_VECTOR_EVALUATION_REPORT_SCHEMA_VERSION, PPO_VECTOR_ROLLOUT_SCHEMA_VERSION,
    _flatten_vector_rollout,
)
from wargame_native_engine.training.ppo_vector_runner_config import PPOVectorRunnerConfig
from wargame_native_engine.training.training_seed import TrainingSeedStream
from wargame_native_engine.io.serialization import canonical_hash


class PPOParallelRunnerError(RuntimeError):
    """Parallel worker, IPC, authority or synchronous collection failed closed."""


class PPOParallelWorkerTimeout(PPOParallelRunnerError):
    pass


@dataclass(frozen=True, slots=True)
class ParallelOperationalTiming:
    total_seconds: float
    policy_seconds: float
    preprocessing_worker_sum_seconds: float
    engine_worker_sum_seconds: float
    engine_critical_path_seconds: float
    barrier_seconds: float
    ipc_estimated_seconds: float
    reward_replay_worker_sum_seconds: float
    overlapping_rounds: int


@dataclass(frozen=True, slots=True)
class IPCPayloadMetrics:
    observation_bytes: int
    mask_bytes: int
    command_bytes: int
    transition_bytes: int


@dataclass(frozen=True, slots=True)
class PPOParallelRolloutCollection:
    vector_rollout: PPOVectorRollout
    batch: PPORolloutBatch
    episodes: tuple[PPOVectorEpisodeMetrics, ...]
    completion_orders: tuple[tuple[int, ...], ...]
    timing: ParallelOperationalTiming
    payload: IPCPayloadMetrics
    parallel_config_hash: str
    invalid_action_attempts: tuple[int, ...]
    stale_action_attempts: tuple[int, ...]


@dataclass(slots=True)
class _CoordinatorEnvState:
    env_index: int
    policy_generator: object
    episode_index: int = 0
    decision: WorkerDecisionEnvelope | None = None
    active: bool = False


class ParallelWorkerClient:
    """One dedicated spawn process and duplex Pipe for one semantic env index."""

    def __init__(
        self, *, context, env_index: int, factory: Callable,
        init: WorkerInitRequest, timeout_seconds: float,
    ) -> None:
        try:
            pickle.dumps(factory, protocol=pickle.HIGHEST_PROTOCOL)
        except (pickle.PickleError, TypeError, AttributeError) as exc:
            raise PPOParallelRunnerError(
                f"worker factory {env_index} is not spawn-picklable: {exc}"
            ) from exc
        parent, child = context.Pipe(duplex=True)
        self.connection: Connection = parent
        self.process = context.Process(
            target=parallel_environment_worker_main,
            args=(child, factory),
            name=f"native-ppo-env-{env_index}", daemon=False,
        )
        self.env_index = env_index
        self.timeout_seconds = timeout_seconds
        self.init = init
        self.ready: WorkerReady | None = None
        self.process.start()
        child.close()
        self.connection.send(init)

    def receive(self, timeout: float | None = None):
        wait_seconds = self.timeout_seconds if timeout is None else timeout
        if not self.connection.poll(wait_seconds):
            raise PPOParallelWorkerTimeout(
                f"worker {self.env_index} response timeout after {wait_seconds}s"
            )
        try:
            message = self.connection.recv()
        except (EOFError, OSError) as exc:
            raise PPOParallelRunnerError(
                f"worker {self.env_index} exited without response"
            ) from exc
        if isinstance(message, WorkerError):
            raise PPOParallelRunnerError(
                f"worker {message.env_index} {message.code}: {message.detail}"
            )
        return message

    def send(self, message: object) -> None:
        if not self.process.is_alive():
            raise PPOParallelRunnerError(f"worker {self.env_index} is not alive")
        self.connection.send(message)

    def shutdown(self, run_id: str, *, timeout: float = 2.0) -> None:
        if self.process.is_alive():
            try:
                self.connection.send(WorkerShutdown(
                    PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION, run_id
                ))
                if self.connection.poll(timeout):
                    message = self.connection.recv()
                    if not isinstance(message, WorkerShutdownAck):
                        raise PPOParallelRunnerError("worker shutdown acknowledgement invalid")
            except (BrokenPipeError, EOFError, OSError, PPOParallelRunnerError):
                pass
            self.process.join(timeout)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout)
        try:
            self.connection.close()
        except OSError:
            pass


class NativeParallelPPORunner:
    """Single learner/optimizer coordinating persistent spawn env workers."""

    def __init__(
        self,
        environment_factories: Sequence[Callable[[], object]],
        core: NativeMaskedPPO,
        *,
        parallel_config: PPOParallelRunnerConfig | None,
        vector_config: PPOVectorRunnerConfig,
        base_runner_config: PPORunnerConfig,
        run_namespace: str = "native-parallel-ppo-runner",
        artifact_root: str | Path | None = None,
    ) -> None:
        self.config = load_ppo_parallel_runner_config() if parallel_config is None else parallel_config
        self.vector_config = vector_config
        self.base_runner_config = base_runner_config
        self.core = core
        if self.core.preprocessor_spec.input_encoder_schema != (
            "native-decision-observation-encoder/1.0"
        ):
            raise PPOParallelRunnerError(
                "parallel runner does not support Decision-v2 observations"
            )
        self.factories = tuple(environment_factories)
        if len(self.factories) != self.config.num_envs:
            raise PPOParallelRunnerError("factory count must equal num_envs")
        if self.config.base_vector_runner.identity_hash != vector_config.content_hash:
            raise PPOParallelRunnerError("parallel/vector config mismatch")
        if self.config.ppo_config.identity_hash != core.config.content_hash or (
            vector_config.transitions_per_update != core.config.rollout_length
        ):
            raise PPOParallelRunnerError("parallel/core PPO mismatch")
        if self.config.seed_config.identity_hash != core.seed.content_hash:
            raise PPOParallelRunnerError("parallel/core seed mismatch")
        if self.config.trained_side != base_runner_config.trained_side or (
            self.config.reward_mode is not base_runner_config.reward_mode
        ):
            raise PPOParallelRunnerError("parallel/base runner mismatch")
        self.run_namespace = _text(run_namespace)
        self.run_id = canonical_hash({
            "namespace": self.run_namespace,
            "parallel_config_hash": self.config.content_hash,
            "ppo_config_hash": self.core.config.content_hash,
            "seed_hash": self.core.seed.content_hash,
        })
        self.artifact_root = None if artifact_root is None else Path(artifact_root)
        self.context = multiprocessing.get_context("spawn")
        if self.context.get_start_method() != "spawn":
            raise PPOParallelRunnerError("parallel v1 requires spawn context")
        self.states = tuple(
            _CoordinatorEnvState(index, torch.Generator(device="cpu"))
            for index in range(self.config.num_envs)
        )
        self.clients: list[ParallelWorkerClient] = []
        self.ready: tuple[WorkerReady, ...] = ()
        self.environment_steps = 0
        self.vector_steps = 0
        self.invalid_action_attempts = [0] * self.config.num_envs
        self.stale_action_attempts = [0] * self.config.num_envs
        self._started = False
        self._failed = False
        self._at_boundary = True
        self._completion_counter = 0

    @property
    def reward_identity(self) -> PPORewardIdentity:
        if not self.ready:
            raise PPOParallelRunnerError("workers are not ready")
        authority = self.ready[0].reward_authority
        if self.config.reward_mode is PPORewardMode.BASE_ONLY:
            value = authority["base"]
            return PPORewardIdentity(value["policy_id"], value["policy_version"], value["policy_hash"])
        value = authority["composite"]
        return PPORewardIdentity(value["policy_id"], value["policy_version"], value["composite_hash"])

    def start(self, *, evaluation: bool = False) -> tuple[WorkerReady, ...]:
        if self._started:
            raise PPOParallelRunnerError("parallel runner already started")
        expected_preprocessor = load_learner_observation_preprocessor_spec().semantic_hash
        shaping = (
            load_eight_factor_shaping_policy()
            if self.config.reward_mode is PPORewardMode.COMPOSITE_EIGHT_FACTOR else None
        )
        try:
            for env_index, factory in enumerate(self.factories):
                init = WorkerInitRequest(
                    PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION, self.run_id,
                    env_index, env_index, self.base_runner_config.worker_index,
                    self.base_runner_config.env_index,
                    self.config.trained_side, self.config.reward_mode.value,
                    self.core.seed, expected_preprocessor,
                    self.core.config.action_encoder.contract_hash,
                    self.core.discount.content_hash,
                    None if shaping is None else shaping.analyzer.model_hash,
                    None if shaping is None else shaping.content_hash,
                    self.states[env_index].episode_index, evaluation,
                    None if self.artifact_root is None else str(self.artifact_root),
                )
                self.clients.append(ParallelWorkerClient(
                    context=self.context, env_index=env_index, factory=factory,
                    init=init, timeout_seconds=self.config.worker_timeout_seconds,
                ))
            values = []
            for client in self.clients:
                message = client.receive()
                if not isinstance(message, WorkerReady):
                    raise PPOParallelRunnerError("worker ready handshake invalid")
                client.ready = message
                values.append(message)
            self.ready = tuple(sorted(values, key=lambda item: item.env_index))
            self._validate_ready()
            for item in self.ready:
                state = self.states[item.env_index]
                state.decision = item.initial_decision
                state.active = True
                self._seed_policy_generator(state)
            self._started = True
            return self.ready
        except BaseException:
            self._failed = True
            self.close()
            raise

    def collect_rollout(
        self, length: int | None = None, *, artificial_delays: Mapping[int, float] | None = None,
    ) -> PPOParallelRolloutCollection:
        if not self._started:
            self.start(evaluation=False)
        if self._failed:
            raise PPOParallelRunnerError("parallel runner is failed")
        target = self.core.config.rollout_length if length is None else length
        if type(target) is not int or not 0 < target <= self.core.config.rollout_length:
            raise PPOParallelRunnerError("parallel rollout target outside PPO config")
        # A prior rollout may end on the same synchronous round that terminates
        # every active episode.  Those terminal transitions belong to the prior
        # batch, so reset only now, at the next rollout boundary.  Never use a
        # reset observation as the prior terminal/truncation bootstrap value.
        boundary_inactive = tuple(
            state.env_index for state in self.states if not state.active
        )
        if boundary_inactive:
            self._reset_workers(boundary_inactive, evaluation=False)
        delays = dict(artificial_delays or {})
        self._at_boundary = False
        started = time.perf_counter()
        policy_seconds = barrier_seconds = ipc_seconds = 0.0
        engine_sum = engine_critical = preprocess_sum = replay_sum = 0.0
        overlap_rounds = 0
        payload_obs = payload_mask = payload_command = payload_transition = 0
        segments: list[list[VectorTransitionRecord]] = [[] for _ in self.states]
        episodes: list[PPOVectorEpisodeMetrics] = []
        completion_orders: list[tuple[int, ...]] = []
        committed = 0
        round_index = 0
        policy_version = self.core.update_step
        try:
            while committed < target:
                active = tuple(
                    state.env_index for state in self.states if state.active
                )[:target - committed]
                if not active:
                    raise PPOParallelRunnerError("no active worker decision")
                decisions = [self._decision(index) for index in active]
                payload_obs += sum(len(pickle.dumps(item.observation, protocol=5)) for item in decisions)
                payload_mask += sum(len(pickle.dumps(item.action_mask, protocol=5)) for item in decisions)
                observations = torch.tensor(
                    [item.observation for item in decisions], dtype=torch.float32
                )
                masks = torch.tensor([item.action_mask for item in decisions], dtype=torch.bool)
                before = time.perf_counter()
                actions, log_probs, values = self._infer(active, observations, masks, evaluation=False)
                policy_seconds += time.perf_counter() - before
                send_started = time.perf_counter()
                for row, env_index in enumerate(active):
                    decision = decisions[row]
                    command = CoordinatorActionCommand(
                        PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION, self.run_id,
                        env_index, env_index, decision.request_id,
                        decision.episode_index, decision.binding_token,
                        actions[row], float(delays.get(env_index, 0.0)),
                    )
                    payload_command += len(pickle.dumps(command, protocol=5))
                    self.clients[env_index].send(command)
                send_seconds = time.perf_counter() - send_started
                barrier_started = time.perf_counter()
                results, completion = self._gather(active, WorkerTransitionResult)
                barrier = time.perf_counter() - barrier_started
                barrier_seconds += barrier
                completion_orders.append(completion)
                ordered = [results[index] for index in active]
                payload_transition += sum(len(pickle.dumps(item, protocol=5)) for item in ordered)
                worker_max = max(item.worker_total_seconds for item in ordered)
                ipc_seconds += send_seconds + max(0.0, barrier - worker_max)
                engine_sum += sum(item.engine_seconds for item in ordered)
                engine_critical += max(item.engine_seconds for item in ordered)
                preprocess_sum += sum(item.preprocessing_seconds for item in ordered)
                replay_sum += sum(item.reward_replay_seconds for item in ordered)
                if _has_overlap(ordered):
                    overlap_rounds += 1
                next_values = self._next_values(ordered)
                for row, (env_index, result, next_value) in enumerate(zip(active, ordered, next_values)):
                    decision = decisions[row]
                    transition = self._transition(
                        decision, result, log_probs[row], values[row], next_value
                    )
                    segments[env_index].append(VectorTransitionRecord(
                        env_index, round_index, committed + row, policy_version,
                        decision.scenario_id, transition,
                    ))
                    if result.episode_result is not None:
                        episodes.append(self._episode_metrics(result))
                        self.states[env_index].active = False
                        self.states[env_index].decision = None
                    else:
                        self.states[env_index].decision = result.next_decision
                committed += len(ordered)
                self.environment_steps += len(ordered)
                self.vector_steps += 1
                round_index += 1
                if committed < target:
                    done = [index for index in active if not self.states[index].active]
                    if done:
                        self._reset_workers(done, evaluation=False)
            for segment in segments:
                if segment and segment[-1].transition.trace_continues:
                    segment[-1] = replace(
                        segment[-1], transition=replace(
                            segment[-1].transition, trace_continues=False
                        ),
                    )
            rollout = PPOVectorRollout(
                PPO_VECTOR_ROLLOUT_SCHEMA_VERSION, self.vector_config.content_hash,
                self.config.num_envs, round_index, committed,
                self.vector_config.flatten_order, policy_version,
                tuple(tuple(item) for item in segments),
            )
            batch = _flatten_vector_rollout(rollout, self.core)
            self._at_boundary = True
            return PPOParallelRolloutCollection(
                rollout, batch, tuple(episodes), tuple(completion_orders),
                ParallelOperationalTiming(
                    time.perf_counter() - started, policy_seconds, preprocess_sum,
                    engine_sum, engine_critical, barrier_seconds, ipc_seconds,
                    replay_sum, overlap_rounds,
                ),
                IPCPayloadMetrics(payload_obs, payload_mask, payload_command, payload_transition),
                self.config.content_hash, tuple(self.invalid_action_attempts),
                tuple(self.stale_action_attempts),
            )
        except BaseException:
            self._failed = True
            self.close()
            raise

    def train_update(self, length: int | None = None, **kwargs):
        collection = self.collect_rollout(length, **kwargs)
        started = time.perf_counter()
        metrics = self.core.update(collection.batch)
        return collection, metrics, time.perf_counter() - started

    def evaluate(self, episodes: int) -> PPOVectorEvaluationReport:
        if type(episodes) is not int or episodes <= 0:
            raise PPOParallelRunnerError("evaluation count must be positive")
        if not self._started:
            self.start(evaluation=True)
        before_update = self.core.update_step
        before_sample = self.core.sample_step
        before_hash = self.core.parameter_content_hash()
        before_rng = self.core.policy_sampling_generator.get_state().clone()
        results: list[PPOVectorEpisodeMetrics] = []
        try:
            while len(results) < episodes:
                wave = tuple(range(min(
                    self.config.num_envs, episodes - len(results)
                )))
                if any(not self.states[index].active for index in wave):
                    self._reset_workers(
                        tuple(index for index in wave if not self.states[index].active),
                        evaluation=True,
                    )
                pending = set(wave)
                while pending:
                    active = tuple(index for index in wave if index in pending)
                    decisions = [self._decision(index) for index in active]
                    observations = torch.tensor([item.observation for item in decisions], dtype=torch.float32)
                    masks = torch.tensor([item.action_mask for item in decisions], dtype=torch.bool)
                    actions, _logs, _values = self._infer(active, observations, masks, evaluation=True)
                    for row, env_index in enumerate(active):
                        item = decisions[row]
                        self.clients[env_index].send(CoordinatorActionCommand(
                            PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION, self.run_id,
                            env_index, env_index, item.request_id, item.episode_index,
                            item.binding_token, actions[row], 0.0,
                        ))
                    gathered, _completion = self._gather(active, WorkerTransitionResult)
                    for env_index in active:
                        result = gathered[env_index]
                        if result.episode_result is None:
                            self.states[env_index].decision = result.next_decision
                        else:
                            results.append(self._episode_metrics(result))
                            self.states[env_index].active = False
                            self.states[env_index].decision = None
                            pending.remove(env_index)
            if self.core.update_step != before_update or self.core.sample_step != before_sample:
                raise PPOParallelRunnerError("evaluation mutated PPO counters")
            if self.core.parameter_content_hash() != before_hash or not torch.equal(
                before_rng, self.core.policy_sampling_generator.get_state()
            ):
                raise PPOParallelRunnerError("evaluation mutated learner state")
            ordered = tuple(sorted(
                results,
                key=lambda item: (item.episode.environment_seed, item.env_index,
                                  item.episode.episode_index),
            ))
            return PPOVectorEvaluationReport(
                PPO_VECTOR_EVALUATION_REPORT_SCHEMA_VERSION,
                self.vector_config.content_hash, "PARALLEL_IN_MEMORY",
                self.reward_identity,
                canonical_hash([item.episode.environment_seed for item in ordered]),
                ordered, before_hash, before_update,
            )
        except BaseException:
            self._failed = True
            self.close()
            raise

    def close(self) -> None:
        clients, self.clients = self.clients, []
        for client in clients:
            client.shutdown(self.run_id)
        self._started = False

    @property
    def worker_pids(self) -> tuple[int, ...]:
        return tuple(item.pid for item in self.ready)

    def _validate_ready(self) -> None:
        if len(self.ready) != self.config.num_workers:
            raise PPOParallelRunnerError("worker ready count mismatch")
        first = self.ready[0]
        if any(item.worker_index != item.env_index for item in self.ready):
            raise PPOParallelRunnerError("worker/env mapping mismatch")
        if len({item.pid for item in self.ready}) != len(self.ready) or len({item.session_identity for item in self.ready}) != len(self.ready):
            raise PPOParallelRunnerError("worker process/session isolation failed")
        for item in self.ready:
            if item.runtime_authority != first.runtime_authority:
                raise PPOParallelRunnerError("mixed worker runtime authority")
            if item.reward_authority != first.reward_authority:
                raise PPOParallelRunnerError("mixed worker reward authority")
            if item.seed_hash != self.core.seed.content_hash:
                raise PPOParallelRunnerError("worker seed authority mismatch")
            if item.preprocessor_hash != self.core.config.observation_preprocessor.identity_hash:
                raise PPOParallelRunnerError("worker preprocessor mismatch")
            if item.initial_decision.action_encoder_hash != self.core.config.action_encoder.contract_hash:
                raise PPOParallelRunnerError("worker action encoder mismatch")
            # Spawn re-imports the parent entry module; test/CLI entry points may
            # therefore load the optional torch package in the child.  Worker
            # ownership is enforced structurally: no model/core/optimizer is
            # constructed or passed through this protocol.

    def _seed_policy_generator(self, state: _CoordinatorEnvState) -> None:
        seed = self.core.seed.derive_seed(
            TrainingSeedStream.POLICY_SAMPLING,
            worker_index=self.base_runner_config.worker_index,
            env_index=self.base_runner_config.env_index + state.env_index,
            episode_index=state.episode_index,
        )
        state.policy_generator.manual_seed(seed)

    def _infer(self, env_indices, observations, masks, *, evaluation: bool):
        was_training = self.core.model.training
        self.core.model.eval()
        try:
            with torch.no_grad():
                logits, values = self.core.model(observations)
                distribution = MaskedCategorical(logits, masks)
                if evaluation:
                    actions = masked_argmax(logits, masks)
                else:
                    actions = torch.stack([
                        MaskedCategorical(logits[row], masks[row]).sample(
                            generator=self.states[env_index].policy_generator
                        ) for row, env_index in enumerate(env_indices)
                    ])
                    self.core.sample_step += len(env_indices)
                logs = distribution.log_prob(actions)
            return tuple(map(int, actions.tolist())), tuple(map(float, logs.tolist())), tuple(map(float, values.tolist()))
        finally:
            self.core.model.train(was_training)

    def _gather(self, env_indices: Sequence[int], expected_type):
        pending = {self.clients[index].connection: index for index in env_indices}
        results = {}
        completion = []
        deadline = time.monotonic() + self.config.worker_timeout_seconds
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PPOParallelWorkerTimeout("parallel barrier response timeout")
            ready_connections = wait(tuple(pending), timeout=remaining)
            if not ready_connections:
                raise PPOParallelWorkerTimeout("parallel barrier response timeout")
            for connection in ready_connections:
                index = pending.pop(connection)
                message = self.clients[index].receive(timeout=0.0)
                if not isinstance(message, expected_type):
                    raise PPOParallelRunnerError(
                        f"worker {index} returned {type(message).__name__}, expected {expected_type.__name__}"
                    )
                results[index] = message
                completion.append(index)
        return results, tuple(completion)

    def _next_values(self, results: Sequence[WorkerTransitionResult]) -> tuple[float, ...]:
        values = [0.0] * len(results)
        rows = [index for index, item in enumerate(results) if not item.terminated]
        if rows:
            if any(results[index].next_decision is None for index in rows):
                raise PPOParallelRunnerError("bootstrap transition lacks next Decision")
            observations = torch.tensor([
                results[index].next_decision.observation for index in rows
            ], dtype=torch.float32)
            was_training = self.core.model.training
            self.core.model.eval()
            try:
                with torch.no_grad():
                    _logits, predicted = self.core.model(observations)
            finally:
                self.core.model.train(was_training)
            for row, value in zip(rows, predicted.tolist()):
                values[row] = float(value)
        return tuple(values)

    def _transition(self, decision, result, old_log_prob, old_value, next_value):
        if result.binding_token != decision.binding_token or result.request_id != decision.request_id:
            raise PPOParallelRunnerError("worker transition binding response mismatch")
        if not math.isclose(
            result.reward, result.base_reward_amount + result.shaping_reward_amount,
            rel_tol=0.0, abs_tol=1e-7,
        ):
            raise PPOParallelRunnerError("worker reward component mismatch")
        return LearnerTransition(
            PPO_LEARNER_TRANSITION_SCHEMA_VERSION,
            result.episode_id,
            self.run_namespace, decision.side, decision.observation,
            decision.action_mask, decision.decision_hash, decision.catalog_hash,
            decision.action_encoder_hash, result.action_index, result.action_id,
            old_log_prob, old_value, result.reward, result.reward_record_hash,
            result.base_reward_amount, result.shaping_reward_amount,
            result.terminated, result.truncated, result.truncation_source,
            result.effective_gamma,
            result.next_observation,
            result.next_decision_identity, next_value, not result.terminated,
            not result.terminated and not result.truncated,
            decision.preprocessor_hash,
        )

    def _episode_metrics(self, result: WorkerTransitionResult) -> PPOVectorEpisodeMetrics:
        item = result.episode_result
        assert item is not None
        episode = PPOEpisodeMetrics(
            PPO_EPISODE_METRICS_SCHEMA_VERSION, item.episode_id,
            self.run_namespace, self.config.trained_side.lower(), item.episode_index,
            item.environment_seed, item.outcome, item.winner, item.terminal_reason,
            item.terminated, item.truncated, item.truncation_source,
            item.decision_count, item.sim_time_ms, item.base_return,
            item.shaping_return, item.composite_return, self.reward_identity,
            item.action_ids, item.reward_record_hashes, item.reward_rederivation_verified,
        )
        wrapper = PPOVectorEpisodeMetrics(
            PPO_VECTOR_EPISODE_METRICS_SCHEMA_VERSION, result.env_index,
            self.ready[result.env_index].scenario_id, self._completion_counter, episode,
        )
        self._completion_counter += 1
        return wrapper

    def _reset_workers(self, env_indices: Sequence[int], *, evaluation: bool) -> None:
        for env_index in env_indices:
            state = self.states[env_index]
            request_id = 1_000_000_000 + state.episode_index
            self.clients[env_index].send(WorkerResetRequest(
                PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION, self.run_id,
                env_index, env_index, request_id, state.episode_index + 1,
                evaluation,
            ))
        results, _completion = self._gather(env_indices, WorkerResetResult)
        for env_index in sorted(env_indices):
            result = results[env_index]
            state = self.states[env_index]
            state.episode_index = result.episode_index
            state.decision = result.decision
            state.active = True
            if not evaluation:
                self._seed_policy_generator(state)

    def _decision(self, env_index: int) -> WorkerDecisionEnvelope:
        value = self.states[env_index].decision
        if value is None:
            raise PPOParallelRunnerError(f"env {env_index} has no active decision")
        return value


def _has_overlap(results: Sequence[WorkerTransitionResult]) -> bool:
    intervals = sorted(
        (item.engine_started_monotonic, item.engine_finished_monotonic)
        for item in results
    )
    return any(right[0] < left[1] for left, right in zip(intervals, intervals[1:]))


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("run_namespace must be non-empty trimmed text")
    return value


__all__ = [
    "IPCPayloadMetrics", "NativeParallelPPORunner", "PPOParallelRolloutCollection",
    "PPOParallelRunnerError", "PPOParallelWorkerTimeout",
    "ParallelOperationalTiming", "ParallelWorkerClient",
]
