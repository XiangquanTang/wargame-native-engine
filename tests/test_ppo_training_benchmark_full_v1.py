from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from wargame_native_engine.io.serialization import canonical_hash, file_sha256
from wargame_native_engine.training.ppo_benchmark import (
    PPOTrainingBenchmarkError,
    PPOTrainingBenchmarkRunner,
    _publish_aggregate,
    aggregate_benchmark_results,
    build_experiment_source_manifest,
    require_only_treatment_difference,
    rollout_collection_lengths,
)
from wargame_native_engine.training.ppo_benchmark_config import (
    load_ppo_training_benchmark_config,
    normalized_trapezoidal_auc,
)
from wargame_native_engine.training.training_seed import (
    TrainingSeedStream,
    load_training_seed_config,
)


FULL_CONFIG = Path("config/benchmarks/native_ppo_training_benchmark_full_v1.json")
FULL_HASH = "3ac63af1c6774146129f05d77474cc8fcc68ab9782fbce2270629c8f00a07f72"
FULL_EVAL_HASH = "14b751179fec3b54d8e37acb342e2788ea988c68601529b1925fcab002fae67e"


def _protocol():
    return load_ppo_training_benchmark_config(FULL_CONFIG)


def _point(spec, parameter: str, transition: int, win_rate: float) -> dict[str, object]:
    wins = int(round(win_rate * 30))
    return {
        "parameter_hash": parameter,
        "action_sequence_hash": canonical_hash([spec.scenario_id, spec.train_seed_index, transition]),
        "outcome_sequence": ["WIN"] * wins + ["DRAW"] * (30 - wins),
        "wins": wins, "losses": 0, "draws": 30 - wins, "truncated": 0,
        "win_rate": win_rate,
        "base_return_mean": win_rate,
        "base_return_std": 0.0,
        "shaping_return_mean": 0.0,
        "shaping_return_std": 0.0,
        "composite_return_mean": win_rate,
        "composite_return_std": 0.0,
        "episode_length_mean": 10.0,
        "episode_length_std": 0.0,
        "environment_transitions": transition,
        "update_step": transition // 2_000,
        "checkpoint_id": "INITIAL" if transition == 0 else "LAST" if transition == 50_000 else f"TRANSITION_{transition}",
        "eval_episodes": 30,
        "eval_seed_set_hash": FULL_EVAL_HASH,
    }


def _summary(spec, *, shaping: bool) -> dict[str, object]:
    initial = canonical_hash([spec.scenario_id, spec.train_seed_index, "initial"])
    final = canonical_hash([spec.run_id, "final"])
    if spec.scenario_id == "demo-direct-fire-basic":
        final_rate = 0.6 if shaping else 0.4
    else:
        final_rate = 0.4 if shaping else 0.6
    points = []
    for transition in (0, 10_000, 20_000, 30_000, 40_000, 50_000):
        fraction = transition / 50_000
        rate = 0.5 + (final_rate - 0.5) * fraction
        point = _point(spec, initial if transition == 0 else final, transition, rate)
        if shaping:
            point["shaping_return_mean"] = 0.1 * fraction
            point["composite_return_mean"] = rate + 0.1 * fraction
        points.append(point)
    auc = normalized_trapezoidal_auc(tuple(
        (int(item["environment_transitions"]), float(item["win_rate"]))
        for item in points
    ))
    reached = lambda threshold: next(
        (int(item["environment_transitions"]) for item in points if float(item["win_rate"]) >= threshold),
        None,
    )
    return {
        **spec.to_dict(), "status": "COMPLETE",
        "initial_parameter_hash": initial, "final_parameter_hash": final,
        "environment_transitions": 50_000, "updates": 25,
        "episodes_completed": 100, "invalid_action_attempts": 0,
        "stale_action_attempts": 0, "replay_mismatches": 0,
        "final_win_rate": final_rate, "final_base_return": final_rate,
        "final_shaping_return": 0.1 if shaping else 0.0,
        "final_composite_return": final_rate + (0.1 if shaping else 0.0),
        "win_rate_auc": auc, "t50": reached(0.50), "t75": reached(0.75),
        "final_episode_length": 10.0, "training_wall_time": 100.0,
        "evaluation_wall_time": 10.0, "total_wall_time": 110.0,
        "transitions_per_second": 500.0, "shaping_signal_nonzero": shaping,
        "evaluation_seed_set_hash": FULL_EVAL_HASH,
        "evaluation_points": points,
    }


def _summaries():
    protocol = _protocol()
    return [
        _summary(spec, shaping=spec.treatment_id == "EIGHT_FACTOR_SHAPING")
        for spec in protocol.run_matrix()
    ]


def test_f1_f2_f3_f4_full_identity_matrix_and_pairing() -> None:
    protocol = _protocol()
    assert protocol.content_hash == FULL_HASH
    assert len(protocol.run_matrix()) == 20
    assert protocol.train_seed_indices == (0, 1, 2, 3, 4)
    keys = {(item.scenario_id, item.train_seed_index) for item in protocol.run_matrix()}
    assert len(keys) == 10
    assert all(sum(
        item.scenario_id == scenario and item.train_seed_index == seed
        for item in protocol.run_matrix()
    ) == 2 for scenario, seed in keys)


def test_f5_only_reward_differs() -> None:
    protocol = _protocol()
    for seed in protocol.train_seed_indices:
        require_only_treatment_difference(protocol, train_seed_index=seed, trained_side="RED")


def test_f6_f7_initial_parameter_and_transition_zero_equality() -> None:
    result = aggregate_benchmark_results(_protocol(), _summaries())
    assert len(result["paired_comparisons"]) == 10
    for item in result["run_summaries"]:
        peer = next(other for other in result["run_summaries"] if (
            other["scenario_id"], other["train_seed_index"]
        ) == (item["scenario_id"], item["train_seed_index"]) and other["treatment_id"] != item["treatment_id"])
        assert item["initial_parameter_hash"] == peer["initial_parameter_hash"]
        assert item["evaluation_points"][0]["action_sequence_hash"] == peer["evaluation_points"][0]["action_sequence_hash"]


def test_f8_f11_evaluation_seed_hash_and_30_distinct_episodes() -> None:
    protocol = _protocol()
    authority = load_training_seed_config()
    seeds = tuple(authority.derive_seed(
        TrainingSeedStream.EVALUATION, env_index=env, episode_index=episode,
    ) for env, episode in protocol.evaluation_episode_coordinates)
    assert protocol.eval_seed_set_hash == FULL_EVAL_HASH
    assert len(seeds) == len(set(seeds)) == 30


def test_f9_f10_exact_budget_and_evaluation_boundaries() -> None:
    protocol = _protocol()
    lengths = rollout_collection_lengths(protocol.evaluation_cadence_transitions, 2048)
    assert sum(lengths) == protocol.transitions_per_run == 50_000
    assert len(lengths) == 25
    assert lengths == (2048, 2048, 2048, 2048, 1808) * 5
    assert protocol.evaluation_cadence_transitions == (0, 10_000, 20_000, 30_000, 40_000, 50_000)


def test_f12_f13_last_checkpoint_and_raw_rows() -> None:
    result = aggregate_benchmark_results(_protocol(), _summaries())
    assert _protocol().checkpoint_selection == "LAST_CHECKPOINT_ONLY"
    assert all(len(item["evaluation_points"]) == 6 for item in result["run_summaries"])
    assert sum(len(item["evaluation_points"]) for item in result["run_summaries"]) == 120


def test_f14_f15_auc_and_thresholds() -> None:
    assert normalized_trapezoidal_auc(((0, 0.0), (25_000, 0.5), (50_000, 1.0))) == 0.5
    direct_shaping = next(item for item in _summaries() if (
        item["scenario_id"] == "demo-direct-fire-basic" and item["treatment_id"] == "EIGHT_FACTOR_SHAPING"
    ))
    assert direct_shaping["t50"] == 0
    assert direct_shaping["t75"] is None


def test_f16_f17_f18_paired_macro_and_bootstrap_are_deterministic() -> None:
    first = aggregate_benchmark_results(_protocol(), _summaries())
    second = aggregate_benchmark_results(_protocol(), list(reversed(_summaries())))
    assert first["paired_comparisons"] == second["paired_comparisons"]
    assert first["macro_summary"] == second["macro_summary"]
    assert first["scenario_summaries"] == second["scenario_summaries"]
    assert all(item["paired_final_delta"]["bootstrap_resamples"] == 10_000 for item in first["scenario_summaries"])


def test_f19_failed_or_missing_pair_is_flagged() -> None:
    summaries = _summaries()[:-1]
    result = aggregate_benchmark_results(_protocol(), summaries, ({"run_id": "failed", "status": "FAILED"},))
    assert result["implementation_status"] == "PARTIAL"
    assert result["research_result"] == "INCONCLUSIVE"
    assert len(result["paired_comparisons"]) == 9


def test_f20_f21_f22_completion_is_atomic_and_strict(tmp_path: Path) -> None:
    protocol = _protocol()
    runner = PPOTrainingBenchmarkRunner(protocol, output_root=tmp_path)
    spec = protocol.run_matrix()[0]
    target = tmp_path / "runs" / spec.run_id
    target.mkdir(parents=True)
    assert not runner._is_complete(target, spec)
    (target / "checkpoints/final").mkdir(parents=True)
    (target / "checkpoints/final/state.pt").write_bytes(b"state")
    for name, value in (
        ("final_summary.json", "{}\n"), ("run_manifest.json", "{}\n"),
        ("train_metrics.jsonl", "{}\n"), ("evaluation_metrics.jsonl", "{}\n"),
    ):
        (target / name).write_text(value, encoding="utf-8")
    completion = {
        "schema_version": "native-ppo-benchmark-run-completion/1.0",
        "benchmark_hash": protocol.content_hash, "run_id": spec.run_id,
        "run_manifest_hash": "x", "summary_hash": file_sha256(target / "final_summary.json"),
        "checkpoint_manifest_hash": "y", "status": "COMPLETE",
        "train_metrics_hash": file_sha256(target / "train_metrics.jsonl"),
        "evaluation_metrics_hash": file_sha256(target / "evaluation_metrics.jsonl"),
        "run_manifest_file_hash": file_sha256(target / "run_manifest.json"),
    }
    completion["semantic_hash"] = canonical_hash(completion)
    (target / "completion.json").write_text(json.dumps(completion), encoding="utf-8")
    assert runner._is_complete(target, spec)
    (target / "train_metrics.jsonl").write_text("corrupt", encoding="utf-8")
    assert not runner._is_complete(target, spec)


def test_f23_f24_source_manifest_is_deterministic_and_records_dirty_worktree() -> None:
    first = build_experiment_source_manifest()
    second = build_experiment_source_manifest()
    assert first["semantic_hash"] == second["semantic_hash"]
    assert len(first["git_head"]) == 40
    assert first["dirty"] is True
    assert first["relevant_files"]


def test_f25_f26_reward_treatment_records_are_isolated() -> None:
    summaries = _summaries()
    assert all(item["final_shaping_return"] == 0.0 for item in summaries if item["treatment_id"] == "BASE_ONLY")
    assert all(item["shaping_signal_nonzero"] for item in summaries if item["treatment_id"] == "EIGHT_FACTOR_SHAPING")


def test_f27_f28_f29_integrity_failures_reject_full_run() -> None:
    protocol = _protocol()
    for field, value, match in (
        ("invalid_action_attempts", 1, "invalid"),
        ("stale_action_attempts", 1, "stale"),
        ("replay_mismatches", 1, "replay"),
        ("final_win_rate", math.nan, "non-finite"),
    ):
        summaries = _summaries()
        summaries[0][field] = value
        with pytest.raises(PPOTrainingBenchmarkError, match=match):
            aggregate_benchmark_results(protocol, summaries)


def test_f30_full_artifact_rows_have_deterministic_order(tmp_path: Path) -> None:
    result = aggregate_benchmark_results(_protocol(), list(reversed(_summaries())))
    first, second = tmp_path / "first", tmp_path / "second"
    _publish_aggregate(first, result)
    _publish_aggregate(second, result)
    expected = {
        "benchmark_result.json", "full_research_summary.json", "learning_curve.csv",
        "final_runs.csv", "paired_comparison.csv", "scenario_summary.csv",
        "treatment_summary.csv", "operational_cost.csv",
    }
    assert {item.name for item in first.iterdir()} == expected
    assert all((first / name).read_bytes() == (second / name).read_bytes() for name in expected)
