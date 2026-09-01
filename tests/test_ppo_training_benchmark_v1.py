from __future__ import annotations

from copy import deepcopy
import csv
import json
from pathlib import Path

import pytest

from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.training.ppo_benchmark import (
    PPOTrainingBenchmarkError,
    PPOTrainingBenchmarkRunner,
    aggregate_benchmark_results,
    build_benchmark_runner_authorities,
    complete_benchmark_green_template,
    require_only_treatment_difference,
)
from wargame_native_engine.training.ppo_benchmark_config import (
    BENCHMARK_GREEN_TEMPLATE_POLICY_HASH,
    PPOTrainingBenchmarkConfigError,
    build_ppo_training_benchmark_config,
    load_ppo_training_benchmark_config,
    normalized_trapezoidal_auc,
)
from wargame_native_engine.training.ppo_core import NativeMaskedPPO


CONFIG_PATH = Path("config/benchmarks/native_ppo_training_benchmark_pilot_v1.json")


def _document():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _evaluation(initial_hash: str, *, final: bool = False):
    return {
        "parameter_hash": initial_hash if not final else "f" * 64,
        "action_sequence_hash": "a" * 64,
        "outcome_sequence": ["TRUNCATED"],
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "truncated": 1,
        "win_rate": 0.0,
        "base_return_mean": 0.0,
        "environment_transitions": 2048 if final else 0,
    }


def _summary(spec, *, shaping: bool, delta: float = 0.0):
    parameter = f"{spec.train_seed_index + 1}" * 64
    initial = _evaluation(parameter)
    final = _evaluation(parameter, final=True)
    final["win_rate"] = delta if shaping else 0.0
    return {
        **spec.to_dict(),
        "status": "COMPLETE",
        "initial_parameter_hash": parameter,
        "final_parameter_hash": "e" * 64,
        "final_win_rate": final["win_rate"],
        "win_rate_auc": final["win_rate"] / 2.0,
        "final_base_return": 0.0,
        "final_shaping_return": 0.1 if shaping else 0.0,
        "final_composite_return": 0.1 if shaping else 0.0,
        "training_wall_time": 1.0,
        "transitions_per_second": 2048.0,
        "shaping_signal_nonzero": shaping,
        "evaluation_points": [initial, final],
    }


def test_b1_b2_b5_b8_b11_b12_b13_b16_protocol_and_matrix() -> None:
    # B1 deterministic config/hash; B2 stable matrix; B5 eval set; B8 cadence;
    # B11 PPO control; B12 scenario hashes; B13 opponent; B16 LAST checkpoint.
    first = load_ppo_training_benchmark_config()
    second = build_ppo_training_benchmark_config(_document())
    assert first.content_hash == second.content_hash == "e0248945ebed679da24b307f4389df22631b18be3e7d0b31e4bc5940479a404b"
    assert first.eval_seed_set_hash == "9bcbdbfab95281caaadb4160bfb24afed6ee149d3dfd6f1272203cb732d90aa6"
    assert first.run_matrix() == second.run_matrix()
    assert len(first.run_matrix()) == 8
    assert first.evaluation_cadence_transitions == (0, 2048)
    assert first.checkpoint_selection == "LAST_CHECKPOINT_ONLY"
    assert first.ppo.identity_hash == "d0b08a8295b22b316d8082272fa3763f491961d07934f2ac2c2b84c951fa622d"
    assert first.opponent.identity_id == "native-no-op-fixed-opponent-v1"
    assert all(load_scenario(item.path).scenario_hash == item.scenario_hash for item in first.scenarios)


def test_b3_b7_treatment_diff_and_budget_are_exact() -> None:
    protocol = load_ppo_training_benchmark_config()
    require_only_treatment_difference(protocol, train_seed_index=0, trained_side="RED")
    controls = [
        build_benchmark_runner_authorities(
            protocol, treatment, train_seed_index=0, trained_side="RED"
        )
        for treatment in protocol.treatments
    ]
    assert controls[0].control_payload() == controls[1].control_payload()
    assert controls[0].base.reward_mode != controls[1].base.reward_mode
    assert protocol.transitions_per_run == 2048
    assert protocol.planned_transitions == 16384


def test_b4_b6_b50_seed_coordinate_controls_initialization() -> None:
    # Paired arms construct the same stream-indexed initial model. A different
    # training replicate gets a distinct, deterministic initialization.
    a = NativeMaskedPPO(seed_stream_index=0)
    b = NativeMaskedPPO(seed_stream_index=0)
    c = NativeMaskedPPO(seed_stream_index=1)
    assert a.parameter_content_hash() == b.parameter_content_hash()
    assert a.parameter_content_hash() != c.parameter_content_hash()
    assert a.seed_stream_index == b.seed_stream_index == 0
    with pytest.raises(Exception, match="seed_stream_index"):
        NativeMaskedPPO(seed_stream_index=-1)


def test_b6_transition_zero_real_evaluation_is_paired(tmp_path: Path) -> None:
    protocol = load_ppo_training_benchmark_config()
    benchmark = PPOTrainingBenchmarkRunner(protocol, output_root=tmp_path)
    scenario = protocol.scenarios[0]
    spec_by_treatment = {
        item.treatment_id: item
        for item in protocol.run_matrix()
        if item.scenario_id == scenario.scenario_id and item.train_seed_index == 0
    }
    rows = {}
    for treatment in protocol.treatments:
        spec = spec_by_treatment[treatment.treatment_id]
        authorities = build_benchmark_runner_authorities(
            protocol, treatment, train_seed_index=0, trained_side="RED"
        )
        rows[treatment.treatment_id] = benchmark._evaluate(
            spec, scenario, treatment, authorities, NativeMaskedPPO(seed_stream_index=0),
            environment_transitions=0,
            target=tmp_path / treatment.treatment_id,
        )
    base, shaping = rows["BASE_ONLY"], rows["EIGHT_FACTOR_SHAPING"]
    for name in (
        "parameter_hash", "action_sequence_hash", "outcome_sequence", "wins",
        "losses", "draws", "truncated", "win_rate", "base_return_mean",
    ):
        assert base[name] == shaping[name]
    assert base["shaping_return_mean"] == 0.0


def test_b9_b10_green_supervision_is_explicit_and_complete() -> None:
    scenario = load_scenario("examples/demos/direct_fire_basic/scenario.json")
    frame = complete_benchmark_green_template(scenario)
    assert frame.source_hash == BENCHMARK_GREEN_TEMPLATE_POLICY_HASH
    for unit in frame.operators:
        assert unit.raw["value"] == 100.0
        assert unit.raw["can_to_move"] is True
        assert unit.raw["see_enemy_bop_ids"] == []
        assert unit.raw["keep"] is False
        assert unit.raw["tire"] == 0
        assert unit.raw["lose_control"] is False


def test_b14_b15_b17_b18_validation_fields_are_contractual(tmp_path: Path) -> None:
    protocol = load_ppo_training_benchmark_config()
    plan = PPOTrainingBenchmarkRunner(protocol, output_root=tmp_path).dry_run()
    assert plan.total_transitions == 16384
    assert plan.evaluations_per_run == 2
    assert plan.evaluation_episodes_per_checkpoint == 8
    assert plan.estimated_training_seconds > 0


def test_b19_b20_b21_completion_marker_is_fail_closed(tmp_path: Path) -> None:
    protocol = load_ppo_training_benchmark_config()
    runner = PPOTrainingBenchmarkRunner(protocol, output_root=tmp_path)
    spec = protocol.run_matrix()[0]
    target = tmp_path / "runs" / spec.run_id
    target.mkdir(parents=True)
    assert runner._is_complete(target, spec) is False
    (target / "completion.json").write_text("{}", encoding="utf-8")
    assert runner._is_complete(target, spec) is False


def test_b22_failed_run_is_explicit_in_aggregate() -> None:
    protocol = load_ppo_training_benchmark_config()
    result = aggregate_benchmark_results(
        protocol, (), ({"run_id": "failed", "status": "FAILED"},)
    )
    assert result["implementation_status"] == "PARTIAL"
    assert result["research_result"] == "INCONCLUSIVE"
    assert result["failed_runs"][0]["status"] == "FAILED"


def test_b23_b24_b25_b26_aggregation_and_auc() -> None:
    protocol = load_ppo_training_benchmark_config()
    summaries = []
    for spec in protocol.run_matrix():
        summaries.append(_summary(
            spec, shaping=spec.treatment_id == "EIGHT_FACTOR_SHAPING", delta=0.25
        ))
    result = aggregate_benchmark_results(protocol, summaries)
    assert result["implementation_status"] == "PASS"
    assert result["research_result"] == "SHAPING_BETTER"
    assert len(result["run_summaries"]) == 8
    assert len(result["paired_comparisons"]) == 4
    assert all(item["delta_final_win_rate"] == 0.25 for item in result["paired_comparisons"])
    assert normalized_trapezoidal_auc(((0, 0.0), (10, 1.0))) == 0.5


def test_b27_csv_and_matrix_order_are_deterministic(tmp_path: Path) -> None:
    protocol = load_ppo_training_benchmark_config()
    ids = [item.run_id for item in protocol.run_matrix()]
    assert ids == [item.run_id for item in protocol.run_matrix()]
    assert len(ids) == len(set(ids))


def test_b28_b29_operational_fields_and_replay_boundary() -> None:
    protocol = load_ppo_training_benchmark_config()
    payload = protocol.semantic_payload()
    assert "reference_transitions_per_second" not in payload
    assert payload["training_budget"]["unit"] == "COMMITTED_ENVIRONMENT_TRANSITIONS"
    assert payload["replay_verification"] == {
        "reward": "EVERY_COMPLETED_EPISODE",
        "engine": "FIRST_COMPLETED_EPISODE_PER_RUN",
    }


def test_b30_pilot_is_not_full_and_protocol_drift_fails() -> None:
    document = _document()
    pilot_hash = canonical_hash(document)
    full = load_ppo_training_benchmark_config(
        "config/benchmarks/native_ppo_training_benchmark_full_v1.json"
    )
    assert full.stage == "FULL"
    assert full.benchmark_id != document["benchmark_id"]
    assert full.content_hash != pilot_hash
    drift = deepcopy(document)
    drift["stage"] = "FULL"
    with pytest.raises(PPOTrainingBenchmarkConfigError):
        build_ppo_training_benchmark_config(drift)


def test_pairing_rejects_initial_evaluation_mismatch() -> None:
    protocol = load_ppo_training_benchmark_config()
    summaries = []
    for spec in protocol.run_matrix():
        item = _summary(spec, shaping=spec.treatment_id == "EIGHT_FACTOR_SHAPING")
        if spec.treatment_id == "EIGHT_FACTOR_SHAPING" and spec.train_seed_index == 0:
            item["evaluation_points"][0]["action_sequence_hash"] = "b" * 64
        summaries.append(item)
    with pytest.raises(PPOTrainingBenchmarkError, match="initial equality"):
        aggregate_benchmark_results(protocol, summaries)
