"""Single-environment Native PPO rollout, checkpoint and evaluation runner.

This is the only Phase 6C join between the tensor-only PPO core and
``NativeTrainingEnv``.  It consumes frozen Decision-v1/catalog values and
submitted reward records; it contains no adjudication or reward formulas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Mapping, Sequence

from wargame_native_engine.agents.baselines import NoOpAgent
from wargame_native_engine.io.serialization import (
    canonical_hash,
    canonical_json,
    file_sha256,
)
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
    NativeMaskedPPO,
    PPOCheckpointError,
    PPORewardIdentity,
    PPORolloutBatch,
    PPOUpdateMetrics,
    PPO_LEARNER_TRANSITION_SCHEMA_VERSION,
    torch,
)
from wargame_native_engine.training.ppo_runner_config import (
    PPORunnerConfig,
    PPORewardMode,
    load_ppo_runner_config,
)
from wargame_native_engine.training.reward_replay import (
    write_reward_records_jsonl,
)
from wargame_native_engine.training.training_seed import TrainingSeedStream


PPO_EPISODE_METRICS_SCHEMA_VERSION = "native-ppo-episode-metrics/1.0"
PPO_EVALUATION_REPORT_SCHEMA_VERSION = "native-ppo-evaluation-report/1.0"
PPO_RUN_PUBLICATION_SCHEMA_VERSION = "native-ppo-run-publication/1.0"
PPO_RUNNER_CHECKPOINT_SCHEMA_VERSION = "native-ppo-runner-checkpoint/1.0"


class PPORunnerError(RuntimeError):
    """A live binding, reward join, or runner lifecycle contract failed."""


@dataclass(frozen=True, slots=True)
class LiveActionBinding:
    side: str
    decision_window_index: int
    frame_index: int
    sim_time_ms: int
    decision_hash: str
    catalog_hash: str
    action_encoder_hash: str
    action_index: int
    action_id: str
    snapshot: TrainingDecisionBinding = field(repr=False, compare=True)

    @classmethod
    def select(
        cls, snapshot: TrainingDecisionBinding, action_index: int,
    ) -> "LiveActionBinding":
        if not isinstance(snapshot, TrainingDecisionBinding):
            raise TypeError("snapshot must be TrainingDecisionBinding")
        space = snapshot.action_space
        if type(action_index) is not int or not 0 <= action_index < len(space.mask):
            raise PPORunnerError("policy action index is outside encoder capacity")
        if not space.mask[action_index] or action_index >= len(space.action_ids):
            raise PPORunnerError("policy selected an invalid/padded catalog action")
        return cls(
            side=space.side.value,
            decision_window_index=space.decision_window_index,
            frame_index=space.frame_index,
            sim_time_ms=space.sim_time_ms,
            decision_hash=snapshot.observation.decision_semantic_hash,
            catalog_hash=space.catalog_hash,
            action_encoder_hash=space.encoder_contract_hash,
            action_index=action_index,
            action_id=space.action_ids[action_index],
            snapshot=snapshot,
        )


@dataclass(frozen=True, slots=True)
class PPOEpisodeMetrics:
    schema_version: str
    episode_id: str
    run_namespace: str
    side: str
    episode_index: int
    environment_seed: int
    outcome: str
    winner: str | None
    terminal_reason: str | None
    terminated: bool
    truncated: bool
    truncation_source: str
    decision_count: int
    sim_time_ms: int
    base_return: float
    shaping_return: float
    composite_return: float
    reward_identity: PPORewardIdentity
    action_ids: tuple[str, ...]
    reward_record_hashes: tuple[str, ...]
    reward_rederivation_verified: bool
    semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PPO_EPISODE_METRICS_SCHEMA_VERSION:
            raise PPORunnerError("unsupported PPO episode metrics schema")
        if self.side not in {"red", "blue"}:
            raise PPORunnerError("episode metrics side is invalid")
        if self.outcome not in {"WIN", "LOSS", "DRAW", "TRUNCATED"}:
            raise PPORunnerError("episode outcome bucket is invalid")
        if self.terminated == self.truncated:
            raise PPORunnerError("completed episode must terminate xor truncate")
        for value in (self.base_return, self.shaping_return, self.composite_return):
            if not math.isfinite(value):
                raise PPORunnerError("episode return must be finite")
        if not math.isclose(
            self.composite_return,
            self.base_return + self.shaping_return,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            raise PPORunnerError("episode reward component totals are inconsistent")
        if self.reward_rederivation_verified is not True:
            raise PPORunnerError("episode reward re-derivation is not verified")
        object.__setattr__(self, "semantic_hash", canonical_hash(self._payload()))

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "run_namespace": self.run_namespace,
            "side": self.side,
            "episode_index": self.episode_index,
            "environment_seed": self.environment_seed,
            "outcome": self.outcome,
            "winner": self.winner,
            "terminal_reason": self.terminal_reason,
            "terminated": self.terminated,
            "truncated": self.truncated,
            "truncation_source": self.truncation_source,
            "decision_count": self.decision_count,
            "sim_time_ms": self.sim_time_ms,
            "base_return": self.base_return,
            "shaping_return": self.shaping_return,
            "composite_return": self.composite_return,
            "reward_identity": self.reward_identity.to_dict(),
            "action_ids": list(self.action_ids),
            "reward_record_hashes": list(self.reward_record_hashes),
            "reward_rederivation_verified": self.reward_rederivation_verified,
        }

    def to_dict(self) -> dict[str, object]:
        value = self._payload()
        value["semantic_hash"] = self.semantic_hash
        return value


@dataclass(frozen=True, slots=True)
class PPORolloutCollection:
    batch: PPORolloutBatch
    episodes: tuple[PPOEpisodeMetrics, ...]
    action_ids: tuple[str, ...]
    reward_record_hashes: tuple[str, ...]
    invalid_action_attempts: int
    stale_action_rejections: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class PPORunSummary:
    runner_config_hash: str
    updates: int
    environment_steps: int
    episodes: int
    episode_metrics: tuple[PPOEpisodeMetrics, ...]
    update_metrics: tuple[PPOUpdateMetrics, ...]
    transitions_per_second: float
    elapsed_seconds: float

    def to_dict(self) -> dict[str, object]:
        outcomes = {name: 0 for name in ("WIN", "LOSS", "DRAW", "TRUNCATED")}
        for item in self.episode_metrics:
            outcomes[item.outcome] += 1
        def mean(name: str) -> float:
            values = [float(getattr(item, name)) for item in self.episode_metrics]
            return sum(values) / len(values) if values else 0.0
        return {
            "schema_version": "native-ppo-run-summary/1.0",
            "runner_config_hash": self.runner_config_hash,
            "updates": self.updates,
            "environment_steps": self.environment_steps,
            "episodes": self.episodes,
            "outcomes": outcomes,
            "mean_episode_length": mean("decision_count"),
            "mean_base_return": mean("base_return"),
            "mean_shaping_return": mean("shaping_return"),
            "mean_composite_return": mean("composite_return"),
            "transitions_per_second": self.transitions_per_second,
            "elapsed_seconds": self.elapsed_seconds,
            "operational_metrics_are_not_benchmark_claims": True,
        }


@dataclass(frozen=True, slots=True)
class PPOEvaluationReport:
    schema_version: str
    runner_config_hash: str
    checkpoint_identity: str
    reward_identity: PPORewardIdentity
    seed_set_hash: str
    episodes: tuple[PPOEpisodeMetrics, ...]
    action_sequences: tuple[tuple[str, ...], ...]
    parameter_hash: str
    optimizer_update_step: int
    semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PPO_EVALUATION_REPORT_SCHEMA_VERSION:
            raise PPORunnerError("unsupported PPO evaluation report schema")
        object.__setattr__(self, "semantic_hash", canonical_hash(self._payload()))

    @property
    def wins(self) -> int:
        return sum(item.outcome == "WIN" for item in self.episodes)

    @property
    def losses(self) -> int:
        return sum(item.outcome == "LOSS" for item in self.episodes)

    @property
    def draws(self) -> int:
        return sum(item.outcome == "DRAW" for item in self.episodes)

    @property
    def truncated(self) -> int:
        return sum(item.outcome == "TRUNCATED" for item in self.episodes)

    def _payload(self) -> dict[str, object]:
        count = len(self.episodes)
        completed = count - self.truncated
        def stats(name: str) -> dict[str, float]:
            values = [float(getattr(item, name)) for item in self.episodes]
            mean = sum(values) / len(values) if values else 0.0
            variance = sum((item - mean) ** 2 for item in values) / len(values) if values else 0.0
            return {"mean": mean, "std": variance ** 0.5}
        reasons: dict[str, int] = {}
        for item in self.episodes:
            key = item.terminal_reason or item.truncation_source
            reasons[key] = reasons.get(key, 0) + 1
        return {
            "schema_version": self.schema_version,
            "runner_config_hash": self.runner_config_hash,
            "checkpoint_identity": self.checkpoint_identity,
            "reward_identity": self.reward_identity.to_dict(),
            "seed_set_hash": self.seed_set_hash,
            "episode_count": count,
            "wins": self.wins, "losses": self.losses,
            "draws": self.draws, "truncated": self.truncated,
            "win_rate": self.wins / completed if completed else 0.0,
            "loss_rate": self.losses / completed if completed else 0.0,
            "draw_rate": self.draws / completed if completed else 0.0,
            "base_return": stats("base_return"),
            "shaping_return": stats("shaping_return"),
            "composite_return": stats("composite_return"),
            "episode_length": stats("decision_count"),
            "terminal_reason_counts": dict(sorted(reasons.items())),
            "episodes": [item.to_dict() for item in self.episodes],
            "action_sequences": [list(item) for item in self.action_sequences],
            "parameter_hash": self.parameter_hash,
            "optimizer_update_step": self.optimizer_update_step,
        }

    def to_dict(self) -> dict[str, object]:
        value = self._payload()
        value["semantic_hash"] = self.semantic_hash
        return value


class NativePPORunner:
    """Synchronous single-seat PPO runner against one NativeTrainingEnv."""

    def __init__(
        self,
        env: NativeTrainingEnv,
        core: NativeMaskedPPO,
        *,
        config: PPORunnerConfig | None = None,
        preprocessor: LearnerObservationPreprocessor | None = None,
        run_namespace: str = "native-ppo-runner",
        artifact_root: str | Path | None = None,
    ) -> None:
        if not isinstance(env, NativeTrainingEnv):
            raise TypeError("env must be NativeTrainingEnv")
        if not isinstance(core, NativeMaskedPPO):
            raise TypeError("core must be NativeMaskedPPO")
        self.env = env
        self.core = core
        self.config = load_ppo_runner_config() if config is None else config
        self.preprocessor = (
            LearnerObservationPreprocessor() if preprocessor is None else preprocessor
        )
        if not isinstance(self.config, PPORunnerConfig):
            raise TypeError("config must be PPORunnerConfig")
        if self.config.ppo_config.identity_hash != core.config.content_hash:
            raise PPORunnerError("runner/core PPO config mismatch")
        if self.config.seed_config.identity_hash != core.seed.content_hash:
            raise PPORunnerError("runner/core seed config mismatch")
        if self.preprocessor.spec.semantic_hash != core.config.observation_preprocessor.identity_hash:
            raise PPORunnerError("runner/core preprocessor mismatch")
        if env.controlled_side != self.config.trained_side.lower():
            raise PPORunnerError("runner trained side does not match environment")
        enabled = env.shaping_policy is not None
        if enabled != (self.config.reward_mode is PPORewardMode.COMPOSITE_EIGHT_FACTOR):
            raise PPORunnerError("runner reward mode does not match environment")
        if env.action_encoder.contract_hash != core.config.action_encoder.contract_hash:
            raise PPORunnerError("runner action encoder mismatch")
        if getattr(env.action_encoder, "max_actions", None) != core.config.action_encoder.capacity:
            raise PPORunnerError("runner action capacity mismatch")
        if env.discount_config.content_hash != core.discount.content_hash:
            raise PPORunnerError("runner discount authority mismatch")
        if not isinstance(env.opponent, NoOpAgent) or (
            env.opponent.agent_id != "native-no-op-fixed-opponent-v1"
        ):
            raise PPORunnerError("runner v1 requires the configured NoOp opponent")
        self.run_namespace = _text(run_namespace, "run_namespace")
        self.artifact_root = None if artifact_root is None else Path(artifact_root)
        self.episode_index = 0
        self.environment_steps = 0
        self.completed_episodes = 0
        self.update_count = core.update_step
        self.invalid_action_attempts = 0
        self.stale_action_rejections = 0
        self._active = False
        self._at_rollout_boundary = True
        self._runtime_authority: Mapping[str, object] | None = None
        self._episode_seed = 0
        self._episode_base = 0.0
        self._episode_shaping = 0.0
        self._episode_actions: list[str] = []
        self._episode_rewards: list[str] = []
        self._episode_id: str | None = None

    @property
    def reward_identity(self) -> PPORewardIdentity:
        identity = (
            self.env.base_reward_policy.identity
            if self.env.shaping_policy is None
            else self.env.composite_reward_policy.identity
        )
        return PPORewardIdentity(
            identity.policy_id, identity.policy_version, identity.policy_hash
        )

    @property
    def runtime_authority(self) -> Mapping[str, object]:
        if self._runtime_authority is None:
            raise PPORunnerError("runner has not reset an environment")
        return dict(self._runtime_authority)

    def reset_episode(self, *, evaluation: bool = False) -> TrainingDecisionBinding:
        if self._active:
            raise PPORunnerError("cannot reset an active runner episode")
        stream = TrainingSeedStream.EVALUATION if evaluation else TrainingSeedStream.ENVIRONMENT
        seed = self.core.seed.derive_seed(
            stream,
            worker_index=self.config.worker_index,
            env_index=self.config.env_index,
            episode_index=self.episode_index,
        )
        if not evaluation:
            self.core.set_policy_sampling_stream(
                worker_index=self.config.worker_index,
                env_index=self.config.env_index,
                episode_index=self.episode_index,
            )
        result = self.env.reset(seed)
        if result.info["training_interface_version"] != "decision-v1":
            raise PPORunnerError("PPO runner requires Decision-v1 environment")
        authority = dict(self.env.runtime_authority)
        if self._runtime_authority is None:
            self._runtime_authority = authority
        elif authority != self._runtime_authority:
            raise PPORunnerError("environment runtime authority drifted between episodes")
        self._episode_seed = seed
        self._episode_base = self._episode_shaping = 0.0
        self._episode_actions = []
        self._episode_rewards = []
        self._episode_id = None
        self._active = True
        return self.env.current_decision_binding

    def collect_rollout(self, length: int | None = None) -> PPORolloutCollection:
        target = self.core.config.rollout_length if length is None else length
        if type(target) is not int or not 0 < target <= self.core.config.rollout_length:
            raise PPORunnerError("rollout length is outside PPO config")
        start = time.perf_counter()
        self._at_rollout_boundary = False
        transitions: list[LearnerTransition] = []
        episodes: list[PPOEpisodeMetrics] = []
        action_ids: list[str] = []
        reward_hashes: list[str] = []
        if not self._active:
            self.reset_episode(evaluation=False)
        while len(transitions) < target:
            binding = self.env.current_decision_binding
            processed = self._process(binding)
            tensor = processed.to_torch_cpu()
            mask = torch.tensor(binding.action_space.mask, dtype=torch.bool)
            if not bool(mask.any()):
                raise PPORunnerError("active Decision window has no catalog action")
            output = self.core.act(tensor, mask, evaluation=False)
            action_index = int(output.action_index.item())
            try:
                selected = LiveActionBinding.select(binding, action_index)
            except PPORunnerError:
                self.invalid_action_attempts += 1
                raise
            try:
                step = self.env.step_bound_action(action_index, binding)
            except NativeTrainingError as exc:
                if "stale" in str(exc):
                    self.stale_action_rejections += 1
                raise PPORunnerError(f"bound action submission failed: {exc}") from exc
            next_binding = self.env.current_decision_binding
            next_processed = self._process(next_binding)
            next_value = 0.0 if step.terminated else float(
                self.core.predict_value(next_processed.to_torch_cpu()).item()
            )
            record = step.reward_record
            identity = record.transition_identity
            if (
                identity.decision_before_hash != selected.decision_hash
                or identity.action_catalog_hash != selected.catalog_hash
                or selected.action_id not in (identity.submitted_action_ids or ())
                or identity.decision_after_hash
                != next_binding.observation.decision_semantic_hash
            ):
                raise PPORunnerError("reward/Decision/action binding misalignment")
            base, shaping = _reward_components(record.components)
            is_boundary = len(transitions) + 1 == target
            delta_ms = identity.sim_time_after_ms - identity.sim_time_before_ms
            discount_gamma = self.core.discount.effective_gamma(delta_time_ms=delta_ms)
            transition = LearnerTransition(
                schema_version=PPO_LEARNER_TRANSITION_SCHEMA_VERSION,
                episode_id=identity.episode_id,
                run_namespace=self.run_namespace,
                side=self.env.controlled_side,
                observation=processed.values,
                action_mask=tuple(binding.action_space.mask),
                decision_identity=selected.decision_hash,
                catalog_hash=selected.catalog_hash,
                action_encoder_hash=selected.action_encoder_hash,
                action_index=selected.action_index,
                action_id=selected.action_id,
                old_log_prob=float(output.log_prob.item()),
                value_prediction=float(output.value_prediction.item()),
                reward=float(step.reward),
                reward_record_hash=record.semantic_hash,
                base_reward_amount=base,
                shaping_reward_amount=shaping,
                terminated=step.terminated,
                truncated=step.truncated,
                truncation_source=record.truncation.source.value,
                effective_gamma=discount_gamma,
                next_observation=next_processed.values,
                next_decision_identity=identity.decision_after_hash,
                next_value_prediction=next_value,
                bootstrap_allowed=not step.terminated,
                trace_continues=(
                    not step.terminated and not step.truncated and not is_boundary
                ),
                preprocessor_hash=processed.preprocessor_hash,
            )
            transitions.append(transition)
            action_ids.append(selected.action_id)
            reward_hashes.append(record.semantic_hash)
            self.environment_steps += 1
            self._episode_id = identity.episode_id
            self._episode_base += base
            self._episode_shaping += shaping
            self._episode_actions.append(selected.action_id)
            self._episode_rewards.append(record.semantic_hash)
            if step.terminated or step.truncated:
                episodes.append(self._finish_episode(step))
                if len(transitions) < target:
                    self.reset_episode(evaluation=False)
        batch = PPORolloutBatch.from_transitions(transitions, self.core.config)
        self._at_rollout_boundary = True
        return PPORolloutCollection(
            batch=batch,
            episodes=tuple(episodes),
            action_ids=tuple(action_ids),
            reward_record_hashes=tuple(reward_hashes),
            invalid_action_attempts=self.invalid_action_attempts,
            stale_action_rejections=self.stale_action_rejections,
            elapsed_seconds=time.perf_counter() - start,
        )

    def train_update(self, length: int | None = None) -> tuple[PPORolloutCollection, PPOUpdateMetrics]:
        rollout = self.collect_rollout(length)
        metrics = self.core.update(rollout.batch)
        self.update_count = self.core.update_step
        if self.artifact_root is not None:
            self._append_jsonl("update_metrics.jsonl", {
                "record_type": "PPO_UPDATE", "update_index": self.update_count,
                "rollout_transitions": len(rollout.batch.provenance),
                "policy_loss": metrics.policy_loss,
                "value_loss": metrics.value_loss,
                "entropy": metrics.entropy,
                "total_loss": metrics.total_loss,
                "approx_kl": metrics.approx_kl,
                "clip_fraction": metrics.clip_fraction,
                "grad_norm": metrics.grad_norm,
                "clipped_grad_norm": metrics.clipped_grad_norm,
                "optimizer_steps": metrics.optimizer_steps,
            })
            for episode in rollout.episodes:
                self._append_jsonl("episode_metrics.jsonl", episode.to_dict())
            cadence = self.config.checkpoint_every_updates
            if cadence is not None and self.update_count % cadence == 0:
                self.save_checkpoint(
                    self.artifact_root / "checkpoints" / f"update-{self.update_count:06d}"
                )
        return rollout, metrics

    def train(self) -> PPORunSummary:
        """Run synchronously until one configured boundary is reached."""

        start_updates = self.core.update_step
        start_steps = self.environment_steps
        start_episodes = self.completed_episodes
        episode_values: list[PPOEpisodeMetrics] = []
        update_values: list[PPOUpdateMetrics] = []
        started = time.perf_counter()
        limits = self.config.run_limits
        while True:
            if limits.max_updates is not None and (
                self.core.update_step - start_updates >= limits.max_updates
            ):
                break
            if limits.max_environment_steps is not None and (
                self.environment_steps - start_steps >= limits.max_environment_steps
            ):
                break
            if limits.max_episodes is not None and (
                self.completed_episodes - start_episodes >= limits.max_episodes
            ):
                break
            length = self.core.config.rollout_length
            if limits.max_environment_steps is not None:
                remaining = limits.max_environment_steps - (
                    self.environment_steps - start_steps
                )
                length = min(length, remaining)
            rollout, update = self.train_update(length)
            episode_values.extend(rollout.episodes)
            update_values.append(update)
        elapsed = time.perf_counter() - started
        steps = self.environment_steps - start_steps
        summary = PPORunSummary(
            runner_config_hash=self.config.content_hash,
            updates=self.core.update_step - start_updates,
            environment_steps=steps,
            episodes=self.completed_episodes - start_episodes,
            episode_metrics=tuple(episode_values),
            update_metrics=tuple(update_values),
            transitions_per_second=steps / elapsed if elapsed > 0.0 else 0.0,
            elapsed_seconds=elapsed,
        )
        if self.artifact_root is not None:
            _atomic_json(self.artifact_root / "run_summary.json", summary.to_dict())
        return summary

    def evaluate_checkpoint(
        self, source: str | Path, episodes: int | None = None,
    ) -> PPOEvaluationReport:
        """Fail-closed load followed by deterministic, optimizer-free evaluation."""

        self.load_checkpoint(source)
        manifest = json.loads(
            (Path(source) / "manifest.json").read_text(encoding="utf-8")
        )
        return self.evaluate(
            episodes, checkpoint_identity=str(manifest["semantic_hash"])
        )

    def evaluate(self, episodes: int | None = None, *, checkpoint_identity: str = "IN_MEMORY") -> PPOEvaluationReport:
        count = self.config.evaluation_episodes if episodes is None else episodes
        if type(count) is not int or count <= 0:
            raise PPORunnerError("evaluation episode count must be positive")
        before_update = self.core.update_step
        before_sample = self.core.sample_step
        before_parameters = self.core.parameter_content_hash()
        training_rng = self.core.policy_sampling_generator.get_state().clone()
        results: list[PPOEpisodeMetrics] = []
        sequences: list[tuple[str, ...]] = []
        seeds: list[int] = []
        if self._active:
            raise PPORunnerError("evaluation requires an episode/rollout boundary")
        for _ in range(count):
            self.reset_episode(evaluation=True)
            seeds.append(self._episode_seed)
            while self._active:
                binding = self.env.current_decision_binding
                processed = self._process(binding)
                output = self.core.act(
                    processed.to_torch_cpu(),
                    torch.tensor(binding.action_space.mask, dtype=torch.bool),
                    evaluation=True,
                )
                index = int(output.action_index.item())
                selected = LiveActionBinding.select(binding, index)
                step = self.env.step_bound_action(index, binding)
                base, shaping = _reward_components(step.reward_record.components)
                self.environment_steps += 1
                self._episode_id = step.reward_record.transition_identity.episode_id
                self._episode_base += base
                self._episode_shaping += shaping
                self._episode_actions.append(selected.action_id)
                self._episode_rewards.append(step.reward_record.semantic_hash)
                if step.terminated or step.truncated:
                    result = self._finish_episode(step)
                    results.append(result)
                    sequences.append(result.action_ids)
        if self.core.update_step != before_update or self.core.sample_step != before_sample:
            raise PPORunnerError("evaluation mutated optimizer/sample counters")
        if self.core.parameter_content_hash() != before_parameters:
            raise PPORunnerError("evaluation mutated policy parameters")
        if not torch.equal(training_rng, self.core.policy_sampling_generator.get_state()):
            raise PPORunnerError("evaluation consumed training sampling RNG")
        report = PPOEvaluationReport(
            schema_version=PPO_EVALUATION_REPORT_SCHEMA_VERSION,
            runner_config_hash=self.config.content_hash,
            checkpoint_identity=checkpoint_identity,
            reward_identity=self.reward_identity,
            seed_set_hash=canonical_hash(seeds),
            episodes=tuple(results), action_sequences=tuple(sequences),
            parameter_hash=before_parameters,
            optimizer_update_step=before_update,
        )
        if self.artifact_root is not None:
            _atomic_json(self.artifact_root / "evaluation_report.json", report.to_dict())
        return report

    def save_checkpoint(self, target: str | Path) -> Path:
        if not self._at_rollout_boundary:
            raise PPORunnerError("v1 checkpoint requires a rollout/update boundary")
        if self._runtime_authority is None:
            raise PPORunnerError("cannot checkpoint before environment authority is known")
        destination = Path(target)
        if destination.exists():
            raise FileExistsError(f"checkpoint already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
        try:
            state_path = staging / "state.pt"
            torch.save(self.core.checkpoint_payload(self.reward_identity), state_path)
            state_hash = file_sha256(state_path)
            manifest = self._checkpoint_manifest(state_hash)
            _atomic_json(staging / "manifest.json", manifest)
            os.replace(staging, destination)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return destination

    def load_checkpoint(self, source: str | Path) -> None:
        if self._active or not self._at_rollout_boundary:
            raise PPORunnerError("v1 checkpoint restore requires a new runner boundary")
        if self._runtime_authority is None:
            self.reset_episode(evaluation=True)
            self._active = False
        root = Path(source)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        state_path = root / "state.pt"
        if file_sha256(state_path) != manifest.get("state_hash"):
            raise PPOCheckpointError("runner checkpoint state hash mismatch")
        expected = self._checkpoint_static_payload()
        if any(manifest.get(name) != value for name, value in expected.items()):
            raise PPOCheckpointError("runner checkpoint/runtime authority mismatch")
        semantic = dict(manifest)
        claimed = semantic.pop("semantic_hash", None)
        if claimed != canonical_hash(semantic):
            raise PPOCheckpointError("runner checkpoint manifest hash mismatch")
        try:
            payload = torch.load(state_path, map_location="cpu", weights_only=False)
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            raise PPOCheckpointError(f"runner checkpoint state is invalid: {exc}") from exc
        if not isinstance(payload, Mapping) or (
            manifest.get("optimizer_state_hash")
            != _stable_object_hash(payload.get("optimizer_state_dict"))
            or manifest.get("rng_state_hash") != _stable_object_hash({
                "torch_cpu": payload.get("torch_cpu_rng_state"),
                "policy_sampling": payload.get("policy_sampling_rng_state"),
                "minibatch_shuffle": payload.get("minibatch_shuffle_rng_state"),
            })
        ):
            raise PPOCheckpointError("runner checkpoint optimizer/RNG hash mismatch")
        self.core.restore_checkpoint(payload, reward_identity=self.reward_identity)
        if self.core.update_step != manifest.get("update_step") or (
            self.core.sample_step != manifest.get("sample_step")
        ):
            raise PPOCheckpointError("runner/core checkpoint counters differ")
        if self.core.parameter_content_hash() != manifest.get("parameter_hash"):
            raise PPOCheckpointError("runner checkpoint model parameter hash mismatch")
        self.episode_index = _counter(manifest["episode_index"], "episode_index")
        self.environment_steps = _counter(manifest["environment_steps"], "environment_steps")
        self.completed_episodes = _counter(manifest["completed_episodes"], "completed_episodes")
        self.update_count = self.core.update_step
        self._active = False
        self._at_rollout_boundary = True

    def _checkpoint_static_payload(self) -> dict[str, object]:
        return {
            "schema_version": PPO_RUNNER_CHECKPOINT_SCHEMA_VERSION,
            "runner_config_hash": self.config.content_hash,
            "runtime_authority": self.runtime_authority,
            "reward_identity": self.reward_identity.to_dict(),
            "reward_authority": self._reward_authority(),
            "seed_identity": self.core.seed.identity.to_dict(),
            "checkpoint_boundary": "ROLLOUT_UPDATE_BOUNDARY_RESUME_NEW_EPISODE",
        }

    def _checkpoint_manifest(self, state_hash: str) -> dict[str, object]:
        payload = self.core.checkpoint_payload(self.reward_identity)
        value = {
            **self._checkpoint_static_payload(),
            "state_hash": state_hash,
            "episode_index": self.episode_index,
            "environment_steps": self.environment_steps,
            "completed_episodes": self.completed_episodes,
            "update_step": self.core.update_step,
            "sample_step": self.core.sample_step,
            "parameter_hash": self.core.parameter_content_hash(),
            "optimizer_state_hash": _stable_object_hash(
                payload["optimizer_state_dict"]
            ),
            "rng_state_hash": _stable_object_hash({
                "torch_cpu": payload["torch_cpu_rng_state"],
                "policy_sampling": payload["policy_sampling_rng_state"],
                "minibatch_shuffle": payload["minibatch_shuffle_rng_state"],
            }),
        }
        value["semantic_hash"] = canonical_hash(value)
        return value

    def _reward_authority(self) -> dict[str, object]:
        shaping = self.env.shaping_policy
        return {
            "mode": self.config.reward_mode.value,
            "base": self.env.base_reward_policy.identity.to_dict(),
            "composite": (
                None if shaping is None else self.env.composite_reward_policy.to_dict()
            ),
            "shaping": None if shaping is None else shaping.to_dict(),
            "discount": self.env.discount_config.to_dict(),
        }

    def _process(self, binding: TrainingDecisionBinding) -> ProcessedLearnerObservation:
        result = self.preprocessor.preprocess(binding.observation)
        if result.decision_window_index != binding.action_space.decision_window_index:
            raise PPORunnerError("preprocessed observation/catalog window mismatch")
        return result

    def _finish_episode(self, step) -> PPOEpisodeMetrics:
        if not self._active or self._episode_id is None:
            raise PPORunnerError("episode completion is missing transition identity")
        winner = step.info.get("winner")
        if winner is not None:
            winner = str(winner).lower()
        outcome = (
            "TRUNCATED" if step.truncated
            else "WIN" if winner == self.env.controlled_side
            else "LOSS" if winner in {"red", "blue"}
            else "DRAW"
        )
        record = step.reward_record
        verification = self.env.verify_reward_rederivation()
        if not verification.matches:
            raise PPORunnerError("episode reward re-derivation mismatch")
        result = PPOEpisodeMetrics(
            schema_version=PPO_EPISODE_METRICS_SCHEMA_VERSION,
            episode_id=self._episode_id, run_namespace=self.run_namespace,
            side=self.env.controlled_side, episode_index=self.episode_index,
            environment_seed=self._episode_seed, outcome=outcome, winner=winner,
            terminal_reason=record.terminal_reason_code,
            terminated=step.terminated, truncated=step.truncated,
            truncation_source=record.truncation.source.value,
            decision_count=len(self._episode_actions),
            sim_time_ms=int(step.info["sim_time_ms"]),
            base_return=self._episode_base,
            shaping_return=self._episode_shaping,
            composite_return=self._episode_base + self._episode_shaping,
            reward_identity=self.reward_identity,
            action_ids=tuple(self._episode_actions),
            reward_record_hashes=tuple(self._episode_rewards),
            reward_rederivation_verified=True,
        )
        if self.artifact_root is not None:
            episode_root = self.artifact_root / "episodes" / f"{self.episode_index:06d}"
            publication = self.env.publish(episode_root, overwrite=False)
            write_reward_records_jsonl(
                episode_root / "joint_reward_records.native.jsonl",
                self.env.reward_records,
            )
            _atomic_json(episode_root / "ppo_episode_metrics.json", result.to_dict())
            self._ensure_run_manifest(publication.replay_path)
        self.completed_episodes += 1
        self.episode_index += 1
        self._active = False
        return result

    def _append_jsonl(self, name: str, value: Mapping[str, object]) -> None:
        assert self.artifact_root is not None
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        with (self.artifact_root / name).open("a", encoding="utf-8") as stream:
            stream.write(canonical_json(value) + "\n")

    def _ensure_run_manifest(self, replay_path: Path) -> None:
        assert self.artifact_root is not None
        target = self.artifact_root / "run_manifest.json"
        if target.exists():
            return
        _atomic_json(target, {
            "schema_version": PPO_RUN_PUBLICATION_SCHEMA_VERSION,
            "run_namespace": self.run_namespace,
            "runner": self.config.to_dict(),
            "ppo": self.core.config.to_dict(),
            "runtime_authority": self.runtime_authority,
            "reward_identity": self.reward_identity.to_dict(),
            "seed_identity": self.core.seed.identity.to_dict(),
            "artifact_policy": {
                "world_payload_in_learner_metrics": False,
                "green_payload_in_learner_metrics": False,
                "episode_replay_relative_example": str(
                    replay_path.relative_to(self.artifact_root)
                ),
            },
        })


def _reward_components(components: Sequence[object]) -> tuple[float, float]:
    base = shaping = None
    for component in components:
        key = (component.namespace, component.code)
        if key == ("base", "TERMINAL_OUTCOME"):
            base = float(component.amount)
        elif key == ("shaping", "EIGHT_FACTOR_POTENTIAL"):
            shaping = float(component.amount)
        else:
            raise PPORunnerError(f"unsupported reward component: {key}")
    if base is None:
        raise PPORunnerError("reward record is missing base component")
    return base, 0.0 if shaping is None else shaping


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical_json(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _counter(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise PPOCheckpointError(f"checkpoint {name} is invalid")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty trimmed text")
    return value


def _stable_object_hash(value: object) -> str:
    digest = hashlib.sha256()
    def update(item: object) -> None:
        if isinstance(item, torch.Tensor):
            tensor = item.detach().cpu().contiguous()
            digest.update(b"tensor\0")
            digest.update(str(tensor.dtype).encode("ascii"))
            digest.update(str(tuple(tensor.shape)).encode("ascii"))
            digest.update(tensor.numpy().tobytes(order="C"))
        elif isinstance(item, Mapping):
            digest.update(b"mapping\0")
            for key in sorted(item, key=lambda current: str(current)):
                update(str(key))
                update(item[key])
        elif isinstance(item, (tuple, list)):
            digest.update(b"sequence\0")
            for child in item:
                update(child)
        elif item is None or isinstance(item, (str, int, float, bool)):
            digest.update(canonical_json(item).encode("utf-8"))
            digest.update(b"\0")
        else:
            raise PPOCheckpointError(
                f"unsupported checkpoint hash value: {type(item).__name__}"
            )
    update(value)
    return digest.hexdigest()


__all__ = [
    "LiveActionBinding", "NativePPORunner", "PPOEpisodeMetrics",
    "PPOEvaluationReport", "PPORolloutCollection", "PPORunSummary",
    "PPORunnerError",
    "PPO_EPISODE_METRICS_SCHEMA_VERSION", "PPO_EVALUATION_REPORT_SCHEMA_VERSION",
    "PPO_RUN_PUBLICATION_SCHEMA_VERSION", "PPO_RUNNER_CHECKPOINT_SCHEMA_VERSION",
]
