#!/usr/bin/env python3
"""Bounded Native PPO train/checkpoint/child-evaluation roundtrip.

This is orchestration only.  PPO updates, checkpoint integrity, episode
publication, strict replay, and Viewer validation stay in their production
modules.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import secrets
import shlex
import signal
import subprocess
import sys
import time
import traceback
from typing import Mapping, Sequence

from wargame_native_engine.agents import NoOpAgent
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import (
    source_frame_template_for_scenario,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import NativeScenario, load_scenario
from wargame_native_engine.io.serialization import canonical_hash, file_sha256
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)
from wargame_native_engine.training import (
    NativeTrainingEnv,
    ObjectiveTaskConfig,
    SpatialCandidateProviderConfig,
    load_objective_task_config,
    load_spatial_candidate_provider_config,
)
from wargame_native_engine.training.ppo_config import (
    PPOAlgorithmConfig,
    build_ppo_algorithm_config,
)
from wargame_native_engine.training.ppo_core import NativeMaskedPPO, torch
from wargame_native_engine.training.ppo_runner import NativePPORunner
from wargame_native_engine.training.ppo_runner_config import (
    PPORunnerConfig,
    build_ppo_runner_config,
)
from wargame_native_engine.training.diagnostics import TrainingTimingCollector
from wargame_native_engine.viewer import load_replay_view


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIO = ROOT / "examples/source_movement_scenario.json"
DEFAULT_MAP = ROOT / "examples/public_source_movement_map.json"
BASE_PPO_CONFIG = ROOT / "config/training/native_masked_ppo_v1.json"
BASE_RUNNER_CONFIG = ROOT / "config/training/native_ppo_runner_v1.json"
BASE_PPO_CONFIG_V2 = ROOT / "config/training/native_masked_ppo_v2.json"
BASE_RUNNER_CONFIG_V2 = ROOT / "config/training/native_ppo_runner_v2.json"
DEFAULT_SPATIAL_PROVIDER = (
    ROOT / "config/training/native_spatial_candidate_provider_v1.json"
)
SUMMARY_SCHEMA = "native-training-roundtrip/1.0"
EVALUATION_SCHEMA = "native-training-roundtrip-evaluation/1.0"
TOTAL_TIMEOUT_SECONDS = 300.0
LOG_LIMIT_BYTES = 64 * 1024
_OWNER_FILE = ".roundtrip-owner"


class RoundtripTimeout(TimeoutError):
    """The single CLI wall-clock budget was exhausted."""


class _OutputEvidence:
    """Track an exclusively owned output directory and its current stage."""

    def __init__(self, output: Path, command: str) -> None:
        self.output = output
        self.command = command
        self.stage = "setup"
        self.owned = False

    def claim(self, owner_token: str | None = None) -> None:
        if owner_token is None:
            if self.output.exists():
                raise FileExistsError(
                    f"refusing to overwrite {self.command} output: {self.output}"
                )
            self.output.mkdir(parents=True)
        else:
            marker = self.output / _OWNER_FILE
            if not self.output.is_dir() or not marker.is_file() or (
                marker.read_text(encoding="utf-8") != owner_token
            ):
                raise FileExistsError(
                    f"evaluation output is not owned by this invocation: {self.output}"
                )
        self.owned = True

    def record_failure(self, exc: BaseException) -> None:
        if not self.owned:
            return
        _write_json(self.output / "failure_summary.json", {
            "schema_version": SUMMARY_SCHEMA,
            "roundtrip_complete": False,
            "failed_stage": self.stage,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        })


@contextmanager
def _wall_time_limit(seconds: float):
    """Interrupt the whole CLI, including in-process training, on POSIX."""

    if not hasattr(signal, "setitimer"):
        yield
        return
    def expired(_signum, _frame) -> None:
        raise RoundtripTimeout(
            f"training roundtrip exceeded {seconds:.0f}s total wall-clock budget"
        )

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _configure_cpu() -> None:
    torch.set_num_threads(1)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        # Torch permits this setting only before inter-op work starts.  A CLI
        # process reaches here first; direct in-process callers may not.
        if torch.get_num_interop_threads() != 1:
            raise


def _small_configs(
    *, max_updates: int = 1, evaluation_episodes: int = 1,
    contract_version: str = "v1", spatial_candidates_enabled: bool = False,
    rollout_length_override: int | None = None,
    objective_task_enabled: bool = False,
    env_index: int = 0,
) -> tuple[dict[str, object], dict[str, object]]:
    if type(max_updates) is not int or max_updates <= 0:
        raise ValueError("max_updates must be a positive exact int")
    if type(evaluation_episodes) is not int or evaluation_episodes <= 0:
        raise ValueError("evaluation_episodes must be a positive exact int")
    if contract_version not in {"v1", "v2"}:
        raise ValueError("contract_version must be v1 or v2")
    if type(env_index) is not int or env_index < 0:
        raise ValueError("env_index must be non-negative")
    ppo_source = BASE_PPO_CONFIG_V2 if contract_version == "v2" else BASE_PPO_CONFIG
    runner_source = (
        BASE_RUNNER_CONFIG_V2 if contract_version == "v2" else BASE_RUNNER_CONFIG
    )
    rollout_length = rollout_length_override or (
        32
        if spatial_candidates_enabled
        else 8 if contract_version == "v2" else 2
    )
    ppo_value = json.loads(ppo_source.read_text(encoding="utf-8"))
    ppo_value["rollout_length"] = rollout_length
    ppo_value["minibatch_size"] = rollout_length
    ppo_value["update_epochs"] = 1
    ppo_value["network"]["hidden_sizes"] = (
        [32, 32] if objective_task_enabled else [8, 8]
    )
    ppo = build_ppo_algorithm_config(ppo_value)

    runner_value = json.loads(runner_source.read_text(encoding="utf-8"))
    runner_value["purpose"] = (
        "OBJECTIVE_CAPTURE_PILOT" if objective_task_enabled else "TEST_SMOKE"
    )
    runner_value["trained_side"] = "RED"
    runner_value["env_index"] = env_index
    runner_value["reward_mode"] = (
        "TASK_OBJECTIVE_CAPTURE" if objective_task_enabled else "BASE_ONLY"
    )
    runner_value["ppo_config_ref"]["hash"] = ppo.content_hash
    runner_value["run_limits"] = {
        "max_updates": max_updates,
        "max_environment_steps": None,
        "max_episodes": None,
    }
    runner_value["checkpoint_every_updates"] = None
    runner_value["evaluation_episodes"] = evaluation_episodes
    runner = build_ppo_runner_config(runner_value, ppo=ppo)
    return ppo.to_dict(), runner.to_dict()


def _load_configs(
    ppo_path: Path, runner_path: Path,
) -> tuple[PPOAlgorithmConfig, PPORunnerConfig]:
    ppo_value = json.loads(ppo_path.read_text(encoding="utf-8"))
    runner_value = json.loads(runner_path.read_text(encoding="utf-8"))
    ppo_value.pop("config_hash", None)
    runner_value.pop("config_hash", None)
    ppo = build_ppo_algorithm_config(ppo_value)
    return ppo, build_ppo_runner_config(runner_value, ppo=ppo)


def _environment(
    scenario: NativeScenario,
    storage_root: Path,
    ppo: PPOAlgorithmConfig,
    spatial_candidate_provider_config: SpatialCandidateProviderConfig | None = None,
    objective_task_config: ObjectiveTaskConfig | None = None,
    timing_enabled: bool = False,
    planning_hash_cache_enabled: bool = True,
) -> NativeTrainingEnv:
    def factory() -> SourceCompatibleEnvironment:
        return SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(scenario)),
            template=source_frame_template_for_scenario(scenario),
        )

    v2 = ppo.observation_preprocessor.identity_id == (
        "native-decision-weapon-aware-fixed-scaling"
    )
    return NativeTrainingEnv(
        factory,
        controlled_side="red",
        opponent=NoOpAgent("native-no-op-fixed-opponent-v1"),
        storage_root=storage_root,
        max_actions=256,
        max_steps=(
            objective_task_config.training_horizon_decisions
            if objective_task_config is not None else
            32
            if spatial_candidate_provider_config is not None
            else 8 if v2 else 2
        ),
        delta_time_seconds=25 if v2 else 20,
        training_interface_version="decision-v2" if v2 else "decision-v1",
        enable_eight_factor_shaping=False,
        spatial_candidate_provider_config=spatial_candidate_provider_config,
        objective_task_config=objective_task_config,
        timing_collector=TrainingTimingCollector(enabled=timing_enabled),
        enable_planning_world_hash_cache=planning_hash_cache_enabled,
    )


def _runner(
    *,
    scenario: NativeScenario,
    storage_root: Path,
    artifact_root: Path,
    ppo: PPOAlgorithmConfig,
    config: PPORunnerConfig,
    spatial_candidate_provider_config: SpatialCandidateProviderConfig | None = None,
    objective_task_config: ObjectiveTaskConfig | None = None,
    timing_enabled: bool = False,
    planning_hash_cache_enabled: bool = True,
) -> NativePPORunner:
    env = _environment(
        scenario, storage_root, ppo, spatial_candidate_provider_config,
        objective_task_config,
        timing_enabled,
        planning_hash_cache_enabled,
    )
    try:
        return NativePPORunner(
            env,
            NativeMaskedPPO(ppo),
            config=config,
            run_namespace="task030-training-roundtrip",
            artifact_root=artifact_root,
        )
    except BaseException:
        env.close()
        raise


def _metric_record(value: object) -> dict[str, object]:
    names = (
        "policy_loss", "value_loss", "entropy", "total_loss", "approx_kl",
        "clip_fraction", "grad_norm", "clipped_grad_norm", "optimizer_steps",
        "update_step",
    )
    result = {name: getattr(value, name) for name in names}
    numeric = [item for item in result.values() if isinstance(item, (int, float))]
    if not all(math.isfinite(float(item)) for item in numeric):
        raise RuntimeError("PPO update produced a non-finite metric")
    return result


def evaluate_checkpoint(
    *,
    checkpoint: Path,
    output: Path,
    ppo_config: Path,
    runner_config: Path,
    scenario_path: Path,
    map_config: Path,
    episodes: int = 1,
    owner_token: str | None = None,
    spatial_candidate_provider_path: Path | None = None,
    objective_task_path: Path | None = None,
    objective_reward_path: Path | None = None,
    evaluation_episode_index_start: int | None = None,
    evaluation_env_index: int | None = None,
    timing_enabled: bool = False,
    planning_hash_cache_enabled: bool = True,
    evaluation_policy_mode: str = "ARGMAX",
    evaluation_policy_seed: int | None = None,
    device: str = "cpu",
) -> Mapping[str, object]:
    """Load and evaluate one checkpoint in the current (normally child) process."""

    evidence = _OutputEvidence(output, "evaluation")
    evidence.claim(owner_token)
    runner: NativePPORunner | None = None
    try:
        try:
            evidence.stage = "setup"
            _configure_cpu()
            from wargame_native_engine.training.bc_device import configure_device, device_evidence
            target_device = configure_device(device)
            if target_device.type == "cuda" and evaluation_policy_mode.upper() != "ARGMAX":
                raise ValueError("CUDA evaluation supports argmax only")
            scenario = load_scenario(scenario_path)
            ppo, config = _load_configs(ppo_config, runner_config)
            provider_config = (
                None
                if spatial_candidate_provider_path is None
                else load_spatial_candidate_provider_config(
                    spatial_candidate_provider_path
                )
            )
            task_config = (
                None
                if objective_task_path is None
                else load_objective_task_config(
                    objective_task_path,
                    reward_policy_path=objective_reward_path,
                )
            )
            runner = _runner(
                scenario=scenario,
                storage_root=output / "facts",
                artifact_root=output / "artifacts",
                ppo=ppo,
                config=config,
                spatial_candidate_provider_config=provider_config,
                objective_task_config=task_config,
                timing_enabled=timing_enabled,
                planning_hash_cache_enabled=planning_hash_cache_enabled,
            )
            initial_hash = runner.core.parameter_content_hash()
            initial_update = runner.core.update_step
            initial_sample = runner.core.sample_step

            evidence.stage = "load"
            manifest = json.loads(
                (checkpoint / "manifest.json").read_text(encoding="utf-8")
            )
            from wargame_native_engine.training.behavior_cloning import validate_bc_checkpoint
            validate_bc_checkpoint(checkpoint, manifest)
            runner.load_checkpoint(checkpoint)
            validate_bc_checkpoint(checkpoint, manifest, runner.core.model)
            # Restore the existing CPU checkpoint/optimizer contract first. Only the
            # inference model moves; this path performs no optimizer updates.
            runner.core.model.to(target_device)
            loaded_hash = runner.core.parameter_content_hash()
            loaded_update = runner.core.update_step
            loaded_sample = runner.core.sample_step
            loaded_sampling_rng = (
                runner.core.policy_sampling_generator.get_state().clone()
            )

            evidence.stage = "evaluate"
            runner.env.timing.clear()
            evaluation_started = time.perf_counter()
            report = runner.evaluate(
                episodes=episodes,
                checkpoint_identity=str(manifest["semantic_hash"]),
                episode_index_start=evaluation_episode_index_start,
                evaluation_env_index=evaluation_env_index,
                policy_mode=evaluation_policy_mode,
                policy_seed=evaluation_policy_seed,
            )
            evaluation_wall_seconds = time.perf_counter() - evaluation_started
            final_hash = runner.core.parameter_content_hash()
            final_update = runner.core.update_step
            final_sample = runner.core.sample_step
            evidence.stage = "replay"
            episode_roots: list[Path] = []
            replay_records: list[dict[str, object]] = []
            viewer_records: list[dict[str, object]] = []
            for episode in report.episodes:
                episode_root = (
                    output / "artifacts" / "episodes"
                    / f"{episode.episode_index:06d}"
                )
                replay_path = episode_root / "replay.zip"
                verification = replay_source_archive(
                    scenario, replay_path, strict=True
                )
                evidence.stage = "viewer"
                view = load_replay_view(
                    episode_root,
                    map_config=map_config,
                    scenario_config=scenario_path,
                )
                map_record = view.map_record()
                replay_record = view.replay_record()
                episode_roots.append(episode_root)
                replay_records.append({
                    "episode_index": episode.episode_index,
                    "matches": verification.matches,
                    "ticks": verification.ticks,
                    "expected_final_hash": verification.expected_world_hash,
                    "actual_final_hash": verification.actual_world_hash,
                })
                viewer_records.append({
                    "episode_index": episode.episode_index,
                    "loaded": True,
                    "frame_count": view.frame_count,
                    "first_frame_index": replay_record["first_frame_index"],
                    "last_frame_index": replay_record["last_frame_index"],
                    "analysis_frame_count": len(
                        view.analysis.timeline.frame_analyses
                    ),
                    "map_id": map_record["map_id"],
                    "scenario_id": map_record["scenario"]["scenario_id"],
                    "strict_replay_evidence_status": (
                        view.strict_replay_evidence_status
                    ),
                })
            first_episode = report.episodes[0]
            first_root = episode_roots[0]
            first_replay = replay_records[0]
            first_viewer = viewer_records[0]
            evaluation_seeds = [
                episode.environment_seed for episode in report.episodes
            ]
            result: dict[str, object] = {
                "schema_version": EVALUATION_SCHEMA,
                "checkpoint": str(checkpoint.resolve()),
                "checkpoint_identity": report.checkpoint_identity,
                "checkpoint_parameter_hash": manifest["parameter_hash"],
                "fresh_instance_parameter_hash_before_load": initial_hash,
                "restored_parameter_hash": loaded_hash,
                "parameter_hash_after_evaluation": final_hash,
                "update_step_before_load": initial_update,
                "sample_step_before_load": initial_sample,
                "update_step_after_load": loaded_update,
                "sample_step_after_load": loaded_sample,
                "update_step_after_evaluation": final_update,
                "sample_step_after_evaluation": final_sample,
                "optimizer_update_step": report.optimizer_update_step,
                "evaluation_wall_seconds": evaluation_wall_seconds,
                "evaluation_policy": {
                    "mode": evaluation_policy_mode.upper(),
                    "seed": evaluation_policy_seed,
                    "independent_from_training_rng": True,
                },
                "evaluation_semantic_hash": report.semantic_hash,
                "seed_set_hash": report.seed_set_hash,
                "episode": first_episode.to_dict(),
                "episodes": [episode.to_dict() for episode in report.episodes],
                "evaluation_seeds": evaluation_seeds,
                "action_sequence": list(report.action_sequences[0]),
                "action_sequences": [
                    list(sequence) for sequence in report.action_sequences
                ],
                "reward_record_hashes": list(first_episode.reward_record_hashes),
                "strict_replay": {
                    **first_replay,
                    "matches": all(
                        bool(record["matches"]) for record in replay_records
                    ),
                    "episodes": replay_records,
                },
                "viewer_offline": {
                    **first_viewer,
                    "loaded": all(
                        bool(record["loaded"]) for record in viewer_records
                    ),
                    "episodes": viewer_records,
                },
                "artifacts": {
                    "root": str(output.resolve()),
                    "episode_root": str(first_root.resolve()),
                    "episode_roots": [
                        str(root.resolve()) for root in episode_roots
                    ],
                    "replay": str((first_root / "replay.zip").resolve()),
                    "replays": [
                        str((root / "replay.zip").resolve())
                        for root in episode_roots
                    ],
                    "analysis": str(
                        (first_root / "frame_analysis.native.jsonl").resolve()
                    ),
                    "evaluation_report": str(
                        (output / "artifacts/evaluation_report.json").resolve()
                    ),
                },
                "spatial_candidate_metrics": dict(
                    runner.env.spatial_candidate_metrics
                ),
                "aggregate_timing": dict(runner.env.timing_metrics),
            }
            checks = {
                "disk_checkpoint_identity": report.checkpoint_identity
                == manifest["semantic_hash"],
                "restored_parameter_hash": loaded_hash
                == manifest["parameter_hash"] == report.parameter_hash,
                "parameters_unchanged_by_evaluation": final_hash == loaded_hash,
                "update_counter_unchanged_by_evaluation": (
                    final_update == loaded_update == report.optimizer_update_step
                ),
                "sample_counter_unchanged_by_evaluation": (
                    final_sample == loaded_sample == manifest["sample_step"]
                ),
                "training_sampling_rng_unchanged_by_evaluation": torch.equal(
                    loaded_sampling_rng,
                    runner.core.policy_sampling_generator.get_state(),
                ),
                "evaluation_seed_count": len(set(evaluation_seeds)) == episodes,
                "strict_replay": all(
                    bool(record["matches"]) for record in replay_records
                ),
                "viewer_analysis_aligned": all(
                    record["frame_count"] == record["analysis_frame_count"]
                    for record in viewer_records
                ),
                "viewer_explicit_map": all(
                    record["map_id"] == scenario.map.map_id
                    for record in viewer_records
                ),
                "viewer_explicit_scenario": all(
                    record["scenario_id"] == scenario.scenario_id
                    for record in viewer_records
                ),
            }
            result["execution"] = {
                **device_evidence(target_device, runner.core.model),
                **runner.core.last_action_devices,
            }
            result["checks"] = checks
            result["evaluation_complete"] = all(checks.values())
            result["semantic_hash"] = canonical_hash(result)
            if not result["evaluation_complete"]:
                raise RuntimeError("evaluation roundtrip checks failed")
            evidence.stage = "summary"
            _write_json(output / "evaluation_summary.json", result)
            return result
        finally:
            if runner is not None:
                runner.env.close()
    except BaseException as exc:
        evidence.record_failure(exc)
        raise


def _run_evaluation_child(
    *,
    checkpoint: Path,
    output: Path,
    ppo_config: Path,
    runner_config: Path,
    scenario: Path,
    map_config: Path,
    deadline: float,
    episodes: int = 1,
    spatial_candidate_provider_path: Path | None = None,
    objective_task_path: Path | None = None,
    objective_reward_path: Path | None = None,
    evaluation_episode_index_start: int | None = None,
    evaluation_env_index: int | None = None,
    device: str = "cpu",
) -> Mapping[str, object]:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite child output: {output}")
    output.mkdir(parents=True)
    owner_token = secrets.token_hex(24)
    (output / _OWNER_FILE).write_text(owner_token, encoding="utf-8")
    command = [
        sys.executable, "-m", "scripts.run_training_roundtrip", "evaluate",
        "--checkpoint", str(checkpoint.resolve()),
        "--output", str(output.resolve()),
        "--ppo-config", str(ppo_config.resolve()),
        "--runner-config", str(runner_config.resolve()),
        "--scenario", str(scenario.resolve()),
        "--map-config", str(map_config.resolve()),
        "--episodes", str(episodes),
        "--owner-token", owner_token,
    ]
    command.extend(["--device", device])
    if spatial_candidate_provider_path is not None:
        command.extend([
            "--spatial-candidate-provider-config",
            str(spatial_candidate_provider_path.resolve()),
        ])
    if objective_task_path is not None:
        assert objective_reward_path is not None
        command.extend([
            "--objective-task-config", str(objective_task_path.resolve()),
            "--objective-reward-config", str(objective_reward_path.resolve()),
        ])
    if evaluation_episode_index_start is not None:
        command.extend([
            "--evaluation-episode-index-start",
            str(evaluation_episode_index_start),
        ])
    if evaluation_env_index is not None:
        command.extend([
            "--evaluation-env-index", str(evaluation_env_index),
        ])
    environment = dict(os.environ)
    source_root = str((ROOT / "src").resolve())
    environment["PYTHONPATH"] = os.pathsep.join(
        [source_root, environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    environment.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    })
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise RoundtripTimeout("no total wall-clock budget remains for evaluation")
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=remaining,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _write_bounded_log(output / "subprocess.stdout.log", exc.stdout)
        _write_bounded_log(output / "subprocess.stderr.log", exc.stderr)
        failure = RoundtripTimeout(
            f"evaluation subprocess exhausted remaining {remaining:.3f}s budget"
        )
        _write_json(output / "failure_summary.json", {
            "schema_version": SUMMARY_SCHEMA,
            "roundtrip_complete": False,
            "failed_stage": "evaluate_subprocess_timeout",
            "error_type": type(failure).__name__,
            "error": str(failure),
            "traceback": traceback.format_exc(),
        })
        raise failure from exc
    _write_bounded_log(output / "subprocess.stdout.log", completed.stdout)
    _write_bounded_log(output / "subprocess.stderr.log", completed.stderr)
    if completed.returncode != 0:
        failure_path = output / "failure_summary.json"
        if not failure_path.exists():
            _write_json(failure_path, {
                "schema_version": SUMMARY_SCHEMA,
                "roundtrip_complete": False,
                "failed_stage": "evaluate_subprocess",
                "error_type": "ChildProcessError",
                "error": f"evaluation subprocess exit code {completed.returncode}",
                "stderr": str(output / "subprocess.stderr.log"),
            })
        raise RuntimeError(
            f"evaluation subprocess failed with exit code {completed.returncode}; "
            f"see {output / 'subprocess.stderr.log'}"
        )
    return json.loads(
        (output / "evaluation_summary.json").read_text(encoding="utf-8")
    )


def _write_bounded_log(path: Path, value: str | bytes | None) -> None:
    if value is None:
        text = ""
    elif isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value
    encoded = text.encode("utf-8")
    if len(encoded) > LOG_LIMIT_BYTES:
        text = encoded[-LOG_LIMIT_BYTES:].decode("utf-8", errors="replace")
        text = "[earlier output truncated]\n" + text
    path.write_text(text, encoding="utf-8")


def run_roundtrip(
    output: Path,
    *,
    scenario_path: Path = DEFAULT_SCENARIO,
    map_config: Path = DEFAULT_MAP,
    updates: int = 1,
    evaluation_episodes: int = 1,
    compare_initial: bool = False,
    contract_version: str = "v1",
    spatial_candidate_provider_path: Path | None = None,
    objective_task_path: Path | None = None,
    objective_reward_path: Path | None = None,
    rollout_length: int | None = None,
    env_index: int = 0,
    evaluation_env_index: int | None = None,
    evaluation_episode_index_start: int | None = None,
    deadline: float | None = None,
) -> Mapping[str, object]:
    """Run bounded updates and fresh-process checkpoint evaluations."""

    evidence = _OutputEvidence(output, "roundtrip")
    evidence.claim()
    active_deadline = (
        time.monotonic() + TOTAL_TIMEOUT_SECONDS
        if deadline is None else deadline
    )
    runner: NativePPORunner | None = None
    started = time.perf_counter()
    try:
        evidence.stage = "setup"
        _configure_cpu()
        ppo_value, runner_value = _small_configs(
            max_updates=updates,
            evaluation_episodes=evaluation_episodes,
            contract_version=contract_version,
            spatial_candidates_enabled=(
                spatial_candidate_provider_path is not None
            ),
            rollout_length_override=rollout_length,
            objective_task_enabled=objective_task_path is not None,
            env_index=env_index,
        )
        ppo_path = output / "config/small_ppo.json"
        runner_path = output / "config/small_runner.json"
        _write_json(ppo_path, ppo_value)
        _write_json(runner_path, runner_value)
        ppo, config = _load_configs(ppo_path, runner_path)
        scenario = load_scenario(scenario_path)
        provider_config = (
            None
            if spatial_candidate_provider_path is None
            else load_spatial_candidate_provider_config(
                spatial_candidate_provider_path
            )
        )
        task_config = (
            None
            if objective_task_path is None
            else load_objective_task_config(
                objective_task_path,
                reward_policy_path=objective_reward_path,
            )
        )

        initial_checkpoint: Path | None = None
        initial_checkpoint_manifest: Mapping[str, object] | None = None
        initial_preparation_seconds = 0.0
        if compare_initial:
            evidence.stage = "initial_checkpoint"
            preparation_started = time.perf_counter()
            initial_runner = _runner(
                scenario=scenario,
                storage_root=output / "initial-preparation/facts",
                artifact_root=output / "initial-preparation/artifacts",
                ppo=ppo,
                config=config,
                spatial_candidate_provider_config=provider_config,
                objective_task_config=task_config,
            )
            try:
                initial_parameter_hash = (
                    initial_runner.core.parameter_content_hash()
                )
                preparation_report = initial_runner.evaluate(episodes=updates)
                if (
                    preparation_report.parameter_hash
                    != initial_parameter_hash
                    or initial_runner.core.update_step != 0
                    or initial_runner.core.sample_step != 0
                ):
                    raise RuntimeError(
                        "initial checkpoint preparation mutated the learner"
                    )
                initial_checkpoint = initial_runner.save_checkpoint(
                    output / "initial-checkpoint"
                )
            finally:
                initial_runner.env.close()
            initial_preparation_seconds = (
                time.perf_counter() - preparation_started
            )
            initial_checkpoint_manifest = json.loads(
                (initial_checkpoint / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )

        runner = _runner(
            scenario=scenario,
            storage_root=output / "train/facts",
            artifact_root=output / "train/artifacts",
            ppo=ppo,
            config=config,
            spatial_candidate_provider_config=provider_config,
            objective_task_config=task_config,
        )
        before_hash = runner.core.parameter_content_hash()
        before_update = runner.core.update_step
        before_sample = runner.core.sample_step
        seed_identity = runner.core.seed.identity.to_dict()
        if (
            compare_initial and task_config is None
            and initial_checkpoint_manifest is not None
            and
            initial_checkpoint_manifest["parameter_hash"] != before_hash
        ):
            raise RuntimeError(
                "independently initialized training model differs from "
                "the saved initial model"
            )

        evidence.stage = "train"
        update_records: list[dict[str, object]] = []
        training_episodes: list[object] = []
        training_decisions = 0
        train_started = time.perf_counter()
        try:
            for update_index in range(1, updates + 1):
                update_started = time.perf_counter()
                rollout, metrics = runner.train_update()
                update_wall_seconds = time.perf_counter() - update_started
                metric_record = _metric_record(metrics)
                training_decisions += len(rollout.batch.provenance)
                training_episodes.extend(rollout.episodes)
                update_records.append({
                    "update_index": update_index,
                    "decision_count": len(rollout.batch.provenance),
                    "episode_count": len(rollout.episodes),
                    "rollout_seconds": rollout.elapsed_seconds,
                    "model_update_and_metrics_seconds": max(
                        0.0, update_wall_seconds - rollout.elapsed_seconds
                    ),
                    "wall_seconds": update_wall_seconds,
                    "metrics": metric_record,
                })
            after_hash = runner.core.parameter_content_hash()
            after_sample = runner.core.sample_step
            training_seconds = time.perf_counter() - train_started

            evidence.stage = "save"
            save_started = time.perf_counter()
            checkpoint = runner.save_checkpoint(output / "checkpoint")
            checkpoint_save_seconds = time.perf_counter() - save_started
            train_episode = training_episodes[-1]
            train_episode_root = (
                output / "train/artifacts/episodes"
                / f"{train_episode.episode_index:06d}"
            )

            evidence.stage = "training_replay"
            train_verification = replay_source_archive(
                scenario, train_episode_root / "replay.zip", strict=True
            )
            training_frames = sum(
                _replay_frame_count(
                    output / "train/artifacts/episodes"
                    / f"{episode.episode_index:06d}",
                    map_config=map_config,
                    scenario_path=scenario_path,
                )
                for episode in training_episodes
            )
            training_spatial_metrics = dict(
                runner.env.spatial_candidate_metrics
            )
        finally:
            runner.env.close()
            runner = None

        checkpoint_manifest = json.loads(
            (checkpoint / "manifest.json").read_text(encoding="utf-8")
        )
        if (
            compare_initial and task_config is None
            and initial_checkpoint_manifest is not None and (
            initial_checkpoint_manifest["episode_index"]
            != checkpoint_manifest["episode_index"]
        )):
            raise RuntimeError(
                "initial and trained checkpoints do not share an evaluation "
                "seed coordinate"
            )

        evaluation_specs = (
            (
                ("initial", initial_checkpoint),
                ("trained", checkpoint),
                ("trained-repeat", checkpoint),
            )
            if compare_initial
            else (("trained", checkpoint), ("trained-repeat", checkpoint))
        )
        evaluations: dict[str, Mapping[str, object]] = {}
        evaluation_seconds: dict[str, float] = {}
        for name, evaluation_checkpoint in evaluation_specs:
            assert evaluation_checkpoint is not None
            evidence.stage = f"evaluation_{name}"
            evaluation_started = time.perf_counter()
            evaluations[name] = _run_evaluation_child(
                checkpoint=evaluation_checkpoint,
                output=output / f"evaluation-{name}",
                ppo_config=ppo_path,
                runner_config=runner_path,
                scenario=scenario_path,
                map_config=map_config,
                episodes=evaluation_episodes,
                deadline=active_deadline,
                spatial_candidate_provider_path=(
                    spatial_candidate_provider_path
                ),
                objective_task_path=objective_task_path,
                objective_reward_path=objective_reward_path,
                evaluation_episode_index_start=(
                    evaluation_episode_index_start
                    if task_config is not None else None
                ),
                evaluation_env_index=(
                    evaluation_env_index if task_config is not None else None
                ),
            )
            evaluation_seconds[name] = (
                time.perf_counter() - evaluation_started
            )
        eval_one = evaluations["trained"]
        eval_two = evaluations["trained-repeat"]
        evidence.stage = "compare"
        comparison_fields = (
            "action_sequences", "episodes",
            "evaluation_semantic_hash", "seed_set_hash",
        )
        deterministic = all(
            eval_one[name] == eval_two[name] for name in comparison_fields
        )
        paired_evaluation_seeds = (
            not compare_initial
            or evaluations["initial"]["evaluation_seeds"]
            == eval_one["evaluation_seeds"]
        )
        viewer_root = Path(eval_one["artifacts"]["episode_root"])
        viewer_command = shlex.join([
            "env", "PYTHONDONTWRITEBYTECODE=1", "PYTHONPATH=src",
            sys.executable, "-m", "scripts.run_replay_viewer",
            "--replay-dir", str(viewer_root),
            "--map-config", str(map_config.resolve()),
            "--scenario-config", str(scenario_path.resolve()),
            "--port", "8000",
        ])
        checks = {
            "real_parameter_update": before_hash != after_hash,
            "update_counter_advanced": (
                checkpoint_manifest["update_step"] == before_update + updates
            ),
            "sample_counter_advanced": after_sample > before_sample,
            "optimizer_stepped": all(
                int(record["metrics"]["optimizer_steps"]) > 0
                for record in update_records
            ),
            "finite_update_metrics": all(
                math.isfinite(float(value))
                for record in update_records
                for value in record["metrics"].values()
            ),
            "checkpoint_parameter_matches_training": checkpoint_manifest[
                "parameter_hash"
            ] == after_hash,
            "checkpoint_state_hash_matches": checkpoint_manifest["state_hash"]
            == file_sha256(checkpoint / "state.pt"),
            "train_reward_rederived": train_episode.reward_rederivation_verified,
            "train_strict_replay": train_verification.matches,
            "all_child_evaluations": all(
                bool(value["evaluation_complete"])
                for value in evaluations.values()
            ),
            "paired_evaluation_seeds": paired_evaluation_seeds,
            "evaluations_deterministic": deterministic,
            "evaluation_not_training_replay": Path(
                eval_one["artifacts"]["replay"]
            ).resolve() != (train_episode_root / "replay.zip").resolve(),
        }
        if (
            compare_initial and task_config is None
            and initial_checkpoint_manifest is not None
        ):
            checks.update({
                "initial_checkpoint_has_no_updates": (
                    initial_checkpoint_manifest["update_step"] == 0
                    and initial_checkpoint_manifest["sample_step"] == 0
                ),
                "initial_checkpoint_matches_training_initialization": (
                    initial_checkpoint_manifest["parameter_hash"] == before_hash
                ),
                "checkpoint_evaluation_coordinates_match": (
                    initial_checkpoint_manifest["episode_index"]
                    == checkpoint_manifest["episode_index"]
                ),
            })
        result: dict[str, object] = {
        "schema_version": SUMMARY_SCHEMA,
        "roundtrip_complete": all(checks.values()),
        "checks": checks,
        "repository_head": _repository_head(),
            "source_identity": {
                "roundtrip_script_sha256": file_sha256(Path(__file__)),
            "ppo_runner_sha256": file_sha256(
                ROOT / "src/wargame_native_engine/training/ppo_runner.py"
            ),
                "engine_sha256": file_sha256(
                    ROOT / "src/wargame_native_engine/core/engine.py"
                ),
                "recorder_sha256": file_sha256(
                    ROOT / "src/wargame_native_engine/replay/source_frame_recorder.py"
                ),
                "source_serialization_sha256": file_sha256(
                    ROOT / "src/wargame_native_engine/compat/source_frame/serialization.py"
                ),
            },
        "configuration": {
            "scenario": str(scenario_path.resolve()),
            "scenario_id": scenario.scenario_id,
            "scenario_hash": scenario.scenario_hash,
            "map_id": scenario.map.map_id,
            "ppo_config": str(ppo_path.resolve()),
            "ppo_config_hash": ppo.content_hash,
            "runner_config": str(runner_path.resolve()),
            "runner_config_hash": config.content_hash,
            "training_interface": (
                "decision-v2" if contract_version == "v2" else "decision-v1"
            ),
            "spatial_candidate_provider": (
                None
                if provider_config is None
                else provider_config.identity_record()
            ),
            "reward_mode": config.reward_mode.value,
            "objective_task": (
                None if task_config is None else task_config.authority_record()
            ),
            "opponent_id": "native-no-op-fixed-opponent-v1",
            "device": "CPU",
            "torch_threads": torch.get_num_threads(),
            "rollout_length": ppo.rollout_length,
            "minibatch_size": ppo.minibatch_size,
            "update_epochs": ppo.update_epochs,
                "hidden_sizes": list(ppo.hidden_sizes),
                "requested_optimizer_updates": updates,
                "evaluation_episodes_per_checkpoint": evaluation_episodes,
                "compare_initial_checkpoint": compare_initial,
                "max_steps_per_episode": (
                    task_config.training_horizon_decisions
                    if task_config is not None else
                    32
                    if provider_config is not None
                    else 8 if contract_version == "v2" else 2
                ),
            "delta_time_seconds": 25 if contract_version == "v2" else 20,
            "seed_identity": seed_identity,
        },
            "training": {
                "parameter_hash_before": before_hash,
                "parameter_hash_after": after_hash,
            "update_step_before": before_update,
            "sample_step_before": before_sample,
            "sample_step_after": after_sample,
                "metrics": update_records[-1]["metrics"],
                "updates": update_records,
                "optimizer_update_count": len(update_records),
                "episode": training_episodes[0].to_dict(),
                "episodes": [
                    episode.to_dict() for episode in training_episodes
                ],
                "decision_count": training_decisions,
                "engine_frame_count": training_frames,
                "strict_replay_matches": train_verification.matches,
                "spatial_candidate_metrics": training_spatial_metrics,
            },
        "checkpoint": {
            "root": str(checkpoint.resolve()),
            "manifest": str((checkpoint / "manifest.json").resolve()),
            "state": str((checkpoint / "state.pt").resolve()),
            "identity": checkpoint_manifest["semantic_hash"],
            "parameter_hash": checkpoint_manifest["parameter_hash"],
            "update_step": checkpoint_manifest["update_step"],
            "sample_step": checkpoint_manifest["sample_step"],
            "optimizer_state_hash": checkpoint_manifest["optimizer_state_hash"],
                "rng_state_hash": checkpoint_manifest["rng_state_hash"],
            },
            "initial_checkpoint": (
                None
                if initial_checkpoint is None
                else {
                    "root": str(initial_checkpoint.resolve()),
                    "identity": initial_checkpoint_manifest["semantic_hash"],
                    "parameter_hash": initial_checkpoint_manifest["parameter_hash"],
                    "update_step": initial_checkpoint_manifest["update_step"],
                    "sample_step": initial_checkpoint_manifest["sample_step"],
                }
            ),
            "evaluations": [evaluations[name] for name, _ in evaluation_specs],
            "evaluation_comparison": {
                "labels": [name for name, _ in evaluation_specs],
                "paired_seed_sets": paired_evaluation_seeds,
                "trained_repeat_deterministic": deterministic,
                "initial": (
                    None if not compare_initial else _evaluation_aggregate(
                        evaluations["initial"]
                    )
                ),
                "trained": _evaluation_aggregate(eval_one),
            },
            "totals": {
                "optimizer_updates": len(update_records),
                "training_decisions": training_decisions,
                "evaluation_decisions": sum(
                    sum(
                        int(episode["decision_count"])
                        for episode in item["episodes"]
                    )
                    for item in evaluations.values()
                ),
                "all_decisions": training_decisions + sum(
                    sum(
                        int(episode["decision_count"])
                        for episode in item["episodes"]
                    )
                    for item in evaluations.values()
                ),
                "training_engine_frames": training_frames,
                "evaluation_engine_frames": sum(
                    sum(
                        int(episode["frame_count"])
                        for episode in item["viewer_offline"]["episodes"]
                    )
                    for item in evaluations.values()
                ),
            },
            "timings_seconds": {
                "initial_checkpoint_preparation": initial_preparation_seconds,
                "rollout_total": sum(
                    float(record["rollout_seconds"])
                    for record in update_records
                ),
                "model_update_and_metrics_total": sum(
                    float(record["model_update_and_metrics_seconds"])
                    for record in update_records
                ),
                "training_total": training_seconds,
                "checkpoint_save": checkpoint_save_seconds,
                "evaluations": evaluation_seconds,
                "roundtrip_total": time.perf_counter() - started,
            },
            "viewer_command": viewer_command,
            "limitations": [
                (
                    "One fixed-target public scenario with 12-decision task horizons."
                    if task_config is not None
                    else "One public scenario with 32-decision truncated episodes."
                    if provider_config is not None
                    else "One public scenario with eight-decision truncated episodes."
                    if contract_version == "v2"
                    else "One public scenario with two-decision truncated episodes."
                ),
                "This proves linkage and determinism, not policy improvement or convergence.",
                "No Map123, long-run, browser/socket, self-play, or private-data validation.",
                "Model-update timing includes the small metrics-write tail after each core update.",
            ],
        }
        result["semantic_hash"] = canonical_hash(result)
        if not result["roundtrip_complete"]:
            raise RuntimeError("training roundtrip checks failed")
        evidence.stage = "summary"
        _write_json(output / "roundtrip_summary.json", result)
        return result
    except BaseException as exc:
        evidence.record_failure(exc)
        raise
    finally:
        if runner is not None:
            runner.env.close()


def _replay_frame_count(
    episode_root: Path,
    *,
    map_config: Path,
    scenario_path: Path,
) -> int:
    return load_replay_view(
        episode_root,
        map_config=map_config,
        scenario_config=scenario_path,
    ).frame_count


def _evaluation_aggregate(
    evaluation: Mapping[str, object],
) -> dict[str, object]:
    episodes = evaluation["episodes"]
    assert isinstance(episodes, list)
    returns = [float(episode["training_return"]) for episode in episodes]
    return {
        "checkpoint_parameter_hash": evaluation["checkpoint_parameter_hash"],
        "episode_count": len(episodes),
        "evaluation_seeds": list(evaluation["evaluation_seeds"]),
        "mean_composite_return": sum(returns) / len(returns),
        "composite_returns": returns,
        "mean_training_return": sum(returns) / len(returns),
        "training_returns": returns,
        "task_successes": [episode["task_success"] for episode in episodes],
        "outcomes": [episode["outcome"] for episode in episodes],
        "terminated": [episode["terminated"] for episode in episodes],
        "truncated": [episode["truncated"] for episode in episodes],
        "decision_counts": [episode["decision_count"] for episode in episodes],
    }


def _repository_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        capture_output=True, check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "UNKNOWN"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser(
        "run", help="run bounded training and fresh-process evaluation"
    )
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    run.add_argument("--map-config", type=Path, default=DEFAULT_MAP)
    run.add_argument("--updates", type=int, default=1)
    run.add_argument("--evaluation-episodes", type=int, default=1)
    run.add_argument(
        "--contract-version", choices=("v1", "v2"), default="v1"
    )
    run.add_argument("--objective-task-config", type=Path)
    run.add_argument("--objective-reward-config", type=Path)
    run.add_argument("--rollout-length", type=int)
    run.add_argument("--env-index", type=int, default=0)
    run.add_argument("--evaluation-env-index", type=int, default=1000)
    run.add_argument("--evaluation-episode-index-start", type=int, default=200000)
    run.add_argument("--total-timeout-seconds", type=float,
                     default=TOTAL_TIMEOUT_SECONDS)
    run.add_argument(
        "--compare-initial",
        action="store_true",
        help=(
            "save a zero-update model and compare it with the trained model "
            "on paired evaluation seeds"
        ),
    )
    run.add_argument(
        "--spatial-candidate-provider-config",
        type=Path,
        help=(
            "explicitly enable bounded Decision-v2 spatial candidates; "
            f"public config: {DEFAULT_SPATIAL_PROVIDER}"
        ),
    )
    evaluate = subparsers.add_parser(
        "evaluate", help="load a checkpoint and evaluate in this fresh process"
    )
    evaluate.add_argument("--device", choices=("cpu", "cuda:0"), default="cpu")
    evaluate.add_argument("--checkpoint", required=True, type=Path)
    evaluate.add_argument("--output", required=True, type=Path)
    evaluate.add_argument("--ppo-config", required=True, type=Path)
    evaluate.add_argument("--runner-config", required=True, type=Path)
    evaluate.add_argument("--scenario", required=True, type=Path)
    evaluate.add_argument("--map-config", required=True, type=Path)
    evaluate.add_argument("--episodes", type=int, default=1)
    evaluate.add_argument("--owner-token", help=argparse.SUPPRESS)
    evaluate.add_argument(
        "--spatial-candidate-provider-config",
        type=Path,
        help=argparse.SUPPRESS,
    )
    evaluate.add_argument("--objective-task-config", type=Path)
    evaluate.add_argument("--objective-reward-config", type=Path)
    evaluate.add_argument("--evaluation-episode-index-start", type=int)
    evaluate.add_argument("--evaluation-env-index", type=int)
    evaluate.add_argument(
        "--evaluation-policy-mode", choices=("argmax", "sample"),
        default="argmax",
    )
    evaluate.add_argument("--evaluation-policy-seed", type=int)
    evaluate.add_argument(
        "--aggregate-timing", action="store_true",
        help="record bounded observer-only aggregate training-path timings",
    )
    evaluate.add_argument(
        "--disable-planning-world-hash-cache", action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    timeout = float(getattr(args, "total_timeout_seconds", TOTAL_TIMEOUT_SECONDS))
    if not 1.0 <= timeout <= 600.0:
        raise ValueError("total timeout must be in [1, 600] seconds")
    deadline = time.monotonic() + timeout
    with _wall_time_limit(timeout):
        if args.command == "run":
            result = run_roundtrip(
                args.output, scenario_path=args.scenario,
                map_config=args.map_config, updates=args.updates,
                evaluation_episodes=args.evaluation_episodes,
                compare_initial=args.compare_initial,
                contract_version=args.contract_version,
                spatial_candidate_provider_path=(
                    args.spatial_candidate_provider_config
                ),
                deadline=deadline,
                objective_task_path=args.objective_task_config,
                objective_reward_path=args.objective_reward_config,
                rollout_length=args.rollout_length,
                env_index=args.env_index,
                evaluation_env_index=args.evaluation_env_index,
                evaluation_episode_index_start=(
                    args.evaluation_episode_index_start
                ),
            )
        else:
            result = evaluate_checkpoint(
                checkpoint=args.checkpoint,
                output=args.output,
                ppo_config=args.ppo_config,
                runner_config=args.runner_config,
                scenario_path=args.scenario,
                map_config=args.map_config,
                episodes=args.episodes,
                owner_token=args.owner_token,
                spatial_candidate_provider_path=(
                    args.spatial_candidate_provider_config
                ),
                objective_task_path=args.objective_task_config,
                objective_reward_path=args.objective_reward_config,
                evaluation_episode_index_start=(
                    args.evaluation_episode_index_start
                ),
                evaluation_env_index=args.evaluation_env_index,
                timing_enabled=args.aggregate_timing,
                planning_hash_cache_enabled=(
                    not args.disable_planning_world_hash_cache
                ),
                evaluation_policy_mode=args.evaluation_policy_mode,
                evaluation_policy_seed=args.evaluation_policy_seed,
                device=args.device,
            )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
