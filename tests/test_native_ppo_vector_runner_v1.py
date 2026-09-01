from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.run_minimal_source_replay import _template
from wargame_native_engine.agents import NoOpAgent
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.training import NativeTrainingEnv, NativeTrainingError
from wargame_native_engine.training.ppo_config import build_ppo_algorithm_config
from wargame_native_engine.training.ppo_core import (
    MaskedCategorical,
    NativeMaskedPPO,
    PPOCheckpointError,
    compute_gae,
    torch,
)
from wargame_native_engine.training.ppo_runner import NativePPORunner
from wargame_native_engine.training.ppo_runner_config import build_ppo_runner_config
from wargame_native_engine.training.ppo_vector_runner import (
    NativeSynchronousVectorPPORunner,
    PPOVectorRunnerError,
)
from wargame_native_engine.training.ppo_vector_runner_config import (
    PPOVectorRunnerConfigError,
    build_ppo_vector_runner_config,
    load_ppo_vector_runner_config,
)


SCENARIO_PATH = Path("examples/source_movement_scenario.json")
PPO_CONFIG_PATH = Path("config/training/native_masked_ppo_v1.json")
RUNNER_CONFIG_PATH = Path("config/training/native_ppo_runner_v1.json")
VECTOR_CONFIG_PATH = Path("config/training/native_ppo_vector_runner_v1.json")


class EnvironmentFactory:
    def __init__(self, *, complete_analysis: bool = False) -> None:
        self.scenario = load_scenario(SCENARIO_PATH)
        self.complete_analysis = complete_analysis

    def __call__(self):
        template = _template(self.scenario)
        if self.complete_analysis:
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
            for unit in document["operators"]:
                unit.update({
                    "value": 100.0, "can_to_move": True,
                    "see_enemy_bop_ids": [], "keep": False,
                    "tire": 0, "lose_control": False,
                })
            template = SourceFrameImporter().from_dict(
                document, source_hash="ppo-vector-analyzer-complete-fixture"
            )
        return SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(self.scenario)), template=template
        )


def _authorities(*, rollout_length=4, side="red", composite=False, env_index=0):
    ppo_document = json.loads(PPO_CONFIG_PATH.read_text(encoding="utf-8"))
    ppo_document["rollout_length"] = rollout_length
    ppo_document["minibatch_size"] = min(2, rollout_length)
    ppo_document["update_epochs"] = 1
    ppo_document["network"]["hidden_sizes"] = [8, 8]
    ppo = build_ppo_algorithm_config(ppo_document)
    runner_document = json.loads(RUNNER_CONFIG_PATH.read_text(encoding="utf-8"))
    runner_document["purpose"] = "TEST_SMOKE"
    runner_document["trained_side"] = side.upper()
    runner_document["reward_mode"] = (
        "COMPOSITE_EIGHT_FACTOR" if composite else "BASE_ONLY"
    )
    runner_document["ppo_config_ref"]["hash"] = ppo.content_hash
    runner_document["env_index"] = env_index
    runner_document["checkpoint_every_updates"] = None
    runner_document["run_limits"] = {
        "max_updates": 1, "max_environment_steps": None, "max_episodes": None,
    }
    base = build_ppo_runner_config(runner_document, ppo=ppo)
    return ppo, base


def _vector_config(ppo, base, *, num_envs):
    value = json.loads(VECTOR_CONFIG_PATH.read_text(encoding="utf-8"))
    value["purpose"] = "TEST_SMOKE"
    value["num_envs"] = num_envs
    value["trained_side"] = base.trained_side
    value["reward_mode"] = base.reward_mode.value
    value["base_runner_config_ref"]["hash"] = base.content_hash
    value["ppo_config_ref"]["hash"] = ppo.content_hash
    value["rollout_target"]["transitions_per_update"] = ppo.rollout_length
    return build_ppo_vector_runner_config(
        value, base_runner=base, ppo=ppo
    )


def _env(factory, root, index, *, side="red", composite=False, max_steps=2,
         opponent=None):
    return NativeTrainingEnv(
        factory, controlled_side=side,
        opponent=opponent or NoOpAgent("native-no-op-fixed-opponent-v1"),
        storage_root=root / f"facts-{index}", max_actions=256,
        max_steps=max_steps, delta_time_seconds=20,
        enable_eight_factor_shaping=composite,
    )


def _vector(tmp_path, *, num_envs=2, rollout_length=4, side="red",
            composite=False, max_steps=2, artifacts=False):
    ppo, base = _authorities(
        rollout_length=rollout_length, side=side, composite=composite
    )
    factory = EnvironmentFactory(complete_analysis=composite)
    envs = [
        _env(factory, tmp_path, index, side=side, composite=composite,
             max_steps=max_steps)
        for index in range(num_envs)
    ]
    runner = NativeSynchronousVectorPPORunner(
        envs, NativeMaskedPPO(ppo),
        vector_config=_vector_config(ppo, base, num_envs=num_envs),
        base_runner_config=base, run_namespace="ppo-vector-test",
        artifact_root=tmp_path / "run" if artifacts else None,
    )
    return runner, factory


def test_vector_production_authority_and_hash() -> None:
    config = load_ppo_vector_runner_config()
    assert config.content_hash == (
        "b95c355a38a854b9426c971e07501b4529b801f782d1f32ed30016425e0a7a34"
    )
    assert config.num_envs == 4
    assert config.transitions_per_update == 2048
    assert config.rollout_target_semantics == "TOTAL_TRANSITIONS_PER_UPDATE"
    assert config.collection_mode == "SYNCHRONOUS"


def test_v39_v40_vector_hash_and_num_env_validation() -> None:
    ppo, base = _authorities()
    one = _vector_config(ppo, base, num_envs=1)
    two = _vector_config(ppo, base, num_envs=2)
    assert one.content_hash == _vector_config(ppo, base, num_envs=1).content_hash
    assert one.content_hash != two.content_hash
    value = json.loads(VECTOR_CONFIG_PATH.read_text(encoding="utf-8"))
    value["purpose"] = "TEST_SMOKE"
    value["num_envs"] = 0
    value["base_runner_config_ref"]["hash"] = base.content_hash
    value["ppo_config_ref"]["hash"] = ppo.content_hash
    value["rollout_target"]["transitions_per_update"] = ppo.rollout_length
    with pytest.raises(PPOVectorRunnerConfigError):
        build_ppo_vector_runner_config(value, base_runner=base, ppo=ppo)


def test_v1_num_env_one_differential_matches_single_eval(tmp_path) -> None:
    vector, _ = _vector(tmp_path / "vector", num_envs=1)
    ppo, base = _authorities()
    factory = EnvironmentFactory()
    single = NativePPORunner(
        _env(factory, tmp_path / "single", 0), NativeMaskedPPO(ppo),
        config=base, run_namespace="ppo-vector-test",
    )
    try:
        vector_report = vector.evaluate(1)
        single_report = single.evaluate(1)
        vector_episode = vector_report.episodes[0].episode
        single_episode = single_report.episodes[0]
        assert vector_episode.environment_seed == single_episode.environment_seed
        assert vector_episode.action_ids == single_episode.action_ids
        assert vector_episode.reward_record_hashes == single_episode.reward_record_hashes
        assert vector_episode.outcome == single_episode.outcome
        assert vector_episode.base_return == single_episode.base_return
    finally:
        vector.close()
        single.env.close()


def test_v2_v3_v4_v6_v7_v8_v9_v10_v11_v12_vector_collection(tmp_path) -> None:
    runner, _ = _vector(tmp_path, num_envs=2, rollout_length=4)
    try:
        collection = runner.collect_rollout()
        assert len({item.env_index for item in runner.states}) == 2
        assert runner.envs[0] is not runner.envs[1]
        assert runner.envs[0]._manager is not runner.envs[1]._manager
        assert (
            id(runner.envs[0]._manager), runner.envs[0]._session_id
        ) != (
            id(runner.envs[1]._manager), runner.envs[1]._session_id
        )
        assert runner.envs[0].opponent is not runner.envs[1].opponent
        assert collection.batch.observations.shape == (4, 8302)
        assert collection.batch.observations.dtype is torch.float32
        assert collection.batch.action_masks.shape == (4, 256)
        assert collection.batch.action_masks.dtype is torch.bool
        assert collection.batch_row_mappings == ((0, 1), (0, 1))
        assert all(
            transition.action_mask[transition.action_index]
            for transition in collection.batch.provenance
        )
        logits, _values = runner.core.model(collection.batch.observations[:2])
        distribution = MaskedCategorical(
            logits, collection.batch.action_masks[:2]
        )
        assert bool((distribution.probs.masked_select(
            ~collection.batch.action_masks[:2]
        ) == 0.0).all())
        assert torch.allclose(
            distribution.probs.sum(dim=-1), torch.ones(2), atol=1e-7
        )
        for env_index, segment in enumerate(collection.vector_rollout.segments):
            for transition_index, record in enumerate(segment):
                submitted = runner.envs[env_index].reward_records[
                    transition_index
                ].side_record("red").transition_identity.submitted_action_ids
                assert record.transition.action_id in submitted
        seeds = [item.episode.environment_seed for item in collection.episodes]
        assert len(seeds) == len(set(seeds)) == 2
        assert collection.invalid_action_attempts == (0, 0)
        assert collection.stale_action_rejections == (0, 0)
    finally:
        runner.close()


def test_batched_forward_uses_active_rows_only(tmp_path) -> None:
    runner, _ = _vector(tmp_path, num_envs=2, rollout_length=2, max_steps=5)
    shapes = []
    handle = runner.core.model.register_forward_pre_hook(
        lambda _module, args: shapes.append(tuple(args[0].shape))
    )
    try:
        collection = runner.collect_rollout()
        assert collection.batch_row_mappings == ((0, 1),)
        assert shapes == [(2, 8302), (2, 8302)]
    finally:
        handle.remove()
        runner.close()


def test_v3_num_envs_four_constructs_distinct_authorities(tmp_path) -> None:
    runner, _ = _vector(tmp_path, num_envs=4, rollout_length=4, max_steps=5)
    try:
        collection = runner.collect_rollout()
        assert collection.batch_row_mappings == ((0, 1, 2, 3),)
        assert len({id(env._manager) for env in runner.envs}) == 4
        assert len({(id(env._manager), env._session_id) for env in runner.envs}) == 4
        assert len({id(env.opponent) for env in runner.envs}) == 4
    finally:
        runner.close()


def test_v8_v19_v20_v24_partial_final_step_no_drop_stable_flatten(tmp_path) -> None:
    runner, _ = _vector(tmp_path, num_envs=2, rollout_length=4, max_steps=5)
    try:
        collection = runner.collect_rollout(3)
        assert collection.batch_row_mappings == ((0, 1), (0,))
        assert collection.vector_rollout.total_transitions == 3
        assert len(collection.batch.provenance) == 3
        assert [len(item) for item in collection.vector_rollout.segments] == [2, 1]
        assert tuple(collection.batch.provenance) == tuple(
            item.transition
            for segment in collection.vector_rollout.segments
            for item in segment
        )
        assert not collection.vector_rollout.segments[0][-1].transition.trace_continues
        assert not collection.vector_rollout.segments[1][-1].transition.trace_continues
    finally:
        runner.close()


def test_v5_d6_reset_env0_does_not_consume_env1_policy_rng(tmp_path) -> None:
    runner, _ = _vector(tmp_path, num_envs=2, rollout_length=4, max_steps=1)
    try:
        runner.reset_env(0)
        runner.reset_env(1)
        env1_before = runner.states[1].policy_generator.get_state().clone()
        runner.collect_rollout(1)
        assert not runner.states[0].active
        runner.reset_env(0)
        assert torch.equal(
            env1_before, runner.states[1].policy_generator.get_state()
        )
    finally:
        runner.close()


def test_v15_v16_v17_v18_truncation_uses_post_state_before_reset(tmp_path) -> None:
    runner, _ = _vector(tmp_path, num_envs=2, rollout_length=4, max_steps=1)
    try:
        collection = runner.collect_rollout()
        for segment in collection.vector_rollout.segments:
            assert len(segment) == 2
            first, second = (item.transition for item in segment)
            assert first.truncated and first.bootstrap_allowed
            assert not first.trace_continues
            expected = float(runner.core.predict_value(torch.tensor(
                first.next_observation, dtype=torch.float32
            )).item())
            assert first.next_value_prediction == pytest.approx(expected)
            assert first.next_decision_identity != second.decision_identity
            assert second.truncated and second.bootstrap_allowed
        assert not bool(collection.batch.terminated.any())
    finally:
        runner.close()


def test_v15_terminated_bootstrap_regression_is_zero() -> None:
    result = compute_gae(
        rewards=torch.tensor([1.0], dtype=torch.float32),
        values=torch.tensor([0.25], dtype=torch.float32),
        next_values=torch.tensor([99.0], dtype=torch.float32),
        effective_gammas=torch.tensor([0.99], dtype=torch.float32),
        bootstrap_allowed=torch.tensor([False], dtype=torch.bool),
        trace_continues=torch.tensor([False], dtype=torch.bool),
        gae_lambda=0.95,
    )
    assert result.advantages.item() == pytest.approx(0.75)
    assert result.value_targets.item() == pytest.approx(1.0)


def test_v21_v22_v23_per_env_gae_and_no_cross_trajectory(tmp_path) -> None:
    runner, _ = _vector(tmp_path, num_envs=2, rollout_length=4, max_steps=5)
    try:
        collection = runner.collect_rollout()
        offset = 0
        for segment in collection.vector_rollout.segments:
            transitions = [item.transition for item in segment]
            gae = compute_gae(
                rewards=torch.tensor([item.reward for item in transitions], dtype=torch.float32),
                values=torch.tensor([item.value_prediction for item in transitions], dtype=torch.float32),
                next_values=torch.tensor([item.next_value_prediction for item in transitions], dtype=torch.float32),
                effective_gammas=torch.tensor([item.effective_gamma for item in transitions], dtype=torch.float32),
                bootstrap_allowed=torch.tensor([item.bootstrap_allowed for item in transitions], dtype=torch.bool),
                trace_continues=torch.tensor([item.trace_continues for item in transitions], dtype=torch.bool),
                gae_lambda=runner.core.config.gae_lambda,
            )
            assert torch.equal(
                collection.batch.advantages[offset:offset + len(transitions)],
                gae.advantages,
            )
            offset += len(transitions)
    finally:
        runner.close()


def test_v25_v26_v27_shared_policy_update(tmp_path) -> None:
    runner, _ = _vector(tmp_path, num_envs=2, rollout_length=4)
    before = runner.core.parameter_content_hash()
    try:
        collection, metrics, _seconds = runner.train_update()
        assert metrics.update_step == 1
        assert runner.core.parameter_content_hash() != before
        assert {
            item.policy_update_step
            for segment in collection.vector_rollout.segments for item in segment
        } == {0}
        assert len({id(runner.core.model) for _env in runner.envs}) == 1
    finally:
        runner.close()


def test_v28_v29_v30_reward_mode_uniform_and_observation_isolated(tmp_path) -> None:
    base, _ = _vector(tmp_path / "base", num_envs=2, composite=False)
    shaped, _ = _vector(tmp_path / "shaped", num_envs=2, composite=True)
    try:
        base_rollout = base.collect_rollout()
        shaped_rollout = shaped.collect_rollout()
        assert torch.equal(base_rollout.batch.observations, shaped_rollout.batch.observations)
        assert torch.equal(base_rollout.batch.action_masks, shaped_rollout.batch.action_masks)
        assert [item.action_id for item in base_rollout.batch.provenance] == [
            item.action_id for item in shaped_rollout.batch.provenance
        ]
        assert all(item.shaping_reward_amount == 0 for item in base_rollout.batch.provenance)
        assert any(item.shaping_reward_amount != 0 for item in shaped_rollout.batch.provenance)
        assert all(
            item.reward == pytest.approx(item.base_reward_amount + item.shaping_reward_amount)
            for item in shaped_rollout.batch.provenance
        )
    finally:
        base.close()
        shaped.close()


@pytest.mark.parametrize("side", ["red", "blue"])
def test_v31_v32_vector_sides(tmp_path, side) -> None:
    runner, _ = _vector(tmp_path, num_envs=2, side=side)
    try:
        collection = runner.collect_rollout()
        assert {item.side for item in collection.batch.provenance} == {side}
        assert {item.episode.side for item in collection.episodes} == {side}
    finally:
        runner.close()


def test_v33_v34_d7_vector_evaluation_matches_sequential_by_seed(tmp_path) -> None:
    vector, _ = _vector(tmp_path / "vector", num_envs=2)
    try:
        before_rng = vector.core.policy_sampling_generator.get_state().clone()
        report = vector.evaluate(2)
        assert torch.equal(before_rng, vector.core.policy_sampling_generator.get_state())
        assert tuple(item.episode.environment_seed for item in report.episodes) == tuple(
            sorted(item.episode.environment_seed for item in report.episodes)
        )
        observed = {
            item.episode.environment_seed: (
                item.episode.action_ids, item.episode.reward_record_hashes,
                item.episode.outcome,
            )
            for item in report.episodes
        }
    finally:
        vector.close()
    sequential = {}
    for env_index in range(2):
        ppo, base = _authorities(env_index=env_index)
        factory = EnvironmentFactory()
        runner = NativePPORunner(
            _env(factory, tmp_path / f"single-{env_index}", env_index),
            NativeMaskedPPO(ppo), config=base, run_namespace="ppo-vector-test",
        )
        try:
            item = runner.evaluate(1).episodes[0]
            sequential[item.environment_seed] = (
                item.action_ids, item.reward_record_hashes, item.outcome,
            )
        finally:
            runner.env.close()
    assert observed == sequential


def test_v35_v36_v37_v38_replay_and_zero_error_metrics(tmp_path) -> None:
    runner, factory = _vector(
        tmp_path, num_envs=2, rollout_length=2, max_steps=1, artifacts=True
    )
    try:
        collection = runner.collect_rollout()
        assert collection.invalid_action_attempts == (0, 0)
        assert collection.stale_action_rejections == (0, 0)
        for env_index, segment in enumerate(collection.vector_rollout.segments):
            replay_path = (
                tmp_path / "run/envs" / f"{env_index:03d}"
                / "training/000000/replay.zip"
            )
            assert replay_source_archive(factory.scenario, replay_path, strict=True).matches
            transition = segment[0].transition
            joint = runner.envs[env_index].reward_records[0]
            side = joint.side_record("red")
            assert transition.reward_record_hash == side.semantic_hash
            assert transition.action_id in side.transition_identity.submitted_action_ids
            assert transition.reward == side.total_reward
    finally:
        runner.close()


def test_v13_stale_vector_binding_fails_whole_rollout(tmp_path, monkeypatch) -> None:
    runner, _ = _vector(tmp_path, num_envs=2, rollout_length=2)
    original = runner.envs[0].step_bound_action
    def stale(index, binding):
        original(index, binding)
        raise NativeTrainingError("bound_action_snapshot_stale")
    monkeypatch.setattr(runner.envs[0], "step_bound_action", stale)
    try:
        with pytest.raises(PPOVectorRunnerError, match="stale"):
            runner.collect_rollout()
        assert runner.stale_action_rejections[0] == 1
    finally:
        runner.close()


def test_d1_d2_d3_d4_d5_d8_same_short_vector_run_reproduces(tmp_path) -> None:
    observed = []
    for index in range(2):
        runner, _ = _vector(tmp_path / str(index), num_envs=2)
        try:
            collection, metrics, _seconds = runner.train_update()
            observed.append((
                tuple(item.action_id for item in collection.batch.provenance),
                tuple(item.reward_record_hash for item in collection.batch.provenance),
                metrics, runner.core.parameter_content_hash(),
                tuple(item.episode.environment_seed for item in collection.episodes),
            ))
        finally:
            runner.close()
    assert observed[0] == observed[1]
    assert len(set(observed[0][-1])) == 2


def test_vector_checkpoint_provenance_and_evaluation_load(tmp_path) -> None:
    source, _ = _vector(tmp_path / "source", num_envs=2)
    try:
        source.collect_rollout()
        checkpoint = source.save_checkpoint(tmp_path / "checkpoint")
        expected_parameters = source.core.parameter_content_hash()
    finally:
        source.close()
    restored, _ = _vector(tmp_path / "restored", num_envs=2)
    try:
        identity = restored.load_checkpoint_for_evaluation(checkpoint)
        assert restored.core.parameter_content_hash() == expected_parameters
        report = restored.evaluate(2, checkpoint_identity=identity)
        assert report.checkpoint_identity == identity
        assert (checkpoint / "manifest.json").is_file()
        assert (checkpoint / "state.pt").is_file()
        manifest = json.loads((checkpoint / "manifest.json").read_text())
        assert manifest["exact_vector_training_resume"] is False
        assert manifest["vector_config_hash"] == restored.config.content_hash
    finally:
        restored.close()


def test_vector_checkpoint_config_mismatch_rejected(tmp_path) -> None:
    source, _ = _vector(tmp_path / "source", num_envs=1)
    try:
        source.collect_rollout()
        checkpoint = source.save_checkpoint(tmp_path / "checkpoint")
    finally:
        source.close()
    restored, _ = _vector(tmp_path / "restored", num_envs=2)
    try:
        with pytest.raises(PPOCheckpointError):
            restored.load_checkpoint_for_evaluation(checkpoint)
    finally:
        restored.close()


def test_vector_constructor_rejects_shared_mutable_opponent(tmp_path) -> None:
    ppo, base = _authorities()
    factory = EnvironmentFactory()
    opponent = NoOpAgent("native-no-op-fixed-opponent-v1")
    envs = [
        _env(factory, tmp_path, index, opponent=opponent) for index in range(2)
    ]
    with pytest.raises(PPOVectorRunnerError, match="opponent"):
        NativeSynchronousVectorPPORunner(
            envs, NativeMaskedPPO(ppo),
            vector_config=_vector_config(ppo, base, num_envs=2),
            base_runner_config=base,
        )
    for env in envs:
        env.close()
