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
    NativeMaskedPPO,
    PPOCheckpointError,
    torch,
)
from wargame_native_engine.training.ppo_runner import (
    LiveActionBinding,
    NativePPORunner,
    PPORunnerError,
)
from wargame_native_engine.training.ppo_runner_config import (
    PPORewardMode,
    build_ppo_runner_config,
    load_ppo_runner_config,
)


SCENARIO_PATH = Path("examples/source_movement_scenario.json")
PPO_CONFIG_PATH = Path("config/training/native_masked_ppo_v1.json")
RUNNER_CONFIG_PATH = Path("config/training/native_ppo_runner_v1.json")


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
                    "value": 100.0,
                    "can_to_move": True,
                    "see_enemy_bop_ids": [],
                    "keep": False,
                    "tire": 0,
                    "lose_control": False,
                })
            template = SourceFrameImporter().from_dict(
                document, source_hash="ppo-analyzer-complete-fixture"
            )
        return SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(self.scenario)), template=template
        )


def _smoke_ppo():
    value = json.loads(PPO_CONFIG_PATH.read_text(encoding="utf-8"))
    value["rollout_length"] = 2
    value["minibatch_size"] = 2
    value["update_epochs"] = 1
    value["network"]["hidden_sizes"] = [8, 8]
    return build_ppo_algorithm_config(value)


def _smoke_runner_config(ppo, *, side="red", composite=False):
    value = json.loads(RUNNER_CONFIG_PATH.read_text(encoding="utf-8"))
    value["purpose"] = "TEST_SMOKE"
    value["trained_side"] = side.upper()
    value["reward_mode"] = (
        "COMPOSITE_EIGHT_FACTOR" if composite else "BASE_ONLY"
    )
    value["ppo_config_ref"]["hash"] = ppo.content_hash
    value["run_limits"] = {
        "max_updates": 1,
        "max_environment_steps": None,
        "max_episodes": None,
    }
    return build_ppo_runner_config(value, ppo=ppo)


def _runner(tmp_path, *, side="red", composite=False, artifacts=False):
    ppo = _smoke_ppo()
    factory = EnvironmentFactory(complete_analysis=composite)
    env = NativeTrainingEnv(
        factory,
        controlled_side=side,
        opponent=NoOpAgent("native-no-op-fixed-opponent-v1"),
        storage_root=tmp_path / "facts",
        max_actions=256,
        max_steps=2,
        delta_time_seconds=20,
        enable_eight_factor_shaping=composite,
    )
    core = NativeMaskedPPO(ppo)
    runner = NativePPORunner(
        env,
        core,
        config=_smoke_runner_config(ppo, side=side, composite=composite),
        run_namespace="ppo-runner-test",
        artifact_root=tmp_path / "run" if artifacts else None,
    )
    return runner, factory


def test_runner_production_authority_and_explicit_reward_mode() -> None:
    value = load_ppo_runner_config()
    assert value.content_hash == (
        "e024f1d0c0db62ec87f042bdc6d284ec224bb777025f2c838025e8d2c3194141"
    )
    assert value.reward_mode is PPORewardMode.BASE_ONLY
    assert value.trained_side == "RED"
    assert value.opponent_kind == "NO_OP_AGENT"
    assert value.run_limits.max_updates == 100


def test_r1_r2_r3_r5_r6_live_decision_catalog_reward_binding(tmp_path) -> None:
    runner, _factory = _runner(tmp_path)
    try:
        binding = runner.reset_episode()
        assert binding.observation.decision_window_index == 0
        assert binding.action_space.mask == runner.env.valid_action_mask
        assert len(binding.action_space.mask) == 256
        rollout = runner.collect_rollout(2)
        assert len(rollout.batch.provenance) == 2
        for item in rollout.batch.provenance:
            assert item.action_id
            assert item.action_mask[item.action_index]
            assert item.catalog_hash == item.catalog_hash.lower()
            assert item.reward == pytest.approx(
                item.base_reward_amount + item.shaping_reward_amount
            )
            assert item.next_decision_identity
        assert rollout.invalid_action_attempts == 0
        assert rollout.stale_action_rejections == 0
    finally:
        runner.env.close()


def test_r4_l7_l8_stale_catalog_fails_before_reinterpretation(tmp_path) -> None:
    runner, _factory = _runner(tmp_path)
    try:
        runner.reset_episode()
        old = runner.env.current_decision_binding
        index = old.action_space.mask.index(True)
        selected = LiveActionBinding.select(old, index)
        assert selected.action_id == old.action_space.action_ids[index]
        runner.env.step_bound_action(index, old)
        with pytest.raises(NativeTrainingError, match="bound_action_snapshot_stale"):
            runner.env.step_bound_action(index, old)
    finally:
        runner.env.close()


@pytest.mark.parametrize("side", ["red", "blue"])
def test_r7_r8_r9_r10_r11_r12_r13_bootstrap_rollout_and_sides(
    tmp_path, side
) -> None:
    runner, _factory = _runner(tmp_path, side=side)
    try:
        rollout = runner.collect_rollout(2)
        first, last = rollout.batch.provenance
        assert first.trace_continues
        assert not first.terminated and not first.truncated
        assert last.truncated and last.bootstrap_allowed
        assert not last.trace_continues
        assert last.next_value_prediction == pytest.approx(
            float(rollout.batch.next_values[-1])
        )
        assert rollout.episodes[0].side == side
        assert rollout.episodes[0].outcome == "TRUNCATED"
        assert runner.episode_index == 1
        first_seed = rollout.episodes[0].environment_seed
        runner.reset_episode()
        assert runner._episode_seed != first_seed
    finally:
        runner.env.close()


def test_r9_rollout_cut_is_not_environment_truncation(tmp_path) -> None:
    ppo = _smoke_ppo()
    env = NativeTrainingEnv(
        EnvironmentFactory(), controlled_side="red",
        opponent=NoOpAgent("native-no-op-fixed-opponent-v1"),
        storage_root=tmp_path / "facts", max_actions=256, max_steps=5,
    )
    runner = NativePPORunner(
        env, NativeMaskedPPO(ppo), config=_smoke_runner_config(ppo)
    )
    try:
        rollout = runner.collect_rollout(2)
        final = rollout.batch.provenance[-1]
        assert not final.terminated and not final.truncated
        assert final.bootstrap_allowed and not final.trace_continues
        assert runner._active
        assert runner.env.current_decision_binding.action_space.decision_window_index == 2
    finally:
        env.close()


def test_s1_r16_r18_r19_r25_r26_base_real_env_update(tmp_path) -> None:
    runner, _factory = _runner(tmp_path, artifacts=True)
    before = runner.core.parameter_content_hash()
    try:
        rollout, metrics = runner.train_update()
        episode = rollout.episodes[0]
        assert episode.base_return == episode.composite_return == 0.0
        assert episode.shaping_return == 0.0
        assert episode.reward_rederivation_verified
        assert all(item.shaping_reward_amount == 0.0 for item in rollout.batch.provenance)
        assert metrics.update_step == 1
        assert math_is_finite_metrics(metrics)
        assert runner.core.parameter_content_hash() != before
        assert (tmp_path / "run/run_manifest.json").is_file()
        assert (tmp_path / "run/episode_metrics.jsonl").is_file()
    finally:
        runner.env.close()


def test_s2_r17_r18_r19_composite_real_env_update(tmp_path) -> None:
    runner, _factory = _runner(tmp_path, composite=True)
    try:
        rollout, metrics = runner.train_update()
        episode = rollout.episodes[0]
        assert metrics.update_step == 1
        assert all(
            len(runner.env.reward_records[index].red.components) == 2
            for index in range(len(runner.env.reward_records))
        )
        assert any(item.shaping_reward_amount != 0.0 for item in rollout.batch.provenance)
        assert episode.composite_return == pytest.approx(
            episode.base_return + episode.shaping_return
        )
        assert episode.reward_rederivation_verified
        assert runner.reward_identity.policy_id == "native-base-plus-eight-factor-potential"
    finally:
        runner.env.close()


def test_base_and_composite_inputs_masks_actions_are_identical(tmp_path) -> None:
    base, _ = _runner(tmp_path / "base")
    shaped, _ = _runner(tmp_path / "shaped", composite=True)
    try:
        first = base.collect_rollout(2)
        second = shaped.collect_rollout(2)
        assert torch.equal(first.batch.observations, second.batch.observations)
        assert torch.equal(first.batch.action_masks, second.batch.action_masks)
        assert first.action_ids == second.action_ids
        assert [item.base_reward_amount for item in first.batch.provenance] == [
            item.base_reward_amount for item in second.batch.provenance
        ]
        assert first.batch.provenance[0].preprocessor_hash == second.batch.provenance[0].preprocessor_hash
    finally:
        base.env.close()
        shaped.env.close()


def test_r20_r21_metrics_use_engine_winner_and_truncation_not_loss(tmp_path) -> None:
    runner, _ = _runner(tmp_path)
    try:
        episode = runner.collect_rollout(2).episodes[0]
        assert episode.winner is None
        assert episode.outcome == "TRUNCATED"
        assert episode.terminal_reason is None
    finally:
        runner.env.close()


def test_s3_r22_r23_r24_r38_deterministic_evaluation(tmp_path) -> None:
    runner, _ = _runner(tmp_path / "source")
    try:
        runner.train_update()
        checkpoint = runner.save_checkpoint(tmp_path / "checkpoint")
    finally:
        runner.env.close()
    reports = []
    for index in range(2):
        restored, _ = _runner(tmp_path / f"restored-{index}")
        try:
            restored.load_checkpoint(checkpoint)
            before = restored.core.parameter_content_hash()
            update = restored.core.update_step
            reports.append(restored.evaluate(1, checkpoint_identity="fixture"))
            assert restored.core.parameter_content_hash() == before
            assert restored.core.update_step == update
        finally:
            restored.env.close()
    assert reports[0].action_sequences == reports[1].action_sequences
    assert reports[0].seed_set_hash == reports[1].seed_set_hash
    assert reports[0].semantic_hash == reports[1].semantic_hash


def test_r27_r28_checkpoint_file_and_rng_continuation(tmp_path) -> None:
    runner, _ = _runner(tmp_path / "source")
    try:
        runner.train_update()
        checkpoint = runner.save_checkpoint(tmp_path / "checkpoint")
        mask = torch.ones(256, dtype=torch.bool)
        observation = torch.zeros(8302, dtype=torch.float32)
        expected = runner.core.act(observation, mask).action_index.item()
    finally:
        runner.env.close()
    restored, _ = _runner(tmp_path / "restored")
    try:
        restored.load_checkpoint(checkpoint)
        actual = restored.core.act(observation, mask).action_index.item()
        assert actual == expected
        assert (checkpoint / "manifest.json").is_file()
        assert (checkpoint / "state.pt").is_file()
    finally:
        restored.env.close()


def test_r29_r30_r31_r32_r33_r34_resume_mismatch_fails_closed(tmp_path) -> None:
    runner, _ = _runner(tmp_path / "source")
    try:
        runner.train_update()
        checkpoint = runner.save_checkpoint(tmp_path / "checkpoint")
    finally:
        runner.env.close()
    manifest_path = checkpoint / "manifest.json"
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    for field in (
        "runner_config_hash", "reward_identity", "reward_authority", "seed_identity",
        "runtime_authority", "state_hash",
    ):
        value = deepcopy(original)
        value[field] = "mismatch" if field.endswith("hash") else {}
        semantic = dict(value)
        semantic.pop("semantic_hash")
        value["semantic_hash"] = canonical_hash_for_test(semantic)
        manifest_path.write_text(json.dumps(value), encoding="utf-8")
        restored, _ = _runner(tmp_path / f"bad-{field}")
        try:
            with pytest.raises(PPOCheckpointError):
                restored.load_checkpoint(checkpoint)
        finally:
            restored.env.close()
        manifest_path.write_text(json.dumps(original), encoding="utf-8")


def test_r35_r37_real_smoke_replay_and_action_reward_join(tmp_path) -> None:
    runner, factory = _runner(tmp_path, artifacts=True)
    try:
        rollout, _metrics = runner.train_update()
        replay_path = tmp_path / "run/episodes/000000/replay.zip"
        verification = replay_source_archive(factory.scenario, replay_path, strict=True)
        assert verification.matches
        records = runner.env.reward_records
        assert tuple(item.action_id for item in rollout.batch.provenance) == rollout.action_ids
        assert tuple(item.side_record("red").semantic_hash for item in records) == tuple(
            item.reward_record_hash for item in rollout.batch.provenance
        )
        for transition, joint in zip(rollout.batch.provenance, records):
            side = joint.side_record("red")
            assert transition.action_id in side.transition_identity.submitted_action_ids
            assert transition.reward == side.total_reward
    finally:
        runner.env.close()


def test_runner_tensor_path_has_no_green_or_world_payload(tmp_path) -> None:
    runner, _ = _runner(tmp_path)
    try:
        rollout = runner.collect_rollout(2)
        text = str(rollout.batch.provenance).lower()
        assert "worldstate" not in text
        assert "frameanalysis" not in text
        assert "controller_view" not in text
    finally:
        runner.env.close()


def test_run_limits_stop_and_publish_summary(tmp_path) -> None:
    runner, _ = _runner(tmp_path, artifacts=True)
    try:
        summary = runner.train()
        assert summary.updates == 1
        assert summary.environment_steps == 2
        assert summary.episodes == 1
        assert summary.transitions_per_second > 0.0
        assert (tmp_path / "run/run_summary.json").is_file()
        assert (tmp_path / "run/checkpoints/update-000001/manifest.json").is_file()
    finally:
        runner.env.close()


def test_same_short_cpu_run_reproduces_actions_metrics_and_parameters(tmp_path) -> None:
    observed = []
    for index in range(2):
        runner, _ = _runner(tmp_path / str(index))
        try:
            rollout, metrics = runner.train_update()
            observed.append((
                rollout.action_ids,
                metrics.policy_loss,
                metrics.value_loss,
                metrics.entropy,
                runner.core.parameter_content_hash(),
            ))
        finally:
            runner.env.close()
    assert observed[0] == observed[1]


def math_is_finite_metrics(value) -> bool:
    import math
    return all(math.isfinite(getattr(value, name)) for name in (
        "policy_loss", "value_loss", "entropy", "total_loss",
        "approx_kl", "clip_fraction", "grad_norm", "clipped_grad_norm",
    ))


def canonical_hash_for_test(value) -> str:
    from wargame_native_engine.io.serialization import canonical_hash
    return canonical_hash(value)
