from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from wargame_native_engine.agents import SeatDecision, SeatOperation
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.data import DataProvenance, ScenarioBundle, ScenarioCatalog
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.training import (
    JOINT_TRAINING_REWARD_SCHEMA_VERSION,
    TRAINING_REWARD_SCHEMA_VERSION,
    NativeMultiSeatEnv,
    NativeMultiSeatError,
    RewardComponent,
    RewardContractError,
    RewardOutcome,
    RewardSide,
    RewardSupervisionSource,
    RewardTruncationSource,
    TrainingTruncationProvenance,
    build_joint_training_reward_record,
    build_reward_transition_identity,
    load_reward_policy,
    reward_sequence_hash,
    verify_reward_sequence,
)


SCENARIO = Path("examples/source_movement_scenario.json")


class MatchFactory:
    def __init__(self, root: Path, *, bundle: ScenarioBundle | None = None) -> None:
        self.root = root
        self.bundle = bundle or ScenarioBundle.from_path(SCENARIO)

    def __call__(self, seed: int, episode_sequence: int):
        app = WargameFacade(
            ScenarioCatalog.from_bundles({
                self.bundle.scenario_identity.scenario_id: self.bundle,
            }),
            storage_root=self.root / f"managed-{episode_sequence}",
        )
        return app.create_match(
            scenario_id=self.bundle.scenario_identity.scenario_id,
            red_controller_id="reward-red",
            blue_controller_id="reward-blue",
            seed=seed,
            decision_interval_ms=1_000,
            frame_interval_ms=1_000,
        )


def _terminal_bundle(winner: str = "red") -> ScenarioBundle:
    record = json.loads(SCENARIO.read_text(encoding="utf-8"))
    record["scenario_id"] = f"reward-terminal-{winner}"
    losing_side = "blue" if winner == "red" else "red"
    loser = next(item for item in record["operators"] if item["side"] == losing_side)
    loser.update({"health": 0, "blood": 0, "alive": False})
    return ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256=canonical_hash(record),
            source_kind="deterministic_test_derivative",
        ),
    )


def _identity(side: str, *, label: str = "base"):
    joint_hash = canonical_hash({"joint": label})
    return build_reward_transition_identity(
        episode_id="reward-episode",
        run_namespace="reward-tests",
        side=side,
        decision_window_index=0,
        frame_before=0,
        fact_frame_start=1,
        fact_frame_end=1,
        frame_after=1,
        sim_time_before_ms=0,
        sim_time_after_ms=1_000,
        decision_before_hash=canonical_hash({"before": side, "label": label}),
        action_catalog_hash=canonical_hash({"catalog": side, "label": label}),
        submitted_action_ids=(),
        submitted_action_semantic_hash=canonical_hash({"action": side}),
        joint_action_hash=joint_hash,
        decision_after_hash=canonical_hash({"after": side, "label": label}),
    )


def _joint(
    *,
    terminated: bool = False,
    truncated: bool = False,
    winner: str | None = None,
    reason: str | None = None,
    provenance: TrainingTruncationProvenance | None = None,
    label: str = "base",
):
    return build_joint_training_reward_record(
        red_transition_identity=_identity("red", label=label),
        blue_transition_identity=_identity("blue", label=label),
        terminated=terminated,
        truncated=truncated,
        winner=winner,
        terminal_reason_code=reason,
        truncation=provenance,
        policy=load_reward_policy(),
    )


def test_r1_r5_r10_r11_r15_policy_records_are_deterministic_and_zero_sum() -> None:
    policy = load_reward_policy()
    policy_document = json.loads(Path(
        "config/training/native_sparse_terminal_reward_v1.json"
    ).read_text(encoding="utf-8"))
    policy_schema = json.loads(Path(
        "config/schemas/native_training_reward_policy.schema.json"
    ).read_text(encoding="utf-8"))
    assert policy_schema["$schema"].endswith("draft/2020-12/schema")
    assert policy_schema["additionalProperties"] is False
    assert set(policy_schema["required"]) == set(policy_document)
    assert policy_schema["properties"]["policy_id"]["const"] == policy.policy_id
    with pytest.raises(TypeError):
        policy.mapping["nonterminal"] = (1.0, -1.0)  # type: ignore[index]
    first = _joint(terminated=True, winner="red", reason="side_eliminated")
    second = _joint(terminated=True, winner="red", reason="side_eliminated")
    blue_win = _joint(
        terminated=True, winner="blue", reason="side_eliminated", label="blue"
    )
    draw = _joint(
        terminated=True, winner=None, reason="mutual_elimination", label="draw"
    )

    assert first == second
    assert first.semantic_hash == second.semantic_hash
    assert first.schema_version == JOINT_TRAINING_REWARD_SCHEMA_VERSION
    assert first.red.schema_version == TRAINING_REWARD_SCHEMA_VERSION
    assert first.reward_policy == policy.identity
    assert (first.red.total_reward, first.blue.total_reward) == (1.0, -1.0)
    assert (blue_win.red.total_reward, blue_win.blue.total_reward) == (-1.0, 1.0)
    assert (draw.red.total_reward, draw.blue.total_reward) == (0.0, 0.0)
    for value in (first, blue_win, draw):
        assert value.red.total_reward + value.blue.total_reward == 0.0
        assert sum(item.amount for item in value.red.components) == value.red.total_reward
        assert sum(item.amount for item in value.blue.components) == value.blue.total_reward


def test_r6_native_and_training_truncation_have_distinct_provenance() -> None:
    native = _joint(
        truncated=True,
        reason="max_ticks",
        provenance=TrainingTruncationProvenance.native_max_ticks(),
        label="native",
    )
    training = _joint(
        truncated=True,
        provenance=TrainingTruncationProvenance.training_limit(
            RewardTruncationSource.TRAINING_MAX_DECISION_WINDOWS, 7
        ),
        label="training",
    )
    assert native.red.truncation.source is RewardTruncationSource.NATIVE_MAX_TICKS
    assert training.red.truncation.source is (
        RewardTruncationSource.TRAINING_MAX_DECISION_WINDOWS
    )
    assert training.red.truncation.config_hash is not None
    assert native.red.total_reward == training.red.total_reward == 0.0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True])
def test_r14_nonfinite_and_bool_components_fail_at_record_boundary(value) -> None:
    with pytest.raises((TypeError, RewardContractError)):
        RewardComponent(
            "base", "TERMINAL_OUTCOME", value,
            RewardSupervisionSource.ENGINE_COMMITTED_TRANSITION,
        )


def test_r12_r22_r23_presentation_and_eight_factor_are_excluded(
    tmp_path: Path,
) -> None:
    record = _joint().to_dict()
    text = json.dumps(record, sort_keys=True)
    assert "reason_text" not in text
    assert "EightFactor" not in text
    source = json.loads(Path(
        "config/training/native_sparse_terminal_reward_v1.json"
    ).read_text(encoding="utf-8"))
    source["shaping"]["enabled"] = True
    target = tmp_path / "unsupported-shaping.json"
    target.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(RewardContractError, match="shaping"):
        load_reward_policy(target)


def test_r7_r17_r18_r19_r20_joint_terminal_alignment_and_no_post_terminal_record(
    tmp_path: Path,
) -> None:
    environment = NativeMultiSeatEnv(
        MatchFactory(tmp_path, bundle=_terminal_bundle("red")),
        run_namespace="reward-terminal",
    )
    try:
        environment.reset(42)
        result = environment.step(SeatDecision(), SeatDecision())
        joint = result.joint_reward_record
        assert result.rewards == {"red": 1.0, "blue": -1.0}
        assert joint.winner is RewardSide.RED
        assert joint.red.terminal_reason_code == "side_eliminated"
        assert joint.red.transition_identity.same_joint_transition(
            joint.blue.transition_identity
        )
        for side in ("red", "blue"):
            identity = joint.side_record(side).transition_identity
            current = environment._session.controllers[side].observe()  # noqa: SLF001
            assert identity.frame_after == result.fact_frame_end == current.frame_index
            assert identity.sim_time_after_ms == result.sim_time_end_ms == current.sim_time_ms
            assert identity.decision_after_hash == current.decision_semantic.semantic_hash
        count = len(environment.reward_records)
        with pytest.raises(NativeMultiSeatError, match="ended"):
            environment.step(SeatDecision(), SeatDecision())
        assert len(environment.reward_records) == count
    finally:
        environment.close()


def test_r8_r9_r21_reward_rebuild_is_pure_and_policy_input_isolated(
    tmp_path: Path,
) -> None:
    environment = NativeMultiSeatEnv(
        MatchFactory(tmp_path), run_namespace="reward-purity",
        max_decision_windows=1,
    )
    try:
        reset = environment.reset(42)
        before_observations = deepcopy(reset.observations)
        before_masks = {
            side: tuple(value.mask) for side, value in reset.action_spaces.items()
        }
        result = environment.step(SeatDecision(), SeatDecision())
        stored = result.joint_reward_record
        rebuilt = build_joint_training_reward_record(
            red_transition_identity=stored.red.transition_identity,
            blue_transition_identity=stored.blue.transition_identity,
            terminated=stored.red.terminated,
            truncated=stored.red.truncated,
            winner=None,
            terminal_reason_code=None,
            truncation=stored.red.truncation,
        )
        assert rebuilt == stored
        assert reset.observations == before_observations
        assert {
            side: tuple(value.mask) for side, value in reset.action_spaces.items()
        } == before_masks
        policy_text = json.dumps(result.observations, sort_keys=True)
        for forbidden in (
            "reward", "winner", "TERMINAL_OUTCOME", "privileged", "EightFactor"
        ):
            assert forbidden not in policy_text
    finally:
        environment.close()


def test_r13_training_episode_return_is_sum_of_committed_records(tmp_path: Path) -> None:
    environment = NativeMultiSeatEnv(
        MatchFactory(tmp_path), run_namespace="reward-return",
        max_decision_windows=2,
    )
    try:
        environment.reset(42)
        environment.step(SeatDecision(), SeatDecision())
        environment.step(SeatDecision(), SeatDecision())
        expected = {
            side: sum(item.side_record(side).total_reward
                      for item in environment.reward_records)
            for side in ("red", "blue")
        }
        assert environment.episode_returns == expected == {"red": 0.0, "blue": 0.0}
    finally:
        environment.close()


def test_r17_explicit_wait_has_no_action_cost(tmp_path: Path) -> None:
    environment = NativeMultiSeatEnv(
        MatchFactory(tmp_path), run_namespace="reward-wait",
        max_decision_windows=2,
    )
    try:
        environment.reset(42)
        result = environment.step(
            SeatDecision.single(SeatOperation.wait()),
            SeatDecision.single(SeatOperation.wait()),
        )
        assert result.terminated is False
        assert result.truncated is False
        assert result.rewards == {"red": 0.0, "blue": 0.0}
        assert all(
            tuple((item.namespace, item.code, item.amount)
                  for item in record.components)
            == (("base", "TERMINAL_OUTCOME", 0.0),)
            for record in (
                result.joint_reward_record.red,
                result.joint_reward_record.blue,
            )
        )
    finally:
        environment.close()


def test_r16_rejected_action_creates_no_transition_or_reward(tmp_path: Path) -> None:
    environment = NativeMultiSeatEnv(
        MatchFactory(tmp_path), run_namespace="reward-reject",
    )
    try:
        environment.reset(42)
        with pytest.raises(NativeMultiSeatError, match="preview"):
            environment.step(
                SeatDecision.single(SeatOperation("STOP_MOVE", (1,))),
                SeatDecision(),
            )
        assert environment.transitions == ()
        assert environment.reward_records == ()
        assert environment.episode_returns == {"red": 0.0, "blue": 0.0}
    finally:
        environment.close()


def test_r2_snapshot_restore_same_transition_rederives_same_reward() -> None:
    record = json.loads(SCENARIO.read_text(encoding="utf-8"))
    record["scenario_id"] = "reward-snapshot-restore"
    loser = next(item for item in record["operators"] if item["side"] == "blue")
    loser.update({"health": 0, "blood": 0, "alive": False})
    scenario = scenario_from_dict(record)
    original = GameSession(NativeEngineBackend(scenario))
    original.reset(42)
    snapshot = deepcopy(original.snapshot())
    original.step_ms((), delta_time_ms=1_000)
    result_a = original.result()
    restored = GameSession(NativeEngineBackend(scenario))
    restored.reset(999)
    restored.restore(snapshot)
    restored.step_ms((), delta_time_ms=1_000)
    result_b = restored.result()
    assert result_a.to_dict() == result_b.to_dict()
    reward_a = _joint(
        terminated=result_a.terminal,
        winner=result_a.winner,
        reason=result_a.reason,
        label="snapshot",
    )
    reward_b = _joint(
        terminated=result_b.terminal,
        winner=result_b.winner,
        reason=result_b.reason,
        label="snapshot",
    )
    assert reward_a == reward_b
    assert reward_a.semantic_hash == reward_b.semantic_hash


def test_r3_r24_strict_replay_rederives_stored_reward_sequence(tmp_path: Path) -> None:
    environment = NativeMultiSeatEnv(
        MatchFactory(tmp_path / "managed"),
        run_namespace="reward-replay",
        max_decision_windows=1,
    )
    output = tmp_path / "episode"
    try:
        environment.reset(42)
        environment.step(SeatDecision(), SeatDecision())
        publication = environment.publish_training_package(
            output, summary_schema="test-reward-publication/1.0"
        )
        verification = verify_reward_sequence(
            environment.transitions,
            terminal=False,
            truncated=True,
            winner=None,
            terminal_reason_code=None,
        )
        assert publication.strict_replay_match
        assert publication.reward_replay_match
        assert verification.matches
        assert publication.reward_sequence_hash == reward_sequence_hash(
            environment.reward_records
        )
        summary = json.loads(publication.summary_path.read_text(encoding="utf-8"))
        assert summary["reward_replay_match"] is True
        assert summary["reward_policy"] == load_reward_policy().identity.to_dict()
    finally:
        environment.close()


def test_r24_canonical_hash_ignores_mapping_input_order() -> None:
    first = load_reward_policy()
    assert first.content_hash == load_reward_policy().content_hash
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})
    assert math.isfinite(_joint().red.total_reward)
