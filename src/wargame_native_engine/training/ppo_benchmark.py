"""Phase-7 paired Base-vs-EightFactor PPO pilot orchestration.

This module composes existing runner, PPO, reward, evaluation, and replay
authorities.  It does not implement learner mathematics or adjudication.
"""

from __future__ import annotations

from copy import deepcopy
import csv
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import tempfile
import time
from typing import Iterable, Mapping, Sequence

from wargame_native_engine.agents import NoOpAgent
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash, file_sha256
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.training.env import NativeTrainingEnv
from wargame_native_engine.training.ppo_benchmark_config import (
    BENCHMARK_GREEN_TEMPLATE_POLICY_HASH,
    BENCHMARK_GREEN_UNIT_DEFAULTS,
    PPOBenchmarkRunSpec,
    PPOBenchmarkScenario,
    PPOBenchmarkTreatment,
    PPOTrainingBenchmarkConfig,
    PPOTrainingBenchmarkConfigError,
    load_ppo_training_benchmark_config,
    normalized_trapezoidal_auc,
)
from wargame_native_engine.training.ppo_config import (
    PPOAuthorityReference,
    load_ppo_algorithm_config,
)
from wargame_native_engine.training.ppo_core import NativeMaskedPPO, PPORewardIdentity, torch
from wargame_native_engine.training.ppo_parallel_runner import NativeParallelPPORunner
from wargame_native_engine.training.ppo_parallel_runner_config import (
    PPOParallelRunnerConfig,
    build_ppo_parallel_runner_config,
)
from wargame_native_engine.training.ppo_runner_config import (
    PPORunnerConfig,
    build_ppo_runner_config,
)
from wargame_native_engine.training.ppo_vector_runner_config import (
    PPOVectorRunnerConfig,
    build_ppo_vector_runner_config,
)


PPO_TRAINING_BENCHMARK_RESULT_SCHEMA = "native-ppo-training-benchmark-result/1.0"
PPO_BENCHMARK_RUN_MANIFEST_SCHEMA = "native-ppo-benchmark-run-manifest/1.0"
PPO_BENCHMARK_COMPLETION_SCHEMA = "native-ppo-benchmark-run-completion/1.0"

_ROOT = Path(__file__).resolve().parents[3]
_BASE_RUNNER_PATH = _ROOT / "config/training/native_ppo_runner_v1.json"
_VECTOR_RUNNER_PATH = _ROOT / "config/training/native_ppo_vector_runner_v1.json"
_PARALLEL_RUNNER_PATH = _ROOT / "config/training/native_ppo_parallel_runner_v1.json"


class PPOTrainingBenchmarkError(RuntimeError):
    """A benchmark run failed its frozen protocol or artifact integrity."""


@dataclass(frozen=True, slots=True)
class BenchmarkNativeEnvFactory:
    """Spawn-picklable child-local Native environment factory."""

    scenario_path: str
    storage_root: str
    side: str
    composite: bool
    max_steps: int
    delta_time_seconds: int
    green_template_policy_hash: str = BENCHMARK_GREEN_TEMPLATE_POLICY_HASH

    def __call__(self) -> NativeTrainingEnv:
        if self.green_template_policy_hash != BENCHMARK_GREEN_TEMPLATE_POLICY_HASH:
            raise PPOTrainingBenchmarkError("worker benchmark GREEN template authority drift")
        scenario = load_scenario(self.scenario_path)
        template = complete_benchmark_green_template(scenario)
        environment_factory = lambda: SourceCompatibleEnvironment(  # child-local
            GameSession(NativeEngineBackend(scenario)), template=template
        )
        return NativeTrainingEnv(
            environment_factory,
            controlled_side=self.side.lower(),
            opponent=NoOpAgent("native-no-op-fixed-opponent-v1"),
            storage_root=Path(self.storage_root),
            max_actions=256,
            max_steps=self.max_steps,
            delta_time_seconds=self.delta_time_seconds,
            enable_eight_factor_shaping=self.composite,
        )


@dataclass(frozen=True, slots=True)
class BenchmarkRunnerAuthorities:
    base: PPORunnerConfig
    vector: PPOVectorRunnerConfig
    parallel: PPOParallelRunnerConfig

    def control_payload(self) -> dict[str, object]:
        """Fields that must match across paired treatment arms."""

        return {
            "purpose": self.base.purpose,
            "mode": self.base.mode,
            "trained_side": self.base.trained_side,
            "opponent": [
                self.base.opponent_kind,
                self.base.opponent_id,
                self.base.opponent_deterministic,
            ],
            "ppo": self.base.ppo_config.to_dict(),
            "seed": self.base.seed_config.to_dict(),
            "worker_index": self.base.worker_index,
            "env_index": self.base.env_index,
            "limits": self.base.run_limits.to_dict(),
            "rollout_model_mode": self.base.rollout_model_mode,
            "device": self.base.device,
            "num_envs": self.vector.num_envs,
            "transitions_per_update": self.vector.transitions_per_update,
            "collection_mode": self.vector.collection_mode,
            "flatten_order": self.vector.flatten_order,
            "parallel_execution": self.parallel.execution_mode,
            "parallel_start": self.parallel.start_method,
            "num_workers": self.parallel.num_workers,
            "worker_protocol": self.parallel.worker_protocol.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class BenchmarkDryRunPlan:
    benchmark_hash: str
    runs: tuple[PPOBenchmarkRunSpec, ...]
    transitions_per_run: int
    total_transitions: int
    evaluations_per_run: int
    evaluation_episodes_per_checkpoint: int
    reference_transitions_per_second: float
    estimated_training_seconds: float

    def to_dict(self) -> dict[str, object]:
        return {
            "benchmark_hash": self.benchmark_hash,
            "runs": [item.to_dict() for item in self.runs],
            "planned_run_count": len(self.runs),
            "transitions_per_run": self.transitions_per_run,
            "total_training_transitions": self.total_transitions,
            "evaluations_per_run": self.evaluations_per_run,
            "evaluation_episodes_per_checkpoint": self.evaluation_episodes_per_checkpoint,
            "reference_transitions_per_second": self.reference_transitions_per_second,
            "estimated_training_seconds": self.estimated_training_seconds,
            "estimate_only": True,
        }


def complete_benchmark_green_template(scenario):
    """Add explicit analysis-only facts without changing Engine or actor inputs."""

    template = source_frame_template_for_scenario(scenario)
    document = {
        "operators": [deepcopy(item.raw) for item in template.operators],
        "passengers": [deepcopy(item.raw) for item in template.passengers],
        "time": deepcopy(template.time.raw),
        "jm_points": [deepcopy(item.raw) for item in template.jm_points],
        "cities": [deepcopy(item.raw) for item in template.cities],
        "scores": template.scores.to_dict(),
        "landmarks": deepcopy(template.landmarks.raw),
        "terrain_id": template.terrain_id,
        "judge_info": deepcopy(template.judge_info),
        "role_and_grouping_info": deepcopy(template.role_and_grouping_info),
        "communication": deepcopy(template.communication),
        "scenario_id": template.scenario_id,
        "valid_actions": deepcopy(template.valid_actions),
        "actions": list(deepcopy(template.actions)),
    }
    for unit in document["operators"] + document["passengers"]:
        for name, value in BENCHMARK_GREEN_UNIT_DEFAULTS.items():
            unit[name] = deepcopy(value)
    return SourceFrameImporter().from_dict(
        document, source_hash=BENCHMARK_GREEN_TEMPLATE_POLICY_HASH
    )


def build_benchmark_runner_authorities(
    protocol: PPOTrainingBenchmarkConfig,
    treatment: PPOBenchmarkTreatment,
    *,
    train_seed_index: int,
    trained_side: str,
) -> BenchmarkRunnerAuthorities:
    ppo = load_ppo_algorithm_config()
    base_document = json.loads(_BASE_RUNNER_PATH.read_text(encoding="utf-8"))
    base_document["trained_side"] = trained_side
    base_document["reward_mode"] = treatment.reward_mode.value
    base_document["worker_index"] = train_seed_index
    base_document["env_index"] = 0
    base_document["run_limits"] = {
        "max_updates": math.ceil(protocol.transitions_per_run / ppo.rollout_length),
        "max_environment_steps": None,
        "max_episodes": None,
    }
    base_document["checkpoint_every_updates"] = None
    base_document["evaluation_episodes"] = protocol.evaluation_episodes_per_checkpoint
    base = build_ppo_runner_config(base_document, ppo=ppo)

    vector_document = json.loads(_VECTOR_RUNNER_PATH.read_text(encoding="utf-8"))
    vector_document["base_runner_config_ref"]["hash"] = base.content_hash
    vector_document["num_envs"] = protocol.num_envs
    vector_document["trained_side"] = trained_side
    vector_document["reward_mode"] = treatment.reward_mode.value
    vector = build_ppo_vector_runner_config(vector_document, base_runner=base, ppo=ppo)

    parallel_document = json.loads(_PARALLEL_RUNNER_PATH.read_text(encoding="utf-8"))
    parallel_document["base_vector_runner_ref"]["hash"] = vector.content_hash
    parallel_document["num_envs"] = protocol.num_envs
    parallel_document["num_workers"] = protocol.num_workers
    parallel_document["trained_side"] = trained_side
    parallel_document["reward_mode"] = treatment.reward_mode.value
    parallel = build_ppo_parallel_runner_config(
        parallel_document, vector=vector, ppo=ppo
    )
    return BenchmarkRunnerAuthorities(base, vector, parallel)


def require_only_treatment_difference(
    protocol: PPOTrainingBenchmarkConfig,
    *,
    train_seed_index: int,
    trained_side: str,
) -> None:
    first, second = (
        build_benchmark_runner_authorities(
            protocol, treatment, train_seed_index=train_seed_index,
            trained_side=trained_side,
        )
        for treatment in protocol.treatments
    )
    if first.control_payload() != second.control_payload():
        raise PPOTrainingBenchmarkConfigError(
            "Base/Shaping runner controls differ beyond reward treatment"
        )


class PPOTrainingBenchmarkRunner:
    """Sequential paired pilot runner; each run owns parallel env workers."""

    def __init__(
        self,
        protocol: PPOTrainingBenchmarkConfig | None = None,
        *,
        output_root: str | Path,
        reference_transitions_per_second: float = 20.59,
    ) -> None:
        self.protocol = load_ppo_training_benchmark_config() if protocol is None else protocol
        self.output_root = Path(output_root)
        if not math.isfinite(reference_transitions_per_second) or reference_transitions_per_second <= 0:
            raise ValueError("reference throughput must be positive finite")
        self.reference_transitions_per_second = float(reference_transitions_per_second)
        self.source_manifest = build_experiment_source_manifest()
        for seed_index in self.protocol.train_seed_indices:
            for side in self.protocol.trained_sides:
                require_only_treatment_difference(
                    self.protocol, train_seed_index=seed_index, trained_side=side
                )

    def dry_run(self) -> BenchmarkDryRunPlan:
        runs = self.protocol.run_matrix()
        return BenchmarkDryRunPlan(
            self.protocol.content_hash, runs, self.protocol.transitions_per_run,
            self.protocol.planned_transitions,
            len(self.protocol.evaluation_cadence_transitions),
            self.protocol.evaluation_episodes_per_checkpoint,
            self.reference_transitions_per_second,
            self.protocol.planned_transitions / self.reference_transitions_per_second,
        )

    def dry_run_payload(self) -> dict[str, object]:
        payload = self.dry_run().to_dict()
        if self.protocol.stage == "FULL":
            payload["full_cost"] = self.full_cost_report()
            payload["rollout_collection_lengths"] = list(
                rollout_collection_lengths(
                    self.protocol.evaluation_cadence_transitions,
                    load_ppo_algorithm_config().rollout_length,
                )
            )
        return payload

    def execute(self, *, skip_completed: bool = True) -> dict[str, object]:
        self.output_root.mkdir(parents=True, exist_ok=True)
        if self.protocol.stage == "FULL":
            preflight = self.preflight(run_transition_smoke=True)
            if preflight["status"] != "PASS":
                raise PPOTrainingBenchmarkError("FULL preflight failed")
            _atomic_json(self.output_root / "experiment_source_manifest.json", self.source_manifest)
            marker = self.output_root / "FULL_EXPERIMENT_STARTED.json"
            if not marker.exists():
                _atomic_json(marker, {
                    "status": "FULL_EXPERIMENT_STARTED",
                    "benchmark_hash": self.protocol.content_hash,
                    "source_manifest_hash": self.source_manifest["semantic_hash"],
                    "planned_runs": len(self.protocol.run_matrix()),
                    "operational_started_unix_ns": time.time_ns(),
                })
        _atomic_json(self.output_root / "benchmark_protocol.json", self.protocol.to_dict())
        _atomic_json(self.output_root / "dry_run_plan.json", self.dry_run_payload())
        _write_csv(
            self.output_root / "run_matrix.csv",
            [item.to_dict() for item in self.protocol.run_matrix()],
            ("run_id", "scenario_id", "scenario_hash", "train_seed_index",
             "trained_side", "treatment_id"),
        )
        summaries = []
        failures = []
        for spec in self.protocol.run_matrix():
            target = self.output_root / "runs" / spec.run_id
            if skip_completed and self._is_complete(target, spec):
                summaries.append(json.loads((target / "final_summary.json").read_text(encoding="utf-8")))
                continue
            if target.exists():
                raise PPOTrainingBenchmarkError(
                    f"partial/corrupt run is not resumable: {target}"
                )
            try:
                summaries.append(self._execute_run(spec, target))
            except BaseException as exc:
                failure = {
                    "run_id": spec.run_id, "status": "FAILED",
                    "error_type": type(exc).__name__, "error": str(exc),
                }
                failures.append(failure)
                target.mkdir(parents=True, exist_ok=True)
                _atomic_json(target / "failure.json", failure)
                break
        result = aggregate_benchmark_results(self.protocol, summaries, failures)
        _publish_aggregate(self.output_root, result)
        return result

    def full_cost_report(self) -> dict[str, object]:
        if self.protocol.stage != "FULL":
            raise PPOTrainingBenchmarkError("FULL cost report requires FULL protocol")
        runs_per_treatment = len(self.protocol.run_matrix()) // 2
        base_tps = 17.30095801073661
        shaping_tps = 14.65852060816415
        base_seconds = runs_per_treatment * self.protocol.transitions_per_run / base_tps
        shaping_seconds = runs_per_treatment * self.protocol.transitions_per_run / shaping_tps
        evaluation_episodes = (
            len(self.protocol.run_matrix())
            * len(self.protocol.evaluation_cadence_transitions)
            * self.protocol.evaluation_episodes_per_checkpoint
        )
        # Conservative Pilot-derived estimate, including repeated spawn startup.
        evaluation_seconds = evaluation_episodes * 0.50
        orchestration_margin = 0.10 * (base_seconds + shaping_seconds + evaluation_seconds)
        total_seconds = base_seconds + shaping_seconds + evaluation_seconds + orchestration_margin
        # Pilot artifact scaling after FULL episode-package retention predicts
        # about 8.1 GB (worker session facts dominate); keep a conservative
        # 10 GB gate estimate rather than claiming only final-checkpoint size.
        disk_bytes = 10_000_000_000
        return {
            "schema_version": "native-ppo-full-cost-estimate/1.0",
            "basis": "PHASE7_PILOT_MEASURED_THROUGHPUT_PLUS_10_PERCENT_MARGIN",
            "runs": len(self.protocol.run_matrix()),
            "transitions_per_run": self.protocol.transitions_per_run,
            "total_training_transitions": self.protocol.planned_transitions,
            "evaluation_checkpoints": len(self.protocol.run_matrix()) * len(self.protocol.evaluation_cadence_transitions),
            "evaluation_episodes": evaluation_episodes,
            "base_training_seconds": base_seconds,
            "shaping_training_seconds": shaping_seconds,
            "evaluation_seconds": evaluation_seconds,
            "orchestration_margin_seconds": orchestration_margin,
            "estimated_total_seconds": total_seconds,
            "estimated_disk_bytes": disk_bytes,
            "disk_estimate_basis": "PHASE7_PILOT_RETAINED_WORKER_FACTS_LINEAR_SCALE_WITH_24_PERCENT_MARGIN",
            "wall_time_limit_seconds": 24 * 3600,
            "disk_limit_bytes": 20_000_000_000,
            "replay_verification_cost_basis": "PHASE6E_SMOKE_BELOW_20_PERCENT",
            "replay_verification_estimated_percent": 0.6,
            "replay_cost_gate": "PASS",
            "launch_gate": (
                "PASS" if total_seconds <= 24 * 3600 and disk_bytes <= 20_000_000_000
                else "STOP"
            ),
        }

    def preflight(self, *, run_transition_smoke: bool = True) -> dict[str, object]:
        if self.protocol.stage != "FULL":
            raise PPOTrainingBenchmarkError("FULL preflight requires FULL protocol")
        self.output_root.mkdir(parents=True, exist_ok=True)
        cost = self.full_cost_report()
        free_bytes = shutil.disk_usage(self.output_root).free
        checks: dict[str, object] = {
            "config_hash": self.protocol.content_hash,
            "run_count": len(self.protocol.run_matrix()),
            "treatment_diff": "PASS",
            "scenario_hashes": {
                item.scenario_id: item.scenario_hash for item in self.protocol.scenarios
            },
            "evaluation_seed_set_hash": self.protocol.eval_seed_set_hash,
            "evaluation_seed_count": len(set(self._evaluation_seeds())),
            "artifact_root_writable": os.access(self.output_root, os.W_OK),
            "disk_free_bytes": free_bytes,
            "disk_estimate_bytes": cost["estimated_disk_bytes"],
            "source_manifest_hash": self.source_manifest["semantic_hash"],
            "cost": cost,
        }
        initial_pairs = []
        for scenario in self.protocol.scenarios:
            for seed_index in self.protocol.train_seed_indices:
                base = NativeMaskedPPO(seed_stream_index=seed_index).parameter_content_hash()
                shaping = NativeMaskedPPO(seed_stream_index=seed_index).parameter_content_hash()
                initial_pairs.append({
                    "scenario": scenario.scenario_id,
                    "train_seed_index": seed_index,
                    "base": base,
                    "shaping": shaping,
                    "match": base == shaping,
                })
        checks["paired_initial_parameters"] = initial_pairs
        smoke_rows = []
        if run_transition_smoke:
            smoke_parent = self.output_root / ".full-preflight-smoke"
            if smoke_parent.exists():
                shutil.rmtree(smoke_parent)
            smoke_parent.mkdir(parents=True)
            try:
                for scenario in self.protocol.scenarios:
                    pair = []
                    for treatment in self.protocol.treatments:
                        spec = next(
                            item for item in self.protocol.run_matrix()
                            if item.scenario_id == scenario.scenario_id
                            and item.train_seed_index == 0
                            and item.treatment_id == treatment.treatment_id
                        )
                        authorities = build_benchmark_runner_authorities(
                            self.protocol, treatment,
                            train_seed_index=0, trained_side="RED",
                        )
                        core = NativeMaskedPPO(seed_stream_index=0)
                        target = smoke_parent / scenario.scenario_id / treatment.treatment_id
                        runner = NativeParallelPPORunner(
                            self._factories(spec, scenario, treatment, target / "workers"),
                            core, parallel_config=authorities.parallel,
                            vector_config=authorities.vector,
                            base_runner_config=authorities.base,
                            run_namespace=f"full-preflight-{spec.run_id}",
                            artifact_root=None,
                        )
                        try:
                            collection = runner.collect_rollout(1)
                        finally:
                            runner.close()
                        record = next(
                            item for segment in collection.vector_rollout.segments
                            for item in segment
                        )
                        transition = record.transition
                        row = {
                            "scenario": scenario.scenario_id,
                            "treatment": treatment.treatment_id,
                            "transitions": collection.vector_rollout.total_transitions,
                            "decision_identity": transition.decision_identity,
                            "catalog_hash": transition.catalog_hash,
                            "action_index": transition.action_index,
                            "action_id": transition.action_id,
                            "observation_hash": canonical_hash(list(transition.observation)),
                            "mask_hash": canonical_hash(list(transition.action_mask)),
                            "reward": transition.reward,
                            "base_reward": transition.base_reward_amount,
                            "shaping_reward": transition.shaping_reward_amount,
                            "invalid": sum(collection.invalid_action_attempts),
                            "stale": sum(collection.stale_action_attempts),
                        }
                        pair.append(row)
                        smoke_rows.append(row)
                    for name in (
                        "decision_identity", "catalog_hash", "action_index", "action_id",
                        "observation_hash", "mask_hash", "base_reward",
                    ):
                        if pair[0][name] != pair[1][name]:
                            raise PPOTrainingBenchmarkError(
                                f"FULL preflight treatment isolation failed: {scenario.scenario_id}:{name}"
                            )
            finally:
                shutil.rmtree(smoke_parent, ignore_errors=True)
        checks["transition_smoke"] = smoke_rows
        status = "PASS" if (
            cost["launch_gate"] == "PASS"
            and free_bytes >= int(cost["estimated_disk_bytes"])
            and checks["artifact_root_writable"]
            and checks["evaluation_seed_count"] == 30
            and all(item["match"] for item in initial_pairs)
            and all(item["transitions"] == 1 and item["invalid"] == item["stale"] == 0
                    for item in smoke_rows)
        ) else "FAIL"
        result = {
            "schema_version": "native-ppo-full-preflight/1.0",
            "benchmark_hash": self.protocol.content_hash,
            "status": status,
            "checks": checks,
        }
        _atomic_json(self.output_root / "full_preflight.json", result)
        return result

    def aggregate_existing(self) -> dict[str, object]:
        summaries = []
        failures = []
        for spec in self.protocol.run_matrix():
            target = self.output_root / "runs" / spec.run_id
            if self._is_complete(target, spec):
                summaries.append(json.loads((target / "final_summary.json").read_text(encoding="utf-8")))
            elif (target / "failure.json").is_file():
                failures.append(json.loads((target / "failure.json").read_text(encoding="utf-8")))
        result = aggregate_benchmark_results(self.protocol, summaries, failures)
        _publish_aggregate(self.output_root, result)
        return result

    def _execute_run(self, spec: PPOBenchmarkRunSpec, target: Path) -> dict[str, object]:
        scenario_spec = next(item for item in self.protocol.scenarios if item.scenario_id == spec.scenario_id)
        treatment = next(item for item in self.protocol.treatments if item.treatment_id == spec.treatment_id)
        authorities = build_benchmark_runner_authorities(
            self.protocol, treatment, train_seed_index=spec.train_seed_index,
            trained_side=spec.trained_side,
        )
        target.mkdir(parents=True)
        initial_core = NativeMaskedPPO(seed_stream_index=spec.train_seed_index)
        initial_parameter_hash = initial_core.parameter_content_hash()
        manifest = {
            "schema_version": PPO_BENCHMARK_RUN_MANIFEST_SCHEMA,
            "benchmark_hash": self.protocol.content_hash,
            **spec.to_dict(),
            "stage": self.protocol.stage,
            "ppo_config_hash": initial_core.config.content_hash,
            "seed_config_hash": initial_core.seed.content_hash,
            "seed_stream_index": spec.train_seed_index,
            "reward": treatment.to_dict(),
            "runner": {
                "base_hash": authorities.base.content_hash,
                "vector_hash": authorities.vector.content_hash,
                "parallel_hash": authorities.parallel.content_hash,
            },
            "opponent": self.protocol.opponent.to_dict(),
            "green_template_policy_hash": self.protocol.green_template_policy_hash,
            "initial_parameter_hash": initial_parameter_hash,
            "training_budget": self.protocol.transitions_per_run,
            "evaluation_seed_set_hash": self.protocol.eval_seed_set_hash,
            "source_manifest_hash": self.source_manifest["semantic_hash"],
            "evaluation_grid": list(self.protocol.evaluation_cadence_transitions),
            "episode_artifact_retention": self.protocol.episode_artifact_retention,
        }
        manifest["semantic_hash"] = canonical_hash(manifest)
        _atomic_json(target / "run_manifest.json", manifest)

        eval_rows = []
        evaluation_started = time.perf_counter()
        transition_zero = self._evaluate(
            spec, scenario_spec, treatment, authorities, initial_core,
            environment_transitions=0, target=target / "evaluation" / "0000000000",
        )
        eval_rows.append(transition_zero)
        _append_jsonl(target / "evaluation_metrics.jsonl", transition_zero)
        evaluation_seconds = time.perf_counter() - evaluation_started
        in_training_evaluation_seconds = 0.0

        factories = self._factories(spec, scenario_spec, treatment, target / "workers")
        runner = NativeParallelPPORunner(
            factories, initial_core,
            parallel_config=authorities.parallel,
            vector_config=authorities.vector,
            base_runner_config=authorities.base,
            run_namespace=f"benchmark-{spec.run_id}",
            artifact_root=target / "episodes",
        )
        started = time.perf_counter()
        update_rows = []
        episodes = []
        invalid = stale = 0
        replay_checked = False
        try:
            remaining = self.protocol.transitions_per_run
            scheduled = iter(self.protocol.evaluation_cadence_transitions[1:])
            next_evaluation = next(scheduled, None)
            while remaining:
                until_evaluation = (
                    remaining if next_evaluation is None else
                    next_evaluation - runner.environment_steps
                )
                if until_evaluation <= 0:
                    raise PPOTrainingBenchmarkError("evaluation grid did not advance")
                length = min(
                    remaining, initial_core.config.rollout_length,
                    until_evaluation,
                )
                collection, metrics, update_seconds = runner.train_update(length)
                remaining -= collection.vector_rollout.total_transitions
                invalid += sum(collection.invalid_action_attempts)
                stale += sum(collection.stale_action_attempts)
                episodes.extend(item.to_dict() for item in collection.episodes)
                row = {
                    "schema_version": "native-ppo-benchmark-update-metrics/1.0",
                    "run_id": spec.run_id,
                    "environment_transitions": runner.environment_steps,
                    "update_step": initial_core.update_step,
                    "policy_loss": metrics.policy_loss,
                    "value_loss": metrics.value_loss,
                    "entropy": metrics.entropy,
                    "total_loss": metrics.total_loss,
                    "approx_kl": metrics.approx_kl,
                    "clip_fraction": metrics.clip_fraction,
                    "grad_norm": metrics.grad_norm,
                    "rollout_seconds": collection.timing.total_seconds,
                    "preprocessing_seconds": collection.timing.preprocessing_worker_sum_seconds,
                    "engine_critical_seconds": collection.timing.engine_critical_path_seconds,
                    "reward_replay_seconds": collection.timing.reward_replay_worker_sum_seconds,
                    "update_seconds": update_seconds,
                    "invalid_action_attempts": sum(collection.invalid_action_attempts),
                    "stale_action_attempts": sum(collection.stale_action_attempts),
                }
                _require_finite_metrics(row)
                update_rows.append(row)
                _append_jsonl(target / "train_metrics.jsonl", row)
                if not replay_checked and collection.episodes:
                    replay_checked = self._verify_first_replay(
                        scenario_spec, collection.episodes, target / "episodes"
                    )
                if next_evaluation == runner.environment_steps and (
                    next_evaluation != self.protocol.transitions_per_run
                ):
                    eval_started = time.perf_counter()
                    evaluation = self._evaluate(
                        spec, scenario_spec, treatment, authorities, initial_core,
                        environment_transitions=next_evaluation,
                        target=target / "evaluation" / f"{next_evaluation:010d}",
                    )
                    elapsed_evaluation = time.perf_counter() - eval_started
                    evaluation_seconds += elapsed_evaluation
                    in_training_evaluation_seconds += elapsed_evaluation
                    eval_rows.append(evaluation)
                    _append_jsonl(target / "evaluation_metrics.jsonl", evaluation)
                    print(
                        f"FULL_PROGRESS run={spec.run_id} scenario={spec.scenario_id} "
                        f"seed={spec.train_seed_index} treatment={spec.treatment_id} "
                        f"transitions={runner.environment_steps}/{self.protocol.transitions_per_run} "
                        f"win_rate={evaluation['win_rate']:.6f}",
                        flush=True,
                    )
                    next_evaluation = next(scheduled, None)
        finally:
            runner.close()
        training_seconds = time.perf_counter() - started - in_training_evaluation_seconds
        if remaining or invalid or stale or not replay_checked:
            raise PPOTrainingBenchmarkError(
                "training budget/action/replay integrity validation failed"
            )
        checkpoint = self._save_checkpoint(
            target / "checkpoints" / "final", initial_core, treatment,
            runner.reward_identity, spec, runner.environment_steps,
        )
        self._verify_checkpoint(
            target / "checkpoints" / "final", treatment,
            runner.reward_identity, spec.train_seed_index,
            initial_core.parameter_content_hash(),
        )
        eval_started = time.perf_counter()
        final_eval = self._evaluate(
            spec, scenario_spec, treatment, authorities, initial_core,
            environment_transitions=self.protocol.transitions_per_run,
            target=target / "evaluation" / f"{self.protocol.transitions_per_run:010d}",
        )
        evaluation_seconds += time.perf_counter() - eval_started
        eval_rows.append(final_eval)
        _append_jsonl(target / "evaluation_metrics.jsonl", final_eval)
        if tuple(int(item["environment_transitions"]) for item in eval_rows) != (
            self.protocol.evaluation_cadence_transitions
        ):
            raise PPOTrainingBenchmarkError("scheduled evaluation grid is incomplete")
        auc = normalized_trapezoidal_auc(tuple(
            (int(item["environment_transitions"]), float(item["win_rate"]))
            for item in eval_rows
        ))
        shaping_values = [float(item["episode"]["shaping_return"]) for item in episodes]
        t50 = _threshold_transition(eval_rows, 0.50)
        t75 = _threshold_transition(eval_rows, 0.75)
        if self.protocol.episode_artifact_retention == "FIRST_ENGINE_REPLAY_VERIFIED_ONLY":
            self._retain_first_episode_package(target / "episodes")
        summary = {
            "schema_version": "native-ppo-benchmark-run-summary/1.0",
            **spec.to_dict(),
            "status": "COMPLETE",
            "initial_parameter_hash": initial_parameter_hash,
            "final_parameter_hash": initial_core.parameter_content_hash(),
            "environment_transitions": runner.environment_steps,
            "updates": initial_core.update_step,
            "episodes_completed": len(episodes),
            "invalid_action_attempts": invalid,
            "stale_action_attempts": stale,
            "replay_mismatches": 0,
            "final_win_rate": final_eval["win_rate"],
            "final_base_return": final_eval["base_return_mean"],
            "final_shaping_return": final_eval["shaping_return_mean"],
            "final_composite_return": final_eval["composite_return_mean"],
            "win_rate_auc": auc,
            "t50": t50,
            "t75": t75,
            "final_episode_length": final_eval["episode_length_mean"],
            "training_wall_time": training_seconds,
            "evaluation_wall_time": evaluation_seconds,
            "total_wall_time": training_seconds + evaluation_seconds,
            "transitions_per_second": runner.environment_steps / training_seconds,
            "shaping_signal_nonzero": any(abs(item) > 1e-12 for item in shaping_values),
            "checkpoint_manifest_hash": checkpoint["semantic_hash"],
            "evaluation_seed_set_hash": self.protocol.eval_seed_set_hash,
            "evaluation_points": eval_rows,
        }
        _require_finite_metrics(summary)
        _atomic_json(target / "final_summary.json", summary)
        completion = {
            "schema_version": PPO_BENCHMARK_COMPLETION_SCHEMA,
            "benchmark_hash": self.protocol.content_hash,
            "run_id": spec.run_id,
            "run_manifest_hash": manifest["semantic_hash"],
            "summary_hash": file_sha256(target / "final_summary.json"),
            "checkpoint_manifest_hash": checkpoint["semantic_hash"],
            "status": "COMPLETE",
        }
        if self.protocol.stage == "FULL":
            completion.update({
                "train_metrics_hash": file_sha256(target / "train_metrics.jsonl"),
                "evaluation_metrics_hash": file_sha256(target / "evaluation_metrics.jsonl"),
                "run_manifest_file_hash": file_sha256(target / "run_manifest.json"),
            })
        completion["semantic_hash"] = canonical_hash(completion)
        _atomic_json(target / "completion.json", completion)
        return summary

    def _verify_checkpoint(
        self, source: Path, treatment: PPOBenchmarkTreatment,
        reward_identity: PPORewardIdentity, seed_stream_index: int,
        expected_parameter_hash: str,
    ) -> None:
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        semantic = dict(manifest)
        claimed = semantic.pop("semantic_hash", None)
        if claimed != canonical_hash(semantic) or file_sha256(source / "state.pt") != manifest.get("state_hash"):
            raise PPOTrainingBenchmarkError("checkpoint artifact integrity mismatch")
        try:
            payload = torch.load(source / "state.pt", map_location="cpu", weights_only=False)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise PPOTrainingBenchmarkError(f"checkpoint load failed: {exc}") from exc
        restored = NativeMaskedPPO(seed_stream_index=seed_stream_index)
        restored.restore_checkpoint(payload, reward_identity=reward_identity)
        if restored.parameter_content_hash() != expected_parameter_hash:
            raise PPOTrainingBenchmarkError("checkpoint restored parameter hash mismatch")

    def _evaluate(
        self, spec, scenario_spec, treatment, training_authorities, source_core,
        *, environment_transitions: int, target: Path,
    ) -> dict[str, object]:
        # Evaluation seed coordinates are fixed across treatments and train seeds.
        eval_authorities = build_benchmark_runner_authorities(
            self.protocol, treatment, train_seed_index=0, trained_side=spec.trained_side
        )
        eval_core = NativeMaskedPPO(seed_stream_index=spec.train_seed_index)
        eval_core.model.load_state_dict(deepcopy(source_core.model.state_dict()), strict=True)
        factories = self._factories(spec, scenario_spec, treatment, target / "workers")
        runner = NativeParallelPPORunner(
            factories, eval_core, parallel_config=eval_authorities.parallel,
            vector_config=eval_authorities.vector,
            base_runner_config=eval_authorities.base,
            run_namespace=f"benchmark-eval-{spec.run_id}-{environment_transitions}",
            artifact_root=None,
        )
        for state in runner.states:
            state.episode_index = self.protocol.evaluation_episode_indices[0]
        try:
            report = runner.evaluate(self.protocol.evaluation_episodes_per_checkpoint)
        finally:
            runner.close()
        payload = report.to_dict()
        if payload["seed_set_hash"] != canonical_hash(
            sorted(item.episode.environment_seed for item in report.episodes)
        ):
            raise PPOTrainingBenchmarkError("evaluation report seed hash is invalid")
        actual_seed_grid = sorted(item.episode.environment_seed for item in report.episodes)
        expected_seed_grid = sorted(self._evaluation_seeds())
        if actual_seed_grid != expected_seed_grid:
            raise PPOTrainingBenchmarkError(
                f"evaluation seed set drift: actual={actual_seed_grid} expected={expected_seed_grid}"
            )
        row = {
            "schema_version": "native-ppo-benchmark-evaluation-point/1.0",
            "run_id": spec.run_id,
            "treatment": spec.treatment_id,
            "scenario": spec.scenario_id,
            "train_seed_index": spec.train_seed_index,
            "side": spec.trained_side,
            "environment_transitions": environment_transitions,
            "update_step": source_core.update_step,
            "checkpoint_id": (
                "INITIAL" if environment_transitions == 0
                else "LAST" if environment_transitions == self.protocol.transitions_per_run
                else f"TRANSITION_{environment_transitions}"
            ),
            "eval_episodes": len(report.episodes),
            "wins": payload["outcomes"]["WIN"],
            "losses": payload["outcomes"]["LOSS"],
            "draws": payload["outcomes"]["DRAW"],
            "truncated": payload["outcomes"]["TRUNCATED"],
            "win_rate": payload["win_rate"],
            "base_return_mean": payload["base_return"]["mean"],
            "base_return_std": payload["base_return"]["std"],
            "shaping_return_mean": payload["shaping_return"]["mean"],
            "shaping_return_std": payload["shaping_return"]["std"],
            "composite_return_mean": payload["composite_return"]["mean"],
            "composite_return_std": payload["composite_return"]["std"],
            "episode_length_mean": payload["episode_length"]["mean"],
            "episode_length_std": payload["episode_length"]["std"],
            "parameter_hash": report.parameter_hash,
            "eval_seed_set_hash": self.protocol.eval_seed_set_hash,
            "action_sequence_hash": canonical_hash([
                list(item.episode.action_ids) for item in report.episodes
            ]),
            "outcome_sequence": [item.episode.outcome for item in report.episodes],
            "raw_report": payload,
        }
        _require_finite_metrics(row)
        target.mkdir(parents=True, exist_ok=True)
        _atomic_json(target / "evaluation_report.json", payload)
        _atomic_json(target / "evaluation_point.json", row)
        return row

    def _factories(self, spec, scenario, treatment, storage_root):
        composite = treatment.reward_mode.value == "COMPOSITE_EIGHT_FACTOR"
        return tuple(
            BenchmarkNativeEnvFactory(
                scenario.path, str(storage_root / f"env-{index:03d}"),
                spec.trained_side, composite, scenario.max_ticks,
                scenario.delta_time_seconds,
            )
            for index in range(self.protocol.num_envs)
        )

    def _evaluation_seeds(self) -> tuple[int, ...]:
        seed = load_ppo_algorithm_config()  # authority load also validates PPO freeze
        del seed
        from wargame_native_engine.training.training_seed import (
            TrainingSeedStream, load_training_seed_config,
        )
        authority = load_training_seed_config()
        return tuple(
            authority.derive_seed(
                TrainingSeedStream.EVALUATION,
                env_index=env_index, episode_index=episode_index,
            )
            for env_index, episode_index in self.protocol.evaluation_episode_coordinates
        )

    def _retain_first_episode_package(self, artifact_root: Path) -> None:
        episodes = sorted(
            path for path in artifact_root.glob("env-*/episode-*") if path.is_dir()
        )
        if not episodes:
            raise PPOTrainingBenchmarkError("episode retention found no verified artifact")
        retained = episodes[0]
        for path in episodes[1:]:
            shutil.rmtree(path)
        _atomic_json(artifact_root / "retention_manifest.json", {
            "policy": "FIRST_ENGINE_REPLAY_VERIFIED_ONLY",
            "retained": str(retained.relative_to(artifact_root)),
            "removed_episode_packages": len(episodes) - 1,
        })

    def _verify_first_replay(self, scenario, episodes, artifact_root: Path) -> bool:
        first = min(
            episodes,
            key=lambda item: (item.env_index, item.episode.episode_index),
        )
        path = artifact_root / f"env-{first.env_index:03d}" / f"episode-{first.episode.episode_index:06d}" / "replay.zip"
        if not path.is_file():
            raise PPOTrainingBenchmarkError("published episode replay is missing")
        replay_source_archive(load_scenario(scenario.path), path, strict=True)
        return True

    def _save_checkpoint(
        self, target: Path, core: NativeMaskedPPO, treatment,
        reward_identity: PPORewardIdentity, spec, transitions: int,
    ) -> dict[str, object]:
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        try:
            state_path = staging / "state.pt"
            torch.save(core.checkpoint_payload(reward_identity), state_path)
            manifest = {
                "schema_version": "native-ppo-benchmark-checkpoint/1.0",
                "benchmark_hash": self.protocol.content_hash,
                "run_id": spec.run_id,
                "treatment": treatment.to_dict(),
                "environment_transitions": transitions,
                "update_step": core.update_step,
                "sample_step": core.sample_step,
                "parameter_hash": core.parameter_content_hash(),
                "state_hash": file_sha256(state_path),
                "ppo_config_hash": core.config.content_hash,
                "seed_config_hash": core.seed.content_hash,
                "checkpoint_selection": "LAST_CHECKPOINT_ONLY",
            }
            manifest["semantic_hash"] = canonical_hash(manifest)
            _atomic_json(staging / "manifest.json", manifest)
            os.replace(staging, target)
            return manifest
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _is_complete(self, target: Path, spec: PPOBenchmarkRunSpec) -> bool:
        try:
            completion = json.loads((target / "completion.json").read_text(encoding="utf-8"))
            claimed = completion.pop("semantic_hash")
            valid = (
                claimed == canonical_hash(completion)
                and completion["status"] == "COMPLETE"
                and completion["benchmark_hash"] == self.protocol.content_hash
                and completion["run_id"] == spec.run_id
                and file_sha256(target / "final_summary.json") == completion["summary_hash"]
                and (target / "checkpoints/final/state.pt").is_file()
            )
            if self.protocol.stage == "FULL":
                valid = valid and (
                    file_sha256(target / "train_metrics.jsonl")
                    == completion["train_metrics_hash"]
                    and file_sha256(target / "evaluation_metrics.jsonl")
                    == completion["evaluation_metrics_hash"]
                    and file_sha256(target / "run_manifest.json")
                    == completion["run_manifest_file_hash"]
                )
            return valid
        except (OSError, ValueError, KeyError, TypeError):
            return False


def aggregate_benchmark_results(
    protocol: PPOTrainingBenchmarkConfig,
    summaries: Sequence[Mapping[str, object]],
    failures: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    if protocol.stage == "FULL":
        for summary in summaries:
            _validate_full_run_summary(protocol, summary)
    ordered = sorted(
        summaries,
        key=lambda item: (
            str(item["scenario_id"]), str(item["trained_side"]),
            int(item["train_seed_index"]), str(item["treatment_id"]),
        ),
    )
    pairs = []
    grouped = {}
    for item in ordered:
        grouped.setdefault(
            (item["scenario_id"], item["train_seed_index"], item["trained_side"]), {}
        )[item["treatment_id"]] = item
    for key, arms in sorted(grouped.items()):
        if set(arms) != {"BASE_ONLY", "EIGHT_FACTOR_SHAPING"}:
            continue
        base, shaping = arms["BASE_ONLY"], arms["EIGHT_FACTOR_SHAPING"]
        base_initial = base["evaluation_points"][0]
        shaping_initial = shaping["evaluation_points"][0]
        if base["initial_parameter_hash"] != shaping["initial_parameter_hash"] or any(
            base_initial[name] != shaping_initial[name]
            for name in (
                "parameter_hash", "action_sequence_hash", "outcome_sequence",
                "wins", "losses", "draws", "truncated", "win_rate",
                "base_return_mean",
            )
        ):
            raise PPOTrainingBenchmarkError(
                f"paired initial equality failed for {key}"
            )
        if float(base["final_shaping_return"]) != 0.0 or bool(base["shaping_signal_nonzero"]):
            raise PPOTrainingBenchmarkError(f"Base arm contains shaping supervision: {key}")
        if not bool(shaping["shaping_signal_nonzero"]):
            raise PPOTrainingBenchmarkError(f"Composite shaping signal is constant zero: {key}")
        pairs.append({
            "scenario": key[0], "train_seed_index": key[1], "side": key[2],
            "base_final_win_rate": base["final_win_rate"],
            "shaping_final_win_rate": shaping["final_win_rate"],
            "delta_final_win_rate": float(shaping["final_win_rate"]) - float(base["final_win_rate"]),
            "base_auc": base["win_rate_auc"], "shaping_auc": shaping["win_rate_auc"],
            "delta_auc": float(shaping["win_rate_auc"]) - float(base["win_rate_auc"]),
            "base_wall_time": base["training_wall_time"],
            "shaping_wall_time": shaping["training_wall_time"],
            "base_t50": base.get("t50"), "shaping_t50": shaping.get("t50"),
            "delta_t50": _nullable_threshold_delta(base.get("t50"), shaping.get("t50")),
            "base_t75": base.get("t75"), "shaping_t75": shaping.get("t75"),
            "delta_t75": _nullable_threshold_delta(base.get("t75"), shaping.get("t75")),
            "base_episode_length": base.get("final_episode_length"),
            "shaping_episode_length": shaping.get("final_episode_length"),
            "delta_episode_length": float(shaping.get("final_episode_length", 0.0)) - float(base.get("final_episode_length", 0.0)),
        })
    deltas = [float(item["delta_final_win_rate"]) for item in pairs]
    auc_deltas = [float(item["delta_auc"]) for item in pairs]
    if failures or len(pairs) != len(protocol.scenarios) * len(protocol.train_seed_indices) * len(protocol.trained_sides):
        conclusion = "INCONCLUSIVE"
    elif deltas and all(item > 0 for item in deltas) and all(item >= 0 for item in auc_deltas):
        conclusion = "SHAPING_BETTER"
    elif deltas and all(item < 0 for item in deltas) and all(item <= 0 for item in auc_deltas):
        conclusion = "BASE_BETTER"
    elif deltas and all(item == 0 for item in deltas) and all(item == 0 for item in auc_deltas):
        conclusion = "NO_CLEAR_DIFFERENCE"
    else:
        conclusion = "MIXED"
    treatment_aggregates = []
    for treatment in ("BASE_ONLY", "EIGHT_FACTOR_SHAPING"):
        values = [item for item in ordered if item["treatment_id"] == treatment]
        treatment_aggregates.append({
            "treatment": treatment,
            "successful_runs": len(values),
            "final_win_rate": _descriptive(float(item["final_win_rate"]) for item in values),
            "win_rate_auc": _descriptive(float(item["win_rate_auc"]) for item in values),
            "transitions_per_second": _descriptive(float(item["transitions_per_second"]) for item in values),
            # Compatibility scalars retained for the PILOT result reader.
            "final_win_rate_mean": _mean(float(item["final_win_rate"]) for item in values),
            "win_rate_auc_mean": _mean(float(item["win_rate_auc"]) for item in values),
            "transitions_per_second_mean": _mean(float(item["transitions_per_second"]) for item in values),
        })
    scenario_summaries = []
    for scenario in protocol.scenarios:
        values = [item for item in ordered if item["scenario_id"] == scenario.scenario_id]
        scenario_pairs = [item for item in pairs if item["scenario"] == scenario.scenario_id]
        arms = {}
        for treatment in ("BASE_ONLY", "EIGHT_FACTOR_SHAPING"):
            selected = [item for item in values if item["treatment_id"] == treatment]
            arms[treatment] = {
                "final_win_rate": _descriptive(float(item["final_win_rate"]) for item in selected),
                "win_rate_auc": _descriptive(float(item["win_rate_auc"]) for item in selected),
                "episode_length": _descriptive(float(item.get("final_episode_length", 0.0)) for item in selected),
            }
        final_deltas = [float(item["delta_final_win_rate"]) for item in scenario_pairs]
        auc_deltas_for_scenario = [float(item["delta_auc"]) for item in scenario_pairs]
        scenario_summaries.append({
            "scenario": scenario.scenario_id,
            "arms": arms,
            "paired_final_delta": _paired_summary(
                final_deltas, protocol, f"{scenario.scenario_id}:final"
            ),
            "paired_auc_delta": _paired_summary(
                auc_deltas_for_scenario, protocol, f"{scenario.scenario_id}:auc"
            ),
            "ceiling_floor": _ceiling_floor(values),
        })
    if protocol.stage == "FULL" and not failures and len(pairs) == 10:
        conclusion = _full_research_candidate(scenario_summaries)
    macro = {
        treatment: {
            "final_win_rate_mean": _mean(
                item["arms"][treatment]["final_win_rate"]["mean"]
                for item in scenario_summaries
            ),
            "win_rate_auc_mean": _mean(
                item["arms"][treatment]["win_rate_auc"]["mean"]
                for item in scenario_summaries
            ),
        }
        for treatment in ("BASE_ONLY", "EIGHT_FACTOR_SHAPING")
    }
    result = {
        "schema_version": PPO_TRAINING_BENCHMARK_RESULT_SCHEMA,
        "benchmark_hash": protocol.content_hash,
        "stage": protocol.stage,
        "implementation_status": "PASS" if not failures and len(ordered) == len(protocol.run_matrix()) else "PARTIAL",
        "research_result": conclusion,
        "research_scope": (
            "FULL_PAIRED_SEED_DESCRIPTIVE_LOW_N" if protocol.stage == "FULL"
            else "PILOT_ONLY_DESCRIPTIVE"
        ),
        "successful_runs": len(ordered),
        "failed_runs": list(failures),
        "run_summaries": ordered,
        "treatment_aggregates": treatment_aggregates,
        "paired_comparisons": pairs,
        "scenario_summaries": scenario_summaries,
        "macro_summary": macro,
    }
    result["reward_alignment"] = _reward_alignment(ordered, pairs)
    return result


def _publish_aggregate(root: Path, result: Mapping[str, object]) -> None:
    _atomic_json(root / "benchmark_result.json", result)
    _atomic_json(root / "full_research_summary.json", {
        "schema_version": "native-ppo-full-research-summary/1.0",
        "benchmark_hash": result["benchmark_hash"],
        "stage": result["stage"],
        "implementation_status": result["implementation_status"],
        "research_result": result["research_result"],
        "research_scope": result["research_scope"],
        "scenario_summaries": result["scenario_summaries"],
        "macro_summary": result["macro_summary"],
        "reward_alignment": result["reward_alignment"],
        "failed_runs": result["failed_runs"],
    })
    summaries = list(result["run_summaries"])
    evaluation_rows = [
        {"benchmark_hash": result["benchmark_hash"], **point}
        for item in summaries for point in item["evaluation_points"]
    ]
    _write_csv(root / "learning_curve.csv", evaluation_rows, (
        "benchmark_hash", "run_id", "treatment", "scenario", "train_seed_index", "side",
        "environment_transitions", "update_step", "checkpoint_id", "eval_episodes",
        "wins", "losses", "draws", "truncated", "win_rate",
        "base_return_mean", "base_return_std", "shaping_return_mean",
        "shaping_return_std", "composite_return_mean", "composite_return_std",
        "episode_length_mean", "episode_length_std", "eval_seed_set_hash",
    ))
    _write_csv(root / "final_runs.csv", summaries, (
        "treatment_id", "scenario_id", "train_seed_index", "trained_side",
        "initial_parameter_hash", "final_parameter_hash", "final_win_rate",
        "win_rate_auc", "t50", "t75", "final_base_return", "final_shaping_return",
        "final_composite_return", "final_episode_length", "environment_transitions",
        "updates", "training_wall_time", "evaluation_wall_time", "total_wall_time",
        "transitions_per_second", "status",
    ))
    _write_csv(root / "paired_comparison.csv", result["paired_comparisons"], (
        "scenario", "train_seed_index", "side", "base_final_win_rate",
        "shaping_final_win_rate", "delta_final_win_rate", "base_auc",
        "shaping_auc", "delta_auc", "base_t50", "shaping_t50", "delta_t50",
        "base_t75", "shaping_t75", "delta_t75", "base_episode_length",
        "shaping_episode_length", "delta_episode_length", "base_wall_time",
        "shaping_wall_time",
    ))
    scenario_rows = []
    for item in result["scenario_summaries"]:
        for treatment, arm in item["arms"].items():
            scenario_rows.append({
                "scenario": item["scenario"], "treatment": treatment,
                "final_win_rate": json.dumps(arm["final_win_rate"], sort_keys=True),
                "win_rate_auc": json.dumps(arm["win_rate_auc"], sort_keys=True),
                "episode_length": json.dumps(arm["episode_length"], sort_keys=True),
                "paired_final_delta": json.dumps(item["paired_final_delta"], sort_keys=True),
                "paired_auc_delta": json.dumps(item["paired_auc_delta"], sort_keys=True),
                "ceiling_floor": item["ceiling_floor"],
            })
    _write_csv(root / "scenario_summary.csv", scenario_rows, (
        "scenario", "treatment", "final_win_rate", "win_rate_auc",
        "episode_length", "paired_final_delta", "paired_auc_delta", "ceiling_floor",
    ))
    _write_csv(root / "treatment_summary.csv", result["treatment_aggregates"], (
        "treatment", "successful_runs", "final_win_rate", "win_rate_auc",
        "transitions_per_second", "final_win_rate_mean", "win_rate_auc_mean",
        "transitions_per_second_mean",
    ))
    _write_csv(root / "operational_cost.csv", summaries, (
        "scenario_id", "train_seed_index", "treatment_id", "environment_transitions",
        "training_wall_time", "evaluation_wall_time", "total_wall_time",
        "transitions_per_second",
    ))


def _mean(values: Iterable[float]) -> float:
    items = tuple(values)
    return sum(items) / len(items) if items else 0.0


def rollout_collection_lengths(
    evaluation_grid: Sequence[int], rollout_length: int,
) -> tuple[int, ...]:
    """Return exact update-sized segments without crossing evaluation boundaries."""

    if type(rollout_length) is not int or rollout_length <= 0:
        raise PPOTrainingBenchmarkError("rollout length must be positive")
    grid = tuple(evaluation_grid)
    if len(grid) < 2 or grid[0] != 0 or any(
        type(item) is not int or item < 0 for item in grid
    ) or any(right <= left for left, right in zip(grid, grid[1:])):
        raise PPOTrainingBenchmarkError("evaluation grid must be increasing from zero")
    result = []
    for left, right in zip(grid, grid[1:]):
        remaining = right - left
        while remaining:
            length = min(remaining, rollout_length)
            result.append(length)
            remaining -= length
    return tuple(result)


def _descriptive(values: Iterable[float]) -> dict[str, object]:
    items = tuple(float(item) for item in values)
    if not items:
        return {"count": 0, "mean": 0.0, "std": 0.0, "median": 0.0,
                "min": 0.0, "max": 0.0, "seed_values": []}
    return {
        "count": len(items),
        "mean": statistics.fmean(items),
        "std": statistics.stdev(items) if len(items) > 1 else 0.0,
        "median": statistics.median(items),
        "min": min(items),
        "max": max(items),
        "seed_values": list(items),
    }


def _nullable_threshold_delta(base: object, shaping: object) -> int | None:
    if base is None or shaping is None:
        return None
    # Negative means shaping reached the threshold earlier.
    return int(shaping) - int(base)


def _paired_summary(
    values: Sequence[float], protocol: PPOTrainingBenchmarkConfig, label: str,
) -> dict[str, object]:
    descriptive = _descriptive(values)
    if not values or protocol.bootstrap_resamples <= 0:
        descriptive["bootstrap_95_ci"] = None
        return descriptive
    seed_material = f"{protocol.bootstrap_seed}:{label}".encode("utf-8")
    rng = random.Random(int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "big"))
    means = sorted(
        statistics.fmean(rng.choice(values) for _ in values)
        for _ in range(protocol.bootstrap_resamples)
    )
    lower_index = max(0, math.floor(0.025 * (len(means) - 1)))
    upper_index = min(len(means) - 1, math.ceil(0.975 * (len(means) - 1)))
    descriptive["bootstrap_95_ci"] = [means[lower_index], means[upper_index]]
    descriptive["bootstrap_resamples"] = protocol.bootstrap_resamples
    descriptive["bootstrap_seed"] = protocol.bootstrap_seed
    return descriptive


def _ceiling_floor(values: Sequence[Mapping[str, object]]) -> str:
    points = [
        float(point["win_rate"])
        for item in values
        for point in item.get("evaluation_points", ())
    ]
    if points and all(item >= 0.95 for item in points):
        return "CEILING_EFFECT"
    if points and all(item <= 0.05 for item in points):
        return "FLOOR_EFFECT"
    return "NONE"


def _full_research_candidate(
    scenario_summaries: Sequence[Mapping[str, object]],
) -> str:
    final = [float(item["paired_final_delta"]["mean"]) for item in scenario_summaries]
    auc = [float(item["paired_auc_delta"]["mean"]) for item in scenario_summaries]
    epsilon = 1e-12
    if all(item > epsilon for item in final) and all(item > epsilon for item in auc):
        return "SHAPING_CLEARLY_BETTER"
    if all(abs(item) <= epsilon for item in final) and all(item > epsilon for item in auc):
        return "SHAPING_SAMPLE_EFFICIENCY_ONLY"
    if any(item > epsilon for item in final) and any(item < -epsilon for item in final):
        return "SCENARIO_DEPENDENT"
    if all(item <= epsilon for item in final) and all(item <= epsilon for item in auc) and (
        any(item < -epsilon for item in final) or any(item < -epsilon for item in auc)
    ):
        return "SHAPING_DEGRADES"
    return "NO_CLEAR_DIFFERENCE"


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    x_mean, y_mean = statistics.fmean(xs), statistics.fmean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    x_scale = math.sqrt(sum((x - x_mean) ** 2 for x in xs))
    y_scale = math.sqrt(sum((y - y_mean) ** 2 for y in ys))
    return numerator / (x_scale * y_scale) if x_scale and y_scale else None


def _reward_alignment(
    summaries: Sequence[Mapping[str, object]],
    pairs: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    shaping = [item for item in summaries if item["treatment_id"] == "EIGHT_FACTOR_SHAPING"]
    shaping_returns = [float(item["final_shaping_return"]) for item in shaping]
    win_rates = [float(item["final_win_rate"]) for item in shaping]
    base_returns = [float(item["final_base_return"]) for item in shaping]
    win_correlation = _pearson(shaping_returns, win_rates)
    base_correlation = _pearson(shaping_returns, base_returns)
    return {
        "shaping_return_vs_win_rate_pearson": win_correlation,
        "shaping_return_vs_base_return_pearson": base_correlation,
        "paired_final_direction_counts": {
            "positive": sum(float(item["delta_final_win_rate"]) > 0 for item in pairs),
            "zero": sum(float(item["delta_final_win_rate"]) == 0 for item in pairs),
            "negative": sum(float(item["delta_final_win_rate"]) < 0 for item in pairs),
        },
        "reward_hacking_signal": (
            "PRESENT" if win_correlation is not None and win_correlation < 0.0
            else "NONE" if win_correlation is not None
            else "INCONCLUSIVE"
        ),
        "eight_factor_final_advantage": "NOT_AVAILABLE_IN_V1_EVALUATION_REPORT",
    }


def _validate_full_run_summary(
    protocol: PPOTrainingBenchmarkConfig, summary: Mapping[str, object],
) -> None:
    _require_finite_metrics(summary)
    if summary.get("status") != "COMPLETE":
        raise PPOTrainingBenchmarkError("FULL aggregate contains incomplete run")
    if int(summary.get("environment_transitions", -1)) != protocol.transitions_per_run:
        raise PPOTrainingBenchmarkError("FULL run training budget mismatch")
    for name in ("invalid_action_attempts", "stale_action_attempts", "replay_mismatches"):
        if int(summary.get(name, -1)) != 0:
            raise PPOTrainingBenchmarkError(f"FULL run integrity failure: {name}")
    grid = tuple(
        int(item["environment_transitions"])
        for item in summary.get("evaluation_points", ())
    )
    if grid != protocol.evaluation_cadence_transitions:
        raise PPOTrainingBenchmarkError("FULL run evaluation grid mismatch")
    if summary.get("evaluation_seed_set_hash") != protocol.eval_seed_set_hash:
        raise PPOTrainingBenchmarkError("FULL evaluation seed set mismatch")


def build_experiment_source_manifest() -> dict[str, object]:
    def git(*args: str) -> bytes:
        result = subprocess.run(
            ("git", *args), cwd=_ROOT, check=True, capture_output=True,
        )
        return result.stdout

    head = git("rev-parse", "HEAD").decode("ascii").strip()
    status = tuple(
        line for line in git("status", "--short").decode("utf-8").splitlines()
        if line
    )
    diff_hash = hashlib.sha256(git("diff", "--binary", "--no-ext-diff")).hexdigest()
    paths = set()
    for base, pattern in (
        (_ROOT / "src/wargame_native_engine", "*.py"),
        (_ROOT / "config", "*.json"),
    ):
        paths.update(path for path in base.rglob(pattern) if path.is_file())
    paths.update({
        _ROOT / "pyproject.toml",
        _ROOT / "scripts/run_ppo_training_benchmark.py",
        _ROOT / "examples/demos/direct_fire_basic/scenario.json",
        _ROOT / "examples/joint_fire_scenario.json",
    })
    files = [
        {
            "path": str(path.relative_to(_ROOT)),
            "sha256": file_sha256(path),
        }
        for path in sorted(paths)
    ]
    payload = {
        "schema_version": "native-experiment-source-manifest/1.0",
        "git_head": head,
        "dirty": bool(status),
        "git_status_short": list(status),
        "git_diff_binary_sha256": diff_hash,
        "relevant_files": files,
    }
    payload["semantic_hash"] = canonical_hash(payload)
    return payload


def _threshold_transition(
    evaluation_rows: Sequence[Mapping[str, object]], threshold: float,
) -> int | None:
    for item in evaluation_rows:
        if float(item["win_rate"]) >= threshold:
            return int(item["environment_transitions"])
    return None


def _require_finite_metrics(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for name, item in value.items():
            _require_finite_metrics(item, f"{path}.{name}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _require_finite_metrics(item, f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise PPOTrainingBenchmarkError(f"non-finite metric at {path}")


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _append_jsonl(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_csv(path: Path, rows: Sequence[Mapping[str, object]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(temporary, path)


__all__ = [
    "BenchmarkDryRunPlan", "BenchmarkNativeEnvFactory", "BenchmarkRunnerAuthorities",
    "PPOTrainingBenchmarkError", "PPOTrainingBenchmarkRunner",
    "aggregate_benchmark_results", "build_benchmark_runner_authorities",
    "build_experiment_source_manifest", "complete_benchmark_green_template",
    "require_only_treatment_difference", "rollout_collection_lengths",
]
