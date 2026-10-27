"""Synchronous multi-environment collection for the Native masked PPO core.

The module batches only deterministic preprocessing and neural inference.  Each
NativeTrainingEnv remains a separate session/World/catalog/reward/RNG authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Mapping, Sequence

from wargame_native_engine.agents.baselines import NoOpAgent
from wargame_native_engine.training.decision_encoder import DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION
from wargame_native_engine.io.serialization import canonical_hash, file_sha256
from wargame_native_engine.training.env import (
    NativeTrainingEnv,
    NativeTrainingError,
    TrainingDecisionBinding,
)
from wargame_native_engine.training.learner_preprocessor import (
    LearnerObservationPreprocessor,
    ProcessedLearnerObservation,
)
from wargame_native_engine.training.ppo_core import (
    LearnerTransition,
    MaskedCategorical,
    NativeMaskedPPO,
    PPOCheckpointError,
    PPORewardIdentity,
    PPORolloutBatch,
    PPOUpdateMetrics,
    PPO_LEARNER_TRANSITION_SCHEMA_VERSION,
    PPO_ROLLOUT_BATCH_SCHEMA_VERSION,
    compute_gae,
    masked_argmax,
    torch,
)
from wargame_native_engine.training.ppo_runner import (
    LiveActionBinding,
    PPOEpisodeMetrics,
    PPORunnerError,
    PPO_EPISODE_METRICS_SCHEMA_VERSION,
    _atomic_json,
    _reward_components,
    _stable_object_hash,
)
from wargame_native_engine.training.ppo_runner_config import (
    PPORunnerConfig,
    PPORewardMode,
)
from wargame_native_engine.training.ppo_vector_runner_config import (
    PPOVectorRunnerConfig,
    load_ppo_vector_runner_config,
)
from wargame_native_engine.training.reward_replay import write_reward_records_jsonl
from wargame_native_engine.training.training_seed import TrainingSeedStream


PPO_VECTOR_ROLLOUT_SCHEMA_VERSION = "native-ppo-vector-rollout/1.0"
PPO_VECTOR_EPISODE_METRICS_SCHEMA_VERSION = "native-ppo-vector-episode-metrics/1.0"
PPO_VECTOR_EVALUATION_REPORT_SCHEMA_VERSION = "native-ppo-vector-evaluation-report/1.0"
PPO_VECTOR_CHECKPOINT_SCHEMA_VERSION = "native-ppo-vector-checkpoint/1.0"


class PPOVectorRunnerError(RuntimeError):
    """A vector isolation, binding, rollout, or lifecycle contract failed."""


@dataclass(frozen=True, slots=True)
class VectorLiveBinding:
    env_index: int
    batch_row: int
    selected: LiveActionBinding

    def __post_init__(self) -> None:
        if type(self.env_index) is not int or self.env_index < 0:
            raise PPOVectorRunnerError("env_index must be non-negative")
        if type(self.batch_row) is not int or self.batch_row < 0:
            raise PPOVectorRunnerError("batch_row must be non-negative")


@dataclass(frozen=True, slots=True)
class VectorTransitionRecord:
    env_index: int
    vector_step: int
    collection_index: int
    policy_update_step: int
    scenario_id: str
    transition: LearnerTransition = field(repr=False)
    semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("env_index", "vector_step", "collection_index", "policy_update_step"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise PPOVectorRunnerError(f"{name} must be non-negative")
        if not self.scenario_id:
            raise PPOVectorRunnerError("scenario_id is required")
        item = self.transition
        payload = {
            "env_index": self.env_index,
            "vector_step": self.vector_step,
            "collection_index": self.collection_index,
            "policy_update_step": self.policy_update_step,
            "scenario_id": self.scenario_id,
            "episode_id": item.episode_id,
            "decision_identity": item.decision_identity,
            "catalog_hash": item.catalog_hash,
            "action_index": item.action_index,
            "action_id": item.action_id,
            "reward_record_hash": item.reward_record_hash,
            "next_decision_identity": item.next_decision_identity,
        }
        object.__setattr__(self, "semantic_hash", canonical_hash(payload))


@dataclass(frozen=True, slots=True)
class PPOVectorRollout:
    schema_version: str
    vector_config_hash: str
    num_envs: int
    vector_steps: int
    total_transitions: int
    flatten_order: str
    policy_update_step: int
    segments: tuple[tuple[VectorTransitionRecord, ...], ...]
    semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PPO_VECTOR_ROLLOUT_SCHEMA_VERSION:
            raise PPOVectorRunnerError("unsupported vector rollout schema")
        if len(self.segments) != self.num_envs:
            raise PPOVectorRunnerError("vector rollout segment count mismatch")
        flattened = [item for segment in self.segments for item in segment]
        if len(flattened) != self.total_transitions:
            raise PPOVectorRunnerError("vector rollout transition count mismatch")
        if self.flatten_order != "ENV_MAJOR_THEN_COLLECTION_ORDER":
            raise PPOVectorRunnerError("unsupported vector flatten ordering")
        if any(
            item.env_index != env_index
            for env_index, segment in enumerate(self.segments)
            for item in segment
        ):
            raise PPOVectorRunnerError("transition stored in the wrong env segment")
        if any(item.policy_update_step != self.policy_update_step for item in flattened):
            raise PPOVectorRunnerError("rollout mixes policy parameter versions")
        payload = {
            "schema_version": self.schema_version,
            "vector_config_hash": self.vector_config_hash,
            "num_envs": self.num_envs,
            "vector_steps": self.vector_steps,
            "total_transitions": self.total_transitions,
            "flatten_order": self.flatten_order,
            "policy_update_step": self.policy_update_step,
            "segments": [[item.semantic_hash for item in segment] for segment in self.segments],
        }
        object.__setattr__(self, "semantic_hash", canonical_hash(payload))


@dataclass(frozen=True, slots=True)
class PPOVectorEpisodeMetrics:
    schema_version: str
    env_index: int
    scenario_id: str
    completion_order: int
    episode: PPOEpisodeMetrics
    semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PPO_VECTOR_EPISODE_METRICS_SCHEMA_VERSION:
            raise PPOVectorRunnerError("unsupported vector episode metrics schema")
        payload = {
            "schema_version": self.schema_version,
            "env_index": self.env_index,
            "scenario_id": self.scenario_id,
            "completion_order": self.completion_order,
            "episode": self.episode.to_dict(),
        }
        object.__setattr__(self, "semantic_hash", canonical_hash(payload))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "env_index": self.env_index,
            "scenario_id": self.scenario_id,
            "completion_order": self.completion_order,
            "episode": self.episode.to_dict(),
            "semantic_hash": self.semantic_hash,
        }


@dataclass(frozen=True, slots=True)
class VectorOperationalTiming:
    preprocessing_seconds: float
    policy_seconds: float
    environment_seconds: float
    reward_replay_seconds: float
    total_seconds: float


@dataclass(frozen=True, slots=True)
class PPORolloutVectorCollection:
    vector_rollout: PPOVectorRollout
    batch: PPORolloutBatch
    episodes: tuple[PPOVectorEpisodeMetrics, ...]
    batch_row_mappings: tuple[tuple[int, ...], ...]
    invalid_action_attempts: tuple[int, ...]
    stale_action_rejections: tuple[int, ...]
    timing: VectorOperationalTiming


@dataclass(frozen=True, slots=True)
class PPOVectorEvaluationReport:
    schema_version: str
    vector_config_hash: str
    checkpoint_identity: str
    reward_identity: PPORewardIdentity
    seed_set_hash: str
    episodes: tuple[PPOVectorEpisodeMetrics, ...]
    parameter_hash: str
    optimizer_update_step: int
    semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PPO_VECTOR_EVALUATION_REPORT_SCHEMA_VERSION:
            raise PPOVectorRunnerError("unsupported vector evaluation report schema")
        ordered = tuple(sorted(
            self.episodes,
            key=lambda item: (item.episode.environment_seed, item.env_index,
                              item.episode.episode_index),
        ))
        if ordered != self.episodes:
            raise PPOVectorRunnerError("evaluation episodes are not stably ordered")
        object.__setattr__(self, "semantic_hash", canonical_hash(self._payload()))

    def _payload(self) -> dict[str, object]:
        outcomes = {name: 0 for name in ("WIN", "LOSS", "DRAW", "TRUNCATED")}
        for item in self.episodes:
            outcomes[item.episode.outcome] += 1
        completed = len(self.episodes) - outcomes["TRUNCATED"]
        def stats(name: str) -> dict[str, float]:
            values = [float(getattr(item.episode, name)) for item in self.episodes]
            mean = sum(values) / len(values) if values else 0.0
            variance = (
                sum((value - mean) ** 2 for value in values) / len(values)
                if values else 0.0
            )
            return {"mean": mean, "std": variance ** 0.5}
        reasons: dict[str, int] = {}
        for item in self.episodes:
            key = item.episode.terminal_reason or item.episode.truncation_source
            reasons[key] = reasons.get(key, 0) + 1
        return {
            "schema_version": self.schema_version,
            "vector_config_hash": self.vector_config_hash,
            "checkpoint_identity": self.checkpoint_identity,
            "reward_identity": self.reward_identity.to_dict(),
            "seed_set_hash": self.seed_set_hash,
            "episodes": [item.to_dict() for item in self.episodes],
            "outcomes": outcomes,
            "win_rate": outcomes["WIN"] / completed if completed else 0.0,
            "loss_rate": outcomes["LOSS"] / completed if completed else 0.0,
            "draw_rate": outcomes["DRAW"] / completed if completed else 0.0,
            "primary_metric": "WIN_RATE_ENGINE_OUTCOME",
            "base_return": stats("base_return"),
            "shaping_return": stats("shaping_return"),
            "composite_return": stats("composite_return"),
            "episode_length": stats("decision_count"),
            "terminal_reason_counts": dict(sorted(reasons.items())),
            "parameter_hash": self.parameter_hash,
            "optimizer_update_step": self.optimizer_update_step,
        }

    def to_dict(self) -> dict[str, object]:
        result = self._payload()
        result["semantic_hash"] = self.semantic_hash
        return result


@dataclass(slots=True)
class _EnvState:
    env_index: int
    policy_generator: object
    episode_index: int = 0
    evaluation_episode_index: int = 0
    active: bool = False
    evaluation: bool = False
    episode_seed: int = 0
    episode_base: float = 0.0
    episode_shaping: float = 0.0
    episode_actions: list[str] = field(default_factory=list)
    episode_rewards: list[str] = field(default_factory=list)
    episode_id: str | None = None


@dataclass(frozen=True, slots=True)
class _PendingStep:
    env_index: int
    vector_step: int
    collection_index: int
    binding: TrainingDecisionBinding
    processed: ProcessedLearnerObservation
    selected: LiveActionBinding
    log_prob: float
    value: float
    step: object
    next_binding: TrainingDecisionBinding
    next_processed: ProcessedLearnerObservation


class NativeSynchronousVectorPPORunner:
    """One shared policy over isolated synchronous NativeTrainingEnv instances."""

    def __init__(
        self,
        envs: Sequence[NativeTrainingEnv],
        core: NativeMaskedPPO,
        *,
        vector_config: PPOVectorRunnerConfig | None = None,
        base_runner_config: PPORunnerConfig,
        preprocessor: LearnerObservationPreprocessor | None = None,
        run_namespace: str = "native-vector-ppo-runner",
        artifact_root: str | Path | None = None,
    ) -> None:
        self.config = (
            load_ppo_vector_runner_config() if vector_config is None else vector_config
        )
        if not isinstance(self.config, PPOVectorRunnerConfig):
            raise TypeError("vector_config must be PPOVectorRunnerConfig")
        if not isinstance(base_runner_config, PPORunnerConfig):
            raise TypeError("base_runner_config must be PPORunnerConfig")
        self.base_runner_config = base_runner_config
        self.envs = tuple(envs)
        if len(self.envs) != self.config.num_envs or len({id(item) for item in self.envs}) != len(self.envs):
            raise PPOVectorRunnerError("vector runner requires num_envs distinct environments")
        if not isinstance(core, NativeMaskedPPO):
            raise TypeError("core must be NativeMaskedPPO")
        self.core = core
        self.preprocessor = preprocessor or LearnerObservationPreprocessor()
        if self.core.preprocessor_spec.input_encoder_schema != (
            DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION
        ):
            raise PPOVectorRunnerError(
                "vector runner does not support Decision-v2 observations"
            )
        if self.config.base_runner_config.identity_hash != base_runner_config.content_hash:
            raise PPOVectorRunnerError("vector/base runner config mismatch")
        if self.config.ppo_config.identity_hash != core.config.content_hash or (
            self.config.transitions_per_update != core.config.rollout_length
        ):
            raise PPOVectorRunnerError("vector/core PPO authority mismatch")
        if self.config.seed_config.identity_hash != core.seed.content_hash:
            raise PPOVectorRunnerError("vector/core seed authority mismatch")
        if self.preprocessor.spec.semantic_hash != core.config.observation_preprocessor.identity_hash:
            raise PPOVectorRunnerError("vector/core preprocessor mismatch")
        opponents: list[object] = []
        for env in self.envs:
            if not isinstance(env, NativeTrainingEnv):
                raise TypeError("every vector item must be NativeTrainingEnv")
            if env.controlled_side != self.config.trained_side.lower():
                raise PPOVectorRunnerError("vector env trained side mismatch")
            enabled = env.shaping_policy is not None
            if enabled != (self.config.reward_mode is PPORewardMode.COMPOSITE_EIGHT_FACTOR):
                raise PPOVectorRunnerError("vector env reward mode mismatch")
            if env.action_encoder.contract_hash != core.config.action_encoder.contract_hash or (
                env.action_encoder.max_actions != core.config.action_encoder.capacity
            ):
                raise PPOVectorRunnerError("vector env action authority mismatch")
            if env.discount_config.content_hash != core.discount.content_hash:
                raise PPOVectorRunnerError("vector env discount authority mismatch")
            if not isinstance(env.opponent, NoOpAgent) or env.opponent.agent_id != "native-no-op-fixed-opponent-v1":
                raise PPOVectorRunnerError("vector v1 requires a NoOp opponent per env")
            opponents.append(env.opponent)
        if len({id(item) for item in opponents}) != len(opponents):
            raise PPOVectorRunnerError("vector environments share mutable opponent state")
        self.run_namespace = _text(run_namespace, "run_namespace")
        self.artifact_root = None if artifact_root is None else Path(artifact_root)
        self.states = tuple(
            _EnvState(index, torch.Generator(device="cpu"))
            for index in range(self.config.num_envs)
        )
        self.environment_steps = 0
        self.vector_steps = 0
        self.completed_episodes = 0
        self.invalid_action_attempts = [0] * self.config.num_envs
        self.stale_action_rejections = [0] * self.config.num_envs
        self._runtime_authorities: list[Mapping[str, object] | None] = [
            None
        ] * self.config.num_envs
        self._at_rollout_boundary = True
        self._completion_order = 0

    @property
    def reward_identity(self) -> PPORewardIdentity:
        env = self.envs[0]
        identity = (
            env.base_reward_policy.identity
            if env.shaping_policy is None
            else env.composite_reward_policy.identity
        )
        return PPORewardIdentity(identity.policy_id, identity.policy_version, identity.policy_hash)

    def reset_env(self, env_index: int, *, evaluation: bool = False) -> TrainingDecisionBinding:
        state = self._state(env_index)
        if state.active:
            raise PPOVectorRunnerError("cannot reset an active vector environment")
        coordinate = state.evaluation_episode_index if evaluation else state.episode_index
        stream = TrainingSeedStream.EVALUATION if evaluation else TrainingSeedStream.ENVIRONMENT
        derived_env_index = self.base_runner_config.env_index + env_index
        seed = self.core.seed.derive_seed(
            stream,
            worker_index=self.base_runner_config.worker_index,
            env_index=derived_env_index,
            episode_index=coordinate,
        )
        if not evaluation:
            policy_seed = self.core.seed.derive_seed(
                TrainingSeedStream.POLICY_SAMPLING,
                worker_index=self.base_runner_config.worker_index,
                env_index=derived_env_index,
                episode_index=coordinate,
            )
            state.policy_generator.manual_seed(policy_seed)
        result = self.envs[env_index].reset(seed)
        if result.info["training_interface_version"] != "decision-v1":
            raise PPOVectorRunnerError("vector runner requires Decision-v1")
        authority = dict(self.envs[env_index].runtime_authority)
        previous = self._runtime_authorities[env_index]
        if previous is None:
            self._runtime_authorities[env_index] = authority
        elif authority != previous:
            raise PPOVectorRunnerError("vector environment authority drifted")
        for other_index, other in enumerate(self._runtime_authorities):
            if other_index != env_index and other is not None and other != authority:
                raise PPOVectorRunnerError("vector environments have mixed runtime authority")
        for other_index, other in enumerate(self.envs):
            if other_index != env_index and self._runtime_authorities[other_index] is not None:
                if other.scenario_id != self.envs[env_index].scenario_id:
                    raise PPOVectorRunnerError("vector environments have mixed scenarios")
        state.active = True
        state.evaluation = evaluation
        state.episode_seed = seed
        state.episode_base = state.episode_shaping = 0.0
        state.episode_actions.clear()
        state.episode_rewards.clear()
        state.episode_id = None
        return self.envs[env_index].current_decision_binding

    def collect_rollout(self, length: int | None = None) -> PPORolloutVectorCollection:
        target = self.core.config.rollout_length if length is None else length
        if type(target) is not int or not 0 < target <= self.core.config.rollout_length:
            raise PPOVectorRunnerError("rollout length is outside PPO config")
        for index, state in enumerate(self.states):
            if not state.active:
                self.reset_env(index, evaluation=False)
            elif state.evaluation:
                raise PPOVectorRunnerError("training cannot consume an evaluation episode")
        self._at_rollout_boundary = False
        started = time.perf_counter()
        policy_seconds = preprocessing_seconds = environment_seconds = 0.0
        reward_replay_seconds = 0.0
        segments: list[list[VectorTransitionRecord]] = [
            [] for _ in range(self.config.num_envs)
        ]
        episodes: list[PPOVectorEpisodeMetrics] = []
        row_mappings: list[tuple[int, ...]] = []
        committed = 0
        local_vector_steps = 0
        rollout_policy_version = self.core.update_step
        while committed < target:
            remaining = target - committed
            selected_envs = tuple(
                index for index, state in enumerate(self.states) if state.active
            )[:remaining]
            if not selected_envs:
                raise PPOVectorRunnerError("vector rollout has no active environment")
            row_mappings.append(selected_envs)
            before = time.perf_counter()
            bindings = [self.envs[index].current_decision_binding for index in selected_envs]
            processed = [self._process(index, binding) for index, binding in zip(selected_envs, bindings)]
            observations = torch.stack([item.to_torch_cpu() for item in processed])
            masks = torch.tensor(
                [binding.action_space.mask for binding in bindings], dtype=torch.bool
            )
            preprocessing_seconds += time.perf_counter() - before
            before = time.perf_counter()
            actions, log_probs, values = self._infer(
                selected_envs, observations, masks, evaluation=False
            )
            policy_seconds += time.perf_counter() - before
            pending: list[_PendingStep] = []
            for row, env_index in enumerate(selected_envs):
                binding = bindings[row]
                try:
                    selected = LiveActionBinding.select(binding, actions[row])
                except PPORunnerError as exc:
                    self.invalid_action_attempts[env_index] += 1
                    raise PPOVectorRunnerError(str(exc)) from exc
                before = time.perf_counter()
                try:
                    step = self.envs[env_index].step_bound_action(actions[row], binding)
                except NativeTrainingError as exc:
                    if "stale" in str(exc):
                        self.stale_action_rejections[env_index] += 1
                    raise PPOVectorRunnerError(
                        f"vector env {env_index} bound action failed: {exc}"
                    ) from exc
                environment_seconds += time.perf_counter() - before
                next_binding = self.envs[env_index].current_decision_binding
                before = time.perf_counter()
                next_processed = self._process(env_index, next_binding)
                preprocessing_seconds += time.perf_counter() - before
                pending.append(_PendingStep(
                    env_index=env_index, vector_step=local_vector_steps,
                    collection_index=committed + row, binding=binding,
                    processed=processed[row], selected=selected,
                    log_prob=log_probs[row], value=values[row], step=step,
                    next_binding=next_binding, next_processed=next_processed,
                ))
            before = time.perf_counter()
            next_values = self._next_values(pending)
            policy_seconds += time.perf_counter() - before
            for item, next_value in zip(pending, next_values):
                transition = self._transition(item, next_value)
                record = VectorTransitionRecord(
                    env_index=item.env_index,
                    vector_step=local_vector_steps,
                    collection_index=item.collection_index,
                    policy_update_step=rollout_policy_version,
                    scenario_id=self.envs[item.env_index].scenario_id,
                    transition=transition,
                )
                segments[item.env_index].append(record)
                self._accumulate(item.env_index, item.selected, item.step)
                if item.step.terminated or item.step.truncated:
                    before = time.perf_counter()
                    episodes.append(self._finish_episode(item.env_index, item.step))
                    reward_replay_seconds += time.perf_counter() - before
            committed += len(pending)
            self.environment_steps += len(pending)
            self.vector_steps += 1
            local_vector_steps += 1
            if committed < target:
                for item in pending:
                    if not self.states[item.env_index].active:
                        self.reset_env(item.env_index, evaluation=False)
        for env_index, segment in enumerate(segments):
            if not segment:
                continue
            final = segment[-1]
            if final.transition.trace_continues:
                segment[-1] = replace(
                    final,
                    transition=replace(final.transition, trace_continues=False),
                )
        vector_rollout = PPOVectorRollout(
            schema_version=PPO_VECTOR_ROLLOUT_SCHEMA_VERSION,
            vector_config_hash=self.config.content_hash,
            num_envs=self.config.num_envs,
            vector_steps=local_vector_steps,
            total_transitions=committed,
            flatten_order=self.config.flatten_order,
            policy_update_step=rollout_policy_version,
            segments=tuple(tuple(item) for item in segments),
        )
        batch = _flatten_vector_rollout(vector_rollout, self.core)
        elapsed = time.perf_counter() - started
        self._at_rollout_boundary = True
        return PPORolloutVectorCollection(
            vector_rollout=vector_rollout, batch=batch,
            episodes=tuple(episodes), batch_row_mappings=tuple(row_mappings),
            invalid_action_attempts=tuple(self.invalid_action_attempts),
            stale_action_rejections=tuple(self.stale_action_rejections),
            timing=VectorOperationalTiming(
                preprocessing_seconds, policy_seconds, environment_seconds,
                reward_replay_seconds, elapsed,
            ),
        )

    def train_update(
        self, length: int | None = None,
    ) -> tuple[PPORolloutVectorCollection, PPOUpdateMetrics, float]:
        rollout = self.collect_rollout(length)
        started = time.perf_counter()
        metrics = self.core.update(rollout.batch)
        update_seconds = time.perf_counter() - started
        if self.artifact_root is not None:
            for item in rollout.episodes:
                self._append_jsonl("vector_episode_metrics.jsonl", item.to_dict())
            self._append_jsonl("vector_update_metrics.jsonl", {
                "schema_version": "native-ppo-vector-update-metrics/1.0",
                "vector_config_hash": self.config.content_hash,
                "update_step": metrics.update_step,
                "committed_transitions": rollout.vector_rollout.total_transitions,
                "vector_steps": rollout.vector_rollout.vector_steps,
                "policy_loss": metrics.policy_loss,
                "value_loss": metrics.value_loss,
                "entropy": metrics.entropy,
                "total_loss": metrics.total_loss,
                "approx_kl": metrics.approx_kl,
                "clip_fraction": metrics.clip_fraction,
                "grad_norm": metrics.grad_norm,
                "update_seconds": update_seconds,
            })
        return rollout, metrics, update_seconds

    def evaluate(
        self, episodes: int, *, checkpoint_identity: str = "IN_MEMORY",
    ) -> PPOVectorEvaluationReport:
        if type(episodes) is not int or episodes <= 0:
            raise PPOVectorRunnerError("evaluation episode count must be positive")
        if any(state.active for state in self.states):
            raise PPOVectorRunnerError("vector evaluation requires inactive environments")
        before_update = self.core.update_step
        before_sample = self.core.sample_step
        before_parameters = self.core.parameter_content_hash()
        training_rng = self.core.policy_sampling_generator.get_state().clone()
        pending_jobs = episodes
        results: list[PPOVectorEpisodeMetrics] = []
        for env_index in range(min(self.config.num_envs, pending_jobs)):
            self.reset_env(env_index, evaluation=True)
            pending_jobs -= 1
        while len(results) < episodes:
            active = tuple(index for index, state in enumerate(self.states) if state.active)
            if not active:
                raise PPOVectorRunnerError("evaluation lost all active environments")
            bindings = [self.envs[index].current_decision_binding for index in active]
            processed = [self._process(index, binding) for index, binding in zip(active, bindings)]
            observations = torch.stack([item.to_torch_cpu() for item in processed])
            masks = torch.tensor([item.action_space.mask for item in bindings], dtype=torch.bool)
            actions, _log_probs, _values = self._infer(
                active, observations, masks, evaluation=True
            )
            completed_now: list[int] = []
            for row, env_index in enumerate(active):
                selected = LiveActionBinding.select(bindings[row], actions[row])
                step = self.envs[env_index].step_bound_action(actions[row], bindings[row])
                self._accumulate(env_index, selected, step)
                if step.terminated or step.truncated:
                    results.append(self._finish_episode(env_index, step))
                    completed_now.append(env_index)
            for env_index in completed_now:
                if pending_jobs > 0:
                    self.reset_env(env_index, evaluation=True)
                    pending_jobs -= 1
        if self.core.update_step != before_update or self.core.sample_step != before_sample:
            raise PPOVectorRunnerError("evaluation mutated PPO counters")
        if self.core.parameter_content_hash() != before_parameters:
            raise PPOVectorRunnerError("evaluation mutated policy parameters")
        if not torch.equal(training_rng, self.core.policy_sampling_generator.get_state()):
            raise PPOVectorRunnerError("evaluation consumed training sampling RNG")
        ordered = tuple(sorted(
            results,
            key=lambda item: (item.episode.environment_seed, item.env_index,
                              item.episode.episode_index),
        ))
        report = PPOVectorEvaluationReport(
            schema_version=PPO_VECTOR_EVALUATION_REPORT_SCHEMA_VERSION,
            vector_config_hash=self.config.content_hash,
            checkpoint_identity=checkpoint_identity,
            reward_identity=self.reward_identity,
            seed_set_hash=canonical_hash([
                item.episode.environment_seed for item in ordered
            ]),
            episodes=ordered, parameter_hash=before_parameters,
            optimizer_update_step=before_update,
        )
        if self.artifact_root is not None:
            _atomic_json(self.artifact_root / "vector_evaluation_report.json", report.to_dict())
        return report

    def save_checkpoint(self, target: str | Path) -> Path:
        """Save vector provenance for evaluation/new-run loading at a boundary."""

        if not self._at_rollout_boundary:
            raise PPOVectorRunnerError("vector checkpoint requires rollout boundary")
        if any(item is None for item in self._runtime_authorities):
            raise PPOVectorRunnerError("cannot checkpoint before every env authority is known")
        destination = Path(target)
        if destination.exists():
            raise FileExistsError(f"checkpoint already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
        core_payload = self.core.checkpoint_payload(self.reward_identity)
        payload = {
            "core": core_payload,
            "per_env_policy_rng_states": [
                state.policy_generator.get_state().clone() for state in self.states
            ],
        }
        try:
            state_path = staging / "state.pt"
            torch.save(payload, state_path)
            manifest = self._checkpoint_manifest(
                file_sha256(state_path), payload
            )
            _atomic_json(staging / "manifest.json", manifest)
            os.replace(staging, destination)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return destination

    def load_checkpoint_for_evaluation(self, source: str | Path) -> str:
        if any(state.active for state in self.states) or not self._at_rollout_boundary:
            raise PPOVectorRunnerError("evaluation load requires inactive boundary")
        for index in range(self.config.num_envs):
            self.reset_env(index, evaluation=True)
            self.states[index].active = False
        root = Path(source)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        state_path = root / "state.pt"
        if file_sha256(state_path) != manifest.get("state_hash"):
            raise PPOCheckpointError("vector checkpoint state hash mismatch")
        static = self._checkpoint_static_payload()
        if any(manifest.get(name) != value for name, value in static.items()):
            raise PPOCheckpointError("vector checkpoint/runtime authority mismatch")
        claimed = manifest.get("semantic_hash")
        semantic = dict(manifest)
        semantic.pop("semantic_hash", None)
        if claimed != canonical_hash(semantic):
            raise PPOCheckpointError("vector checkpoint manifest hash mismatch")
        payload = torch.load(state_path, map_location="cpu", weights_only=False)
        if not isinstance(payload, Mapping) or set(payload) != {
            "core", "per_env_policy_rng_states"
        } or _stable_object_hash(payload) != manifest.get("payload_hash"):
            raise PPOCheckpointError("vector checkpoint payload hash mismatch")
        self.core.restore_checkpoint(payload["core"], reward_identity=self.reward_identity)
        states = payload["per_env_policy_rng_states"]
        if not isinstance(states, Sequence) or len(states) != self.config.num_envs:
            raise PPOCheckpointError("vector checkpoint per-env RNG state mismatch")
        for state, rng in zip(self.states, states):
            state.policy_generator.set_state(rng)
        return str(claimed)

    def close(self) -> None:
        errors: list[BaseException] = []
        for env in self.envs:
            try:
                env.close()
            except BaseException as exc:  # preserve isolation cleanup best effort
                errors.append(exc)
        if errors:
            raise PPOVectorRunnerError(f"vector close failed: {errors[0]}")

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
                        )
                        for row, env_index in enumerate(env_indices)
                    ])
                    self.core.sample_step += len(env_indices)
                log_probs = distribution.log_prob(actions)
            return (
                tuple(int(item) for item in actions.tolist()),
                tuple(float(item) for item in log_probs.tolist()),
                tuple(float(item) for item in values.tolist()),
            )
        finally:
            self.core.model.train(was_training)

    def _next_values(self, pending: Sequence[_PendingStep]) -> tuple[float, ...]:
        values = [0.0] * len(pending)
        rows = [index for index, item in enumerate(pending) if not item.step.terminated]
        if rows:
            observations = torch.stack([
                pending[index].next_processed.to_torch_cpu() for index in rows
            ])
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

    def _transition(self, item: _PendingStep, next_value: float) -> LearnerTransition:
        record = item.step.reward_record
        identity = record.transition_identity
        if (
            identity.decision_before_hash != item.selected.decision_hash
            or identity.action_catalog_hash != item.selected.catalog_hash
            or item.selected.action_id not in (identity.submitted_action_ids or ())
            or identity.decision_after_hash
            != item.next_binding.observation.decision_semantic_hash
        ):
            raise PPOVectorRunnerError("vector reward/Decision/action binding misalignment")
        base, shaping = _reward_components(record.components)
        delta_ms = identity.sim_time_after_ms - identity.sim_time_before_ms
        discount_gamma = self.core.discount.effective_gamma(delta_time_ms=delta_ms)
        return LearnerTransition(
            schema_version=PPO_LEARNER_TRANSITION_SCHEMA_VERSION,
            episode_id=identity.episode_id, run_namespace=self.run_namespace,
            side=self.envs[item.env_index].controlled_side,
            observation=item.processed.values,
            action_mask=tuple(item.binding.action_space.mask),
            decision_identity=item.selected.decision_hash,
            catalog_hash=item.selected.catalog_hash,
            action_encoder_hash=item.selected.action_encoder_hash,
            action_index=item.selected.action_index,
            action_id=item.selected.action_id,
            old_log_prob=item.log_prob, value_prediction=item.value,
            reward=float(item.step.reward), reward_record_hash=record.semantic_hash,
            base_reward_amount=base, shaping_reward_amount=shaping,
            terminated=item.step.terminated, truncated=item.step.truncated,
            truncation_source=record.truncation.source.value,
            effective_gamma=discount_gamma,
            next_observation=item.next_processed.values,
            next_decision_identity=identity.decision_after_hash,
            next_value_prediction=next_value,
            bootstrap_allowed=not item.step.terminated,
            trace_continues=not item.step.terminated and not item.step.truncated,
            preprocessor_hash=item.processed.preprocessor_hash,
        )

    def _process(
        self, env_index: int, binding: TrainingDecisionBinding,
    ) -> ProcessedLearnerObservation:
        result = self.preprocessor.preprocess(binding.observation)
        if result.decision_window_index != binding.action_space.decision_window_index:
            raise PPOVectorRunnerError(
                f"env {env_index} preprocessed observation/catalog mismatch"
            )
        return result

    def _accumulate(self, env_index: int, selected: LiveActionBinding, step) -> None:
        state = self.states[env_index]
        base, shaping = _reward_components(step.reward_record.components)
        state.episode_id = step.reward_record.transition_identity.episode_id
        state.episode_base += base
        state.episode_shaping += shaping
        state.episode_actions.append(selected.action_id)
        state.episode_rewards.append(step.reward_record.semantic_hash)

    def _finish_episode(self, env_index: int, step) -> PPOVectorEpisodeMetrics:
        state = self.states[env_index]
        env = self.envs[env_index]
        if not state.active or state.episode_id is None:
            raise PPOVectorRunnerError("vector episode completion is missing identity")
        winner = step.info.get("winner")
        winner = None if winner is None else str(winner).lower()
        outcome = (
            "TRUNCATED" if step.truncated else
            "WIN" if winner == env.controlled_side else
            "LOSS" if winner in {"red", "blue"} else "DRAW"
        )
        verification = env.verify_reward_rederivation()
        if not verification.matches:
            raise PPOVectorRunnerError("vector episode reward re-derivation mismatch")
        coordinate = (
            state.evaluation_episode_index if state.evaluation else state.episode_index
        )
        record = step.reward_record
        episode = PPOEpisodeMetrics(
            schema_version=PPO_EPISODE_METRICS_SCHEMA_VERSION,
            episode_id=state.episode_id, run_namespace=self.run_namespace,
            side=env.controlled_side, episode_index=coordinate,
            environment_seed=state.episode_seed, outcome=outcome, winner=winner,
            terminal_reason=record.terminal_reason_code,
            terminated=step.terminated, truncated=step.truncated,
            truncation_source=record.truncation.source.value,
            decision_count=len(state.episode_actions),
            sim_time_ms=int(step.info["sim_time_ms"]),
            base_return=state.episode_base,
            shaping_return=state.episode_shaping,
            composite_return=state.episode_base + state.episode_shaping,
            reward_identity=self.reward_identity,
            action_ids=tuple(state.episode_actions),
            reward_record_hashes=tuple(state.episode_rewards),
            reward_rederivation_verified=True,
        )
        wrapper = PPOVectorEpisodeMetrics(
            schema_version=PPO_VECTOR_EPISODE_METRICS_SCHEMA_VERSION,
            env_index=env_index, scenario_id=env.scenario_id,
            completion_order=self._completion_order, episode=episode,
        )
        if self.artifact_root is not None:
            mode = "evaluation" if state.evaluation else "training"
            root = self.artifact_root / "envs" / f"{env_index:03d}" / mode / f"{coordinate:06d}"
            env.publish(root, overwrite=False)
            write_reward_records_jsonl(
                root / "joint_reward_records.native.jsonl", env.reward_records
            )
            _atomic_json(root / "ppo_vector_episode_metrics.json", wrapper.to_dict())
        self._completion_order += 1
        if state.evaluation:
            state.evaluation_episode_index += 1
        else:
            state.episode_index += 1
            self.completed_episodes += 1
        state.active = False
        return wrapper

    def _state(self, env_index: int) -> _EnvState:
        if type(env_index) is not int or not 0 <= env_index < len(self.states):
            raise PPOVectorRunnerError("env_index is outside vector capacity")
        return self.states[env_index]

    def _checkpoint_static_payload(self) -> dict[str, object]:
        return {
            "schema_version": PPO_VECTOR_CHECKPOINT_SCHEMA_VERSION,
            "vector_config_hash": self.config.content_hash,
            "base_runner_config_hash": self.base_runner_config.content_hash,
            "runtime_authorities": [dict(item) for item in self._runtime_authorities],
            "reward_identity": self.reward_identity.to_dict(),
            "reward_authority": self._reward_authority(),
            "seed_identity": self.core.seed.identity.to_dict(),
            "checkpoint_boundary": "VECTOR_ROLLOUT_UPDATE_BOUNDARY",
            "load_modes": ["LOAD_CHECKPOINT_FOR_EVALUATION", "NEW_RUN_INITIAL_POLICY"],
            "exact_vector_training_resume": False,
        }

    def _checkpoint_manifest(self, state_hash: str, payload: Mapping[str, object]) -> dict[str, object]:
        value = {
            **self._checkpoint_static_payload(),
            "state_hash": state_hash,
            "payload_hash": _stable_object_hash(payload),
            "environment_steps": self.environment_steps,
            "vector_steps": self.vector_steps,
            "completed_episodes": self.completed_episodes,
            "per_env_episode_indices": [item.episode_index for item in self.states],
            "update_step": self.core.update_step,
            "sample_step": self.core.sample_step,
            "parameter_hash": self.core.parameter_content_hash(),
        }
        value["semantic_hash"] = canonical_hash(value)
        return value

    def _reward_authority(self) -> dict[str, object]:
        env = self.envs[0]
        return {
            "mode": self.config.reward_mode.value,
            "base": env.base_reward_policy.identity.to_dict(),
            "composite": None if env.shaping_policy is None else env.composite_reward_policy.to_dict(),
            "shaping": None if env.shaping_policy is None else env.shaping_policy.to_dict(),
            "discount": env.discount_config.to_dict(),
        }

    def _append_jsonl(self, name: str, value: Mapping[str, object]) -> None:
        assert self.artifact_root is not None
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        from wargame_native_engine.io.serialization import canonical_json
        with (self.artifact_root / name).open("a", encoding="utf-8") as stream:
            stream.write(canonical_json(value) + "\n")


def _flatten_vector_rollout(
    rollout: PPOVectorRollout, core: NativeMaskedPPO,
) -> PPORolloutBatch:
    ordered: list[LearnerTransition] = []
    advantages = []
    targets = []
    for segment in rollout.segments:
        transitions = [item.transition for item in segment]
        if not transitions:
            continue
        rewards = torch.tensor([item.reward for item in transitions], dtype=torch.float32)
        values = torch.tensor([item.value_prediction for item in transitions], dtype=torch.float32)
        next_values = torch.tensor([item.next_value_prediction for item in transitions], dtype=torch.float32)
        gammas = torch.tensor([item.effective_gamma for item in transitions], dtype=torch.float32)
        bootstrap = torch.tensor([item.bootstrap_allowed for item in transitions], dtype=torch.bool)
        trace = torch.tensor([item.trace_continues for item in transitions], dtype=torch.bool)
        gae = compute_gae(
            rewards=rewards, values=values, next_values=next_values,
            effective_gammas=gammas, bootstrap_allowed=bootstrap,
            trace_continues=trace, gae_lambda=core.config.gae_lambda,
        )
        ordered.extend(transitions)
        advantages.append(gae.advantages)
        targets.append(gae.value_targets)
    if len(ordered) != rollout.total_transitions:
        raise PPOVectorRunnerError("flatten dropped a committed transition")
    def floats(name: str):
        return torch.tensor([getattr(item, name) for item in ordered], dtype=torch.float32)
    return PPORolloutBatch(
        schema_version=PPO_ROLLOUT_BATCH_SCHEMA_VERSION,
        ppo_config_hash=core.config.content_hash,
        observations=torch.tensor([item.observation for item in ordered], dtype=torch.float32),
        action_masks=torch.tensor([item.action_mask for item in ordered], dtype=torch.bool),
        actions=torch.tensor([item.action_index for item in ordered], dtype=torch.long),
        old_log_probs=floats("old_log_prob"), old_values=floats("value_prediction"),
        rewards=floats("reward"),
        terminated=torch.tensor([item.terminated for item in ordered], dtype=torch.bool),
        truncated=torch.tensor([item.truncated for item in ordered], dtype=torch.bool),
        effective_gammas=floats("effective_gamma"),
        next_values=floats("next_value_prediction"),
        bootstrap_allowed=torch.tensor([item.bootstrap_allowed for item in ordered], dtype=torch.bool),
        trace_continues=torch.tensor([item.trace_continues for item in ordered], dtype=torch.bool),
        advantages=torch.cat(advantages), value_targets=torch.cat(targets),
        provenance=tuple(ordered),
    )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty trimmed text")
    return value


__all__ = [
    "NativeSynchronousVectorPPORunner", "PPORolloutVectorCollection",
    "PPOVectorEpisodeMetrics", "PPOVectorEvaluationReport",
    "PPOVectorRollout", "PPOVectorRunnerError", "VectorLiveBinding",
    "VectorOperationalTiming", "VectorTransitionRecord",
]
