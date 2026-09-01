from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import multiprocessing
import os
from pathlib import Path
import time

import pytest

from scripts.run_minimal_source_replay import _template
from wargame_native_engine.agents import NoOpAgent
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import load_scenario, scenario_from_dict
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.training import NativeTrainingEnv
from wargame_native_engine.training.ppo_config import build_ppo_algorithm_config
from wargame_native_engine.training.ppo_core import NativeMaskedPPO, torch
from wargame_native_engine.training.ppo_parallel_protocol import (
    PPO_PARALLEL_WORKER_PROTOCOL_HASH,
    PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION,
    CoordinatorActionCommand,
    PPOParallelProtocolError,
    build_worker_decision_envelope,
)
from wargame_native_engine.training.ppo_parallel_runner import (
    NativeParallelPPORunner, PPOParallelRunnerError, PPOParallelWorkerTimeout,
)
from wargame_native_engine.training.ppo_parallel_runner_config import (
    build_ppo_parallel_runner_config, load_ppo_parallel_runner_config,
)
from wargame_native_engine.training.ppo_runner_config import build_ppo_runner_config
from wargame_native_engine.training.ppo_vector_runner import NativeSynchronousVectorPPORunner
from wargame_native_engine.training.ppo_vector_runner_config import build_ppo_vector_runner_config


SCENARIO_PATH = "examples/source_movement_scenario.json"
PPO_PATH = Path("config/training/native_masked_ppo_v1.json")
RUNNER_PATH = Path("config/training/native_ppo_runner_v1.json")
VECTOR_PATH = Path("config/training/native_ppo_vector_runner_v1.json")
PARALLEL_PATH = Path("config/training/native_ppo_parallel_runner_v1.json")


@dataclass(frozen=True)
class SpawnNativeEnvFactory:
    storage_root: str
    side: str = "red"
    composite: bool = False
    max_steps: int = 2

    def __call__(self):
        scenario = load_scenario(SCENARIO_PATH)
        template = _template(scenario)
        if self.composite:
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
                document, source_hash="parallel-ppo-complete-analysis"
            )
        environment_factory = lambda: SourceCompatibleEnvironment(  # child-local only
            GameSession(NativeEngineBackend(scenario)), template=template
        )
        return NativeTrainingEnv(
            environment_factory, controlled_side=self.side,
            opponent=NoOpAgent("native-no-op-fixed-opponent-v1"),
            storage_root=Path(self.storage_root), max_actions=256,
            max_steps=self.max_steps, delta_time_seconds=20,
            enable_eight_factor_shaping=self.composite,
        )


@dataclass(frozen=True)
class FailingSpawnFactory:
    def __call__(self):
        raise RuntimeError("intentional-worker-factory-failure")


@dataclass(frozen=True)
class WrongSideFactory:
    storage_root: str

    def __call__(self):
        return SpawnNativeEnvFactory(self.storage_root, side="blue")()


@dataclass(frozen=True)
class TerminalSpawnFactory:
    storage_root: str

    def __call__(self):
        document = json.loads(Path(SCENARIO_PATH).read_text())
        loser = next(item for item in document["operators"] if item["side"] == "blue")
        loser.update({"health": 0, "blood": 0, "alive": False})
        scenario = scenario_from_dict(document)
        template = _template(scenario)
        environment_factory = lambda: SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(scenario)), template=template
        )
        return NativeTrainingEnv(
            environment_factory, controlled_side="red",
            opponent=NoOpAgent("native-no-op-fixed-opponent-v1"),
            storage_root=Path(self.storage_root), max_actions=256,
            max_steps=1, delta_time_seconds=20,
        )


def _authorities(*, num_envs=2, rollout=4, side="red", composite=False,
                 timeout=10.0):
    ppo_doc = json.loads(PPO_PATH.read_text())
    ppo_doc["rollout_length"] = rollout
    ppo_doc["minibatch_size"] = min(2, rollout)
    ppo_doc["update_epochs"] = 1
    ppo_doc["network"]["hidden_sizes"] = [8, 8]
    ppo = build_ppo_algorithm_config(ppo_doc)
    base_doc = json.loads(RUNNER_PATH.read_text())
    base_doc["purpose"] = "TEST_SMOKE"
    base_doc["trained_side"] = side.upper()
    base_doc["reward_mode"] = "COMPOSITE_EIGHT_FACTOR" if composite else "BASE_ONLY"
    base_doc["ppo_config_ref"]["hash"] = ppo.content_hash
    base_doc["checkpoint_every_updates"] = None
    base_doc["run_limits"] = {"max_updates": 1, "max_environment_steps": None, "max_episodes": None}
    base = build_ppo_runner_config(base_doc, ppo=ppo)
    vector_doc = json.loads(VECTOR_PATH.read_text())
    vector_doc["purpose"] = "TEST_SMOKE"
    vector_doc["num_envs"] = num_envs
    vector_doc["trained_side"] = side.upper()
    vector_doc["reward_mode"] = base.reward_mode.value
    vector_doc["base_runner_config_ref"]["hash"] = base.content_hash
    vector_doc["ppo_config_ref"]["hash"] = ppo.content_hash
    vector_doc["rollout_target"]["transitions_per_update"] = rollout
    vector = build_ppo_vector_runner_config(vector_doc, base_runner=base, ppo=ppo)
    parallel_doc = json.loads(PARALLEL_PATH.read_text())
    parallel_doc["purpose"] = "TEST_SMOKE"
    parallel_doc["num_envs"] = parallel_doc["num_workers"] = num_envs
    parallel_doc["trained_side"] = side.upper()
    parallel_doc["reward_mode"] = base.reward_mode.value
    parallel_doc["base_vector_runner_ref"]["hash"] = vector.content_hash
    parallel_doc["ppo_config_ref"]["hash"] = ppo.content_hash
    parallel_doc["operational"]["worker_timeout_seconds"] = timeout
    parallel = build_ppo_parallel_runner_config(
        parallel_doc, vector=vector, ppo=ppo
    )
    return ppo, base, vector, parallel


def _parallel(tmp_path, *, num_envs=2, rollout=4, side="red", composite=False,
              max_steps=2, timeout=10.0, artifacts=False):
    ppo, base, vector, parallel = _authorities(
        num_envs=num_envs, rollout=rollout, side=side,
        composite=composite, timeout=timeout,
    )
    factories = [
        SpawnNativeEnvFactory(
            str(tmp_path / f"worker-{index}"), side, composite, max_steps
        ) for index in range(num_envs)
    ]
    runner = NativeParallelPPORunner(
        factories, NativeMaskedPPO(ppo), parallel_config=parallel,
        vector_config=vector, base_runner_config=base,
        run_namespace="parallel-ppo-test",
        artifact_root=tmp_path / "artifacts" if artifacts else None,
    )
    return runner, load_scenario(SCENARIO_PATH)


def _sync(tmp_path, *, num_envs=2, rollout=4, side="red", composite=False,
          max_steps=2):
    ppo, base, vector, _parallel_config = _authorities(
        num_envs=num_envs, rollout=rollout, side=side, composite=composite
    )
    envs = [SpawnNativeEnvFactory(
        str(tmp_path / f"sync-{index}"), side, composite, max_steps
    )() for index in range(num_envs)]
    return NativeSynchronousVectorPPORunner(
        envs, NativeMaskedPPO(ppo), vector_config=vector,
        base_runner_config=base, run_namespace="parallel-ppo-test",
    )


def test_parallel_production_config_protocol_and_operational_timeout() -> None:
    schema = json.loads(Path(
        "config/schemas/native_ppo_parallel_runner_config.schema.json"
    ).read_text())
    document = json.loads(PARALLEL_PATH.read_text())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(document)
    assert schema["properties"]["execution_mode"]["const"] == document["execution_mode"]
    config = load_ppo_parallel_runner_config()
    assert config.content_hash == "f2539a9ab2d284fe8228d87b8412a6857e81468bc182fe542754c3152a3be3ce"
    assert config.start_method == "SPAWN"
    assert config.num_envs == config.num_workers == 4
    assert config.worker_protocol.identity_hash == PPO_PARALLEL_WORKER_PROTOCOL_HASH
    ppo, base, vector, first = _authorities(timeout=1.0)
    _ppo, _base, _vector, second = _authorities(timeout=99.0)
    assert first.content_hash == second.content_hash
    assert first.worker_timeout_seconds != second.worker_timeout_seconds


def test_post_truncation_bootstrap_observation_may_have_no_action() -> None:
    fields = dict(
        protocol_version=PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION,
        run_id="run", worker_index=0, env_index=0, request_id=1,
        episode_index=0, side="red", decision_window_index=1,
        frame_index=1, sim_time_ms=1000, decision_hash="a" * 64,
        catalog_hash="b" * 64, action_encoder_hash="c" * 64,
        preprocessor_hash="d" * 64, observation=(0.0,) * 8302,
        action_mask=(False,) * 256, learner_tensor_hash="e" * 64,
        scenario_id="scenario", post_truncation_observation=True,
    )
    value = build_worker_decision_envelope(**fields)
    assert value.post_truncation_observation is True
    with pytest.raises(PPOParallelProtocolError, match="active worker mask"):
        build_worker_decision_envelope(
            **{**fields, "post_truncation_observation": False}
        )


def test_pe46_pe48_protocol_hash_and_detached_payload_contract() -> None:
    from dataclasses import fields
    from wargame_native_engine.training.ppo_parallel_protocol import (
        WorkerDecisionEnvelope, WorkerTransitionResult,
    )

    forbidden = {
        "world", "world_state", "green", "frame_analysis", "controller_view",
        "action_catalog", "engine", "session_manager",
    }
    names = {
        item.name.lower()
        for record in (WorkerDecisionEnvelope, WorkerTransitionResult)
        for item in fields(record)
    }
    assert names.isdisjoint(forbidden)
    assert PPO_PARALLEL_WORKER_PROTOCOL_HASH == (
        "fa0094258b5b41b1df0001071f073d36ae66a697d3e84d7a884f8b56c0463c32"
    )


def test_pe1_pe2_pe4_pe5_pe6_pe47_spawn_ready_isolated(tmp_path) -> None:
    baseline = {item.pid for item in multiprocessing.active_children()}
    runner, _scenario = _parallel(tmp_path)
    try:
        ready = runner.start()
        assert len(ready) == 2
        assert len(set(runner.worker_pids)) == 2
        assert all(item.pid != os.getpid() for item in ready)
        assert len({item.session_identity for item in ready}) == 2
        assert all(type(item.torch_imported) is bool for item in ready)
        assert all(item.runtime_authority == ready[0].runtime_authority for item in ready)
        assert all(item.preprocessor_hash == "8f0520cd00d03d4346146f22b4cc8a35290d5868b1281419e4a5baa500ef1554" for item in ready)
    finally:
        pids = set(runner.worker_pids)
        runner.close()
    assert not pids & {item.pid for item in multiprocessing.active_children()}
    assert baseline <= {item.pid for item in multiprocessing.active_children()} | baseline


def test_pe8_pe9_pe12_pe16_pe19_pe24_pe25_parallel_rollout(tmp_path) -> None:
    runner, _ = _parallel(tmp_path, num_envs=2, rollout=4)
    shapes = []
    hook = runner.core.model.register_forward_pre_hook(
        lambda _module, args: shapes.append(tuple(args[0].shape))
    )
    try:
        collection = runner.collect_rollout()
        assert collection.vector_rollout.total_transitions == 4
        assert len(collection.batch.provenance) == 4
        assert shapes[:2] == [(2, 8302), (2, 8302)]
        assert all(item.action_mask[item.action_index] for item in collection.batch.provenance)
        assert all(item.action_id for item in collection.batch.provenance)
        assert collection.payload.observation_bytes > 0
        assert collection.payload.mask_bytes > 0
        assert collection.payload.transition_bytes > 0
        assert collection.invalid_action_attempts == (0, 0)
        assert collection.stale_action_attempts == (0, 0)
    finally:
        hook.remove()
        runner.close()


def test_pe21_pe22_pe23_pe26_pe27_truncation_reset_and_gae(tmp_path) -> None:
    runner, _ = _parallel(tmp_path, num_envs=2, rollout=4, max_steps=1)
    try:
        collection = runner.collect_rollout()
        assert len(collection.episodes) == 4
        for segment in collection.vector_rollout.segments:
            assert len(segment) == 2
            first, second = (item.transition for item in segment)
            assert first.truncated and first.bootstrap_allowed and not first.trace_continues
            assert first.next_decision_identity != second.decision_identity
            expected = runner.core.predict_value(torch.tensor(first.next_observation, dtype=torch.float32))
            assert first.next_value_prediction == pytest.approx(float(expected.item()))
        assert collection.batch.advantages.shape == (4,)
    finally:
        runner.close()


def test_rollout_boundary_resets_workers_that_all_completed_on_final_round(
    tmp_path: Path,
) -> None:
    runner, _ = _parallel(tmp_path, num_envs=2, rollout=2, max_steps=1)
    try:
        first = runner.collect_rollout(2)
        assert first.vector_rollout.total_transitions == 2
        assert all(not state.active for state in runner.states)
        assert all(
            item.transition.truncated
            for segment in first.vector_rollout.segments for item in segment
        )

        second = runner.collect_rollout(2)
        assert second.vector_rollout.total_transitions == 2
        assert {item.episode.episode_index for item in second.episodes} == {1}
        assert all(
            item.transition.truncated
            for segment in second.vector_rollout.segments for item in segment
        )
        assert all(item.transition.next_decision_identity for segment in first.vector_rollout.segments for item in segment)
    finally:
        runner.close()


def test_pe20_terminal_worker_transition_bootstraps_zero(tmp_path) -> None:
    ppo, base, vector, config = _authorities(num_envs=1, rollout=1)
    runner = NativeParallelPPORunner(
        [TerminalSpawnFactory(str(tmp_path / "terminal"))],
        NativeMaskedPPO(ppo), parallel_config=config,
        vector_config=vector, base_runner_config=base,
        run_namespace="parallel-terminal-test",
    )
    try:
        collection = runner.collect_rollout()
        transition = collection.batch.provenance[0]
        assert transition.terminated and not transition.truncated
        assert not transition.bootstrap_allowed
        assert not transition.trace_continues
        assert transition.next_value_prediction == 0.0
        assert collection.episodes[0].episode.outcome == "WIN"
    finally:
        runner.close()


def test_pe28_pe29_pe30_real_update_one_model_optimizer(tmp_path) -> None:
    runner, _ = _parallel(tmp_path)
    before = runner.core.parameter_content_hash()
    try:
        collection, metrics, _update = runner.train_update()
        assert metrics.update_step == 1
        assert runner.core.parameter_content_hash() != before
        assert {item.policy_update_step for segment in collection.vector_rollout.segments for item in segment} == {0}
        assert runner.core.optimizer is not None
    finally:
        runner.close()


@pytest.mark.parametrize("side", ["red", "blue"])
def test_pe33_pe34_parallel_sides(tmp_path, side) -> None:
    runner, _ = _parallel(tmp_path, side=side)
    try:
        collection = runner.collect_rollout()
        assert {item.side for item in collection.batch.provenance} == {side}
    finally:
        runner.close()


def test_pe31_pe32_base_composite_and_no_observation_leak(tmp_path) -> None:
    base, _ = _parallel(tmp_path / "base", composite=False)
    shaped, _ = _parallel(tmp_path / "shaped", composite=True)
    try:
        first = base.collect_rollout()
        second = shaped.collect_rollout()
        assert torch.equal(first.batch.observations, second.batch.observations)
        assert torch.equal(first.batch.action_masks, second.batch.action_masks)
        assert [item.action_id for item in first.batch.provenance] == [item.action_id for item in second.batch.provenance]
        assert all(item.shaping_reward_amount == 0 for item in first.batch.provenance)
        assert any(item.shaping_reward_amount != 0 for item in second.batch.provenance)
        assert all(item.reward_authority["shaping"]["analyzer"]["model_hash"] == "77e0113c896c813033528d43bc6c132e296cb91eacf9a153d436cb9fe20bc577" for item in shaped.ready)
        assert "frameanalysis" not in str(second.batch.provenance).lower()
    finally:
        base.close(); shaped.close()


@pytest.mark.parametrize("num_envs", [1, 2])
def test_pe35_pe36_pd9_parallel_sync_differential(tmp_path, num_envs) -> None:
    rollout = num_envs * 2
    parallel, _ = _parallel(tmp_path / "parallel", num_envs=num_envs, rollout=rollout)
    sync = _sync(tmp_path / "sync", num_envs=num_envs, rollout=rollout)
    try:
        first, first_metrics, _ = parallel.train_update()
        second, second_metrics, _ = sync.train_update()
        assert [item.action_id for item in first.batch.provenance] == [item.action_id for item in second.batch.provenance]
        assert [item.reward for item in first.batch.provenance] == [item.reward for item in second.batch.provenance]
        assert torch.equal(first.batch.observations, second.batch.observations)
        assert first_metrics == second_metrics
        assert parallel.core.parameter_content_hash() == sync.core.parameter_content_hash()
    finally:
        parallel.close(); sync.close()


def test_pe18_pe37_pd6_delay_changes_timing_not_semantics(tmp_path) -> None:
    observed = []
    for index, delays in enumerate(({}, {0: 0.1})):
        runner, _ = _parallel(tmp_path / str(index))
        try:
            collection, metrics, _ = runner.train_update(artificial_delays=delays)
            observed.append((
                [item.action_id for item in collection.batch.provenance],
                [item.reward for item in collection.batch.provenance], metrics,
                runner.core.parameter_content_hash(), collection,
            ))
        finally:
            runner.close()
    assert observed[0][:4] == observed[1][:4]
    assert observed[1][4].timing.overlapping_rounds > 0
    assert observed[1][4].completion_orders != ()


def test_pe38_pd8_parallel_evaluation_deterministic(tmp_path) -> None:
    reports = []
    for index in range(2):
        runner, _ = _parallel(tmp_path / str(index))
        try:
            reports.append(runner.evaluate(2))
        finally:
            runner.close()
    assert reports[0].seed_set_hash == reports[1].seed_set_hash
    assert [item.episode.action_ids for item in reports[0].episodes] == [item.episode.action_ids for item in reports[1].episodes]
    assert reports[0].semantic_hash == reports[1].semantic_hash


def test_pe39_pe40_pe41_worker_replay(tmp_path) -> None:
    runner, scenario = _parallel(
        tmp_path, num_envs=2, rollout=2, max_steps=1, artifacts=True
    )
    try:
        collection = runner.collect_rollout()
        for episode in collection.episodes:
            replay = Path(tmp_path / "artifacts" / f"env-{episode.env_index:03d}" / "episode-000000/replay.zip")
            assert replay_source_archive(scenario, replay, strict=True).matches
            transition = collection.vector_rollout.segments[episode.env_index][0].transition
            assert transition.reward_record_hash in episode.episode.reward_record_hashes
            assert transition.action_id in episode.episode.action_ids
    finally:
        runner.close()


def test_pe13_pe14_wrong_and_duplicate_token_fail_closed(tmp_path) -> None:
    runner, _ = _parallel(tmp_path, num_envs=1, rollout=2)
    runner.start()
    try:
        decision = runner.states[0].decision
        assert decision is not None
        bad = CoordinatorActionCommand(
            PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION, runner.run_id,
            0, 0, decision.request_id, decision.episode_index,
            "0" * 64, decision.action_mask.index(True), 0.0,
        )
        runner.clients[0].send(bad)
        with pytest.raises(PPOParallelRunnerError, match="BINDING_TOKEN_MISMATCH"):
            runner.clients[0].receive()
    finally:
        runner.close()

    stale, _ = _parallel(tmp_path / "stale", num_envs=1, rollout=2)
    stale.start()
    try:
        decision = stale.states[0].decision
        index = decision.action_mask.index(True)
        stale.clients[0].send(CoordinatorActionCommand(
            PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION, stale.run_id,
            0, 0, decision.request_id + 1, decision.episode_index,
            decision.binding_token, index, 0.0,
        ))
        with pytest.raises(PPOParallelRunnerError, match="STALE_ACTION_COMMAND"):
            stale.clients[0].receive()
    finally:
        stale.close()

    duplicate, _ = _parallel(tmp_path / "duplicate", num_envs=1, rollout=2)
    duplicate.start()
    try:
        decision = duplicate.states[0].decision
        index = decision.action_mask.index(True)
        command = CoordinatorActionCommand(
            PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION, duplicate.run_id,
            0, 0, decision.request_id, decision.episode_index,
            decision.binding_token, index, 0.0,
        )
        duplicate.clients[0].send(command)
        duplicate.clients[0].receive()
        duplicate.clients[0].send(command)
        with pytest.raises(PPOParallelRunnerError, match="DUPLICATE_ACTION_COMMAND"):
            duplicate.clients[0].receive()
    finally:
        duplicate.close()


def test_pe3_pe42_pe44_mismatch_and_factory_failure_cleanup(tmp_path) -> None:
    before = {item.pid for item in multiprocessing.active_children()}
    ppo, base, vector, config = _authorities(num_envs=1, rollout=2)
    for factory in (
        WrongSideFactory(str(tmp_path / "wrong")), FailingSpawnFactory(),
    ):
        runner = NativeParallelPPORunner(
            [factory], NativeMaskedPPO(ppo), parallel_config=config,
            vector_config=vector, base_runner_config=base,
        )
        with pytest.raises(PPOParallelRunnerError):
            runner.start()
        assert not runner.clients
    after = {item.pid for item in multiprocessing.active_children()}
    assert not (after - before)


def test_pe43_pe45_worker_timeout_cleans_process(tmp_path) -> None:
    runner, _ = _parallel(
        tmp_path, num_envs=1, rollout=2, timeout=0.05
    )
    with pytest.raises(PPOParallelWorkerTimeout):
        runner.collect_rollout(1, artificial_delays={0: 0.2})
    assert not runner.clients
    assert not any(
        child.name.startswith("native-ppo-env-")
        for child in multiprocessing.active_children()
    )


def test_pd1_pd2_pd3_pd4_pd5_pd7_pd10_reproducible_process_runs(tmp_path) -> None:
    values = []
    for index in range(2):
        runner, _ = _parallel(tmp_path / str(index))
        try:
            collection, metrics, _ = runner.train_update()
            values.append((
                tuple(item.action_id for item in collection.batch.provenance),
                tuple(item.reward for item in collection.batch.provenance),
                collection.vector_rollout.semantic_hash, metrics,
                runner.core.parameter_content_hash(), runner.config.content_hash,
                tuple(item.pid for item in runner.ready),
            ))
        finally:
            runner.close()
    assert values[0][:-1] == values[1][:-1]
    assert values[0][-1] != values[1][-1]


@pytest.mark.parametrize("num_envs,transitions", [(1, 16), (2, 32), (4, 64)])
def test_parallel_operational_probe(tmp_path, num_envs, transitions) -> None:
    runner, _ = _parallel(
        tmp_path, num_envs=num_envs, rollout=transitions, max_steps=2,
        timeout=30.0,
    )
    started = time.perf_counter()
    try:
        collection, _metrics, update_seconds = runner.train_update()
        elapsed = time.perf_counter() - started
        assert collection.vector_rollout.total_transitions == transitions
        assert runner.core.update_step == 1
        assert sum(collection.invalid_action_attempts) == 0
        assert sum(collection.stale_action_attempts) == 0
        report = {
            "envs": num_envs,
            "transitions": transitions,
            "wall": elapsed,
            "rollout": collection.timing.total_seconds,
            "engine_critical": collection.timing.engine_critical_path_seconds,
            "engine_sum": collection.timing.engine_worker_sum_seconds,
            "preprocessing_sum": collection.timing.preprocessing_worker_sum_seconds,
            "policy": collection.timing.policy_seconds,
            "ipc": collection.timing.ipc_estimated_seconds,
            "reward_replay_sum": collection.timing.reward_replay_worker_sum_seconds,
            "update": update_seconds,
            "transitions_per_second": transitions / collection.timing.total_seconds,
            "overlap_rounds": collection.timing.overlapping_rounds,
            "observation_bytes": collection.payload.observation_bytes,
            "mask_bytes": collection.payload.mask_bytes,
            "command_bytes": collection.payload.command_bytes,
            "transition_bytes": collection.payload.transition_bytes,
        }
        print("PARALLEL_OPERATIONAL_PROBE=" + json.dumps(report, sort_keys=True))
    finally:
        runner.close()
