from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from wargame_native_engine.analysis import EightFactorAnalyzer, GreenFrameAnalysisInput
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.training import (
    EightFactorShapingError,
    NativeMultiSeatEnv,
    RewardContractError,
    RewardTruncationSource,
    TrainingTruncationProvenance,
    build_composite_reward_policy,
    build_eight_factor_shaping_policy,
    build_shared_training_discount_config,
    build_joint_training_reward_record,
    build_reward_transition_identity,
    compose_joint_training_reward,
    derive_eight_factor_potential,
    derive_joint_eight_factor_shaping,
    load_eight_factor_shaping_policy,
    load_reward_policy,
    load_shared_training_discount_config,
    potential_from_frame_analysis,
    verify_composite_reward_sequence,
)
from wargame_native_engine.training.discount import TrainingDiscountBasis
import wargame_native_engine.training.multi_seat as multi_seat_module
from wargame_native_engine.agents import SeatDecision


SCENARIO = Path("examples/source_movement_scenario.json")


def _unit(obj_id: int, color: int, *, health: float = 100.0) -> dict[str, object]:
    return {
        "obj_id": obj_id,
        "color": color,
        "value": 100.0,
        "blood": health,
        "max_blood": 100.0,
        "carry_weapon_ids": [36],
        "remain_bullet_nums": {"36": 10},
        "weapon_cool_time": 0,
        "weapon_unfold_state": 2,
        "move_state": 0,
        "stop": False,
        "can_to_move": True,
        "on_board": False,
        "see_enemy_bop_ids": [],
        "keep": False,
        "tire": 0,
        "lose_control": False,
        "close_combat": False,
    }


def _analysis(frame_index: int, *, red_health=100.0, blue_health=100.0):
    frame = {
        "operators": [
            _unit(1, 0, health=red_health),
            _unit(2, 1, health=blue_health),
        ],
        "passengers": [],
    }
    return EightFactorAnalyzer().analyze_green_frame(GreenFrameAnalysisInput(
        frame_index=frame_index,
        frame=frame,
        frame_hash=canonical_hash({"frame": frame_index, "payload": frame}),
        scenario_hash=canonical_hash({"scenario": "shaping-v1"}),
    ))


def _identities(label: str = "shaping"):
    action_hash = canonical_hash({"joint": label})
    result = {}
    for side in ("red", "blue"):
        result[side] = build_reward_transition_identity(
            episode_id="shaping-episode",
            run_namespace="shaping-tests",
            side=side,
            decision_window_index=0,
            frame_before=0,
            fact_frame_start=1,
            fact_frame_end=1,
            frame_after=1,
            sim_time_before_ms=0,
            sim_time_after_ms=1_000,
            decision_before_hash=canonical_hash({"before": side}),
            action_catalog_hash=canonical_hash({"catalog": side}),
            submitted_action_ids=(),
            submitted_action_semantic_hash=canonical_hash({"action": side}),
            joint_action_hash=action_hash,
            decision_after_hash=canonical_hash({"after": side}),
        )
    return result


def _base(
    *, terminated=False, truncated=False, winner=None, reason=None,
    provenance=None,
):
    identities = _identities()
    return build_joint_training_reward_record(
        red_transition_identity=identities["red"],
        blue_transition_identity=identities["blue"],
        terminated=terminated,
        truncated=truncated,
        winner=winner,
        terminal_reason_code=reason,
        truncation=(provenance if provenance is not None else (
            TrainingTruncationProvenance.native_max_ticks()
            if truncated else TrainingTruncationProvenance.none()
        )),
        policy=load_reward_policy(),
    )


def _authorities(lambda_value=0.03):
    discount = load_shared_training_discount_config()
    policy = build_eight_factor_shaping_policy(
        lambda_value=lambda_value, discount=discount
    )
    composite = build_composite_reward_policy(load_reward_policy(), policy)
    return discount, policy, composite


def test_s1_s2_s3_s5_s6_s7_s25_s27_formula_symmetry_and_bound() -> None:
    discount, policy, _ = _authorities()
    before = _analysis(0)
    improved = _analysis(1, red_health=100.0, blue_health=50.0)
    first = derive_joint_eight_factor_shaping(
        base_record=_base(), analysis_before=before, analysis_after=improved,
        policy=policy, discount=discount,
    )
    repeated = derive_joint_eight_factor_shaping(
        base_record=_base(), analysis_before=before, analysis_after=improved,
        policy=policy, discount=discount,
    )
    assert first == repeated
    assert first.potential_before.phi_blue == -first.potential_before.phi_red
    assert first.red_amount > 0.0
    assert first.blue_amount == -first.red_amount
    assert abs(first.red_amount) <= 0.03 * 1.99 + 1e-15

    worse = derive_joint_eight_factor_shaping(
        base_record=_base(),
        analysis_before=_analysis(0, red_health=100.0, blue_health=50.0),
        analysis_after=_analysis(1), policy=policy, discount=discount,
    )
    assert worse.red_amount < 0.0
    unchanged = derive_joint_eight_factor_shaping(
        base_record=_base(),
        analysis_before=_analysis(0, red_health=100.0, blue_health=50.0),
        analysis_after=_analysis(1, red_health=100.0, blue_health=50.0),
        policy=policy, discount=discount,
    )
    phi = unchanged.potential_before.phi_red
    assert unchanged.red_amount == pytest.approx(
        policy.lambda_value * (0.99 - 1.0) * phi, abs=1e-18
    )
    extremal = derive_joint_eight_factor_shaping(
        base_record=_base(),
        analysis_before=_analysis(0, red_health=0.0),
        analysis_after=_analysis(1, blue_health=0.0),
        policy=policy,
        discount=discount,
    )
    assert extremal.red_amount == pytest.approx(0.0597)


def test_s4_s24_s30_s31_i1_i5_base_only_and_lambda_zero() -> None:
    base_policy = load_reward_policy()
    expected_hash = "5653b7653eb46d3f091d5d0c5c04ab7dd5ce380236a4d2455214ab735a39b0cf"
    assert base_policy.content_hash == expected_hash
    base = _base()
    disabled = build_composite_reward_policy(base_policy, None)
    assert not disabled.shaping_enabled
    assert len(base.red.components) == len(base.blue.components) == 1

    discount, policy, composite = _authorities(0.0)
    shaping = derive_joint_eight_factor_shaping(
        base_record=base, analysis_before=_analysis(0),
        analysis_after=_analysis(1, blue_health=50.0), policy=policy,
        discount=discount,
    )
    result = compose_joint_training_reward(
        base_record=base, shaping=shaping, composite_policy=composite
    )
    assert result.red.total_reward == base.red.total_reward
    assert result.blue.total_reward == base.blue.total_reward
    assert len(result.red.components) == 2
    assert load_reward_policy().content_hash == expected_hash


@pytest.mark.parametrize(("winner", "phi", "sign"), (("red", 0.5, -1), ("blue", -0.5, 1)))
def test_s8_s9_s10_terminal_uses_absorbing_zero(winner, phi, sign) -> None:
    discount, policy, _ = _authorities()
    before = _analysis(0, red_health=100.0, blue_health=50.0)
    assert potential_from_frame_analysis(before).phi_red > 0
    base = _base(terminated=True, winner=winner, reason="side_eliminated")
    shaping = derive_joint_eight_factor_shaping(
        base_record=base, analysis_before=before, analysis_after=None,
        policy=policy, discount=discount,
    )
    assert shaping.terminal_override
    assert shaping.phi_after_effective == 0.0
    assert shaping.red_amount == -policy.lambda_value * shaping.potential_before.phi_red


def test_s11_s12_truncation_analyzes_committed_post_state() -> None:
    discount, policy, _ = _authorities()
    shaping = derive_joint_eight_factor_shaping(
        base_record=_base(truncated=True, reason="max_ticks"),
        analysis_before=_analysis(0),
        analysis_after=_analysis(1, blue_health=50.0),
        policy=policy, discount=discount,
    )
    assert not shaping.terminal_override
    assert shaping.potential_after is not None
    training = derive_joint_eight_factor_shaping(
        base_record=_base(
            truncated=True,
            provenance=TrainingTruncationProvenance.training_limit(
                RewardTruncationSource.TRAINING_MAX_DECISION_WINDOWS,
                1,
            ),
        ),
        analysis_before=_analysis(0),
        analysis_after=_analysis(1, blue_health=50.0),
        policy=policy,
        discount=discount,
    )
    assert training.phi_after_effective == shaping.phi_after_effective


def test_s13_s14_s15_zero_force_is_bounded_without_epsilon() -> None:
    assert potential_from_frame_analysis(_analysis(0, red_health=0.0)).phi_red == -1.0
    assert potential_from_frame_analysis(_analysis(0, blue_health=0.0)).phi_red == 1.0
    assert potential_from_frame_analysis(
        _analysis(0, red_health=0.0, blue_health=0.0)
    ).phi_red == 0.0


def test_s16_s17_s23_s28_missing_coverage_provenance_and_nonfinite_fail_closed() -> None:
    broken = deepcopy(_analysis(0))
    broken = type(broken)(
        broken.provenance,
        broken.unit_analyses,
        (replace_side := type(broken.side_analyses[0])(
            side="red", unit_ids=(1,), unit_count=1, observed_unit_count=0,
            effective_power=None, coverage=0.0, confidence=None,
            warnings=("no_valid_units",),
        ), broken.side_analyses[1]),
        broken.advantage,
        broken.warnings,
    )
    assert replace_side.coverage == 0.0
    with pytest.raises(EightFactorShapingError, match="coverage"):
        potential_from_frame_analysis(broken)
    with pytest.raises((TypeError, ValueError)):
        build_eight_factor_shaping_policy(
            lambda_value=float("nan"),
            discount=load_shared_training_discount_config(),
        )


def test_s21_s22_s29_potential_record_contains_no_mutable_or_future_authority() -> None:
    frame = {
        "operators": [_unit(1, 0), _unit(2, 1)], "passengers": []
    }
    original = deepcopy(frame)
    potential = derive_eight_factor_potential(GreenFrameAnalysisInput(
        0, frame, canonical_hash(frame), canonical_hash({"scenario": 1})
    ))
    assert frame == original
    payload = str(potential.to_dict()).lower()
    for forbidden in ("winner", "future", "world", "observation", "action_mask"):
        assert forbidden not in payload


def test_s32_i2_i3_i4_i6_production_authority_hashes_are_deterministic() -> None:
    discount = load_shared_training_discount_config()
    policy = load_eight_factor_shaping_policy(discount=discount)
    composite = build_composite_reward_policy(load_reward_policy(), policy)
    assert discount.gamma == 0.99
    assert policy.lambda_value == 0.03
    assert discount.content_hash == "f35a748f9d0232506135b7e2438fa64f5bcc601a7aeb1fc631b182f396e4f06e"
    assert policy.content_hash == "bc21e92be00f9f9243b436741321f7d326f51a8bb734386a6de880dad37d7e81"
    assert composite == build_composite_reward_policy(load_reward_policy(), policy)
    alternate_discount = build_shared_training_discount_config(
        config_id=discount.config_id,
        config_version="gamma-probe",
        gamma=0.9,
        basis=TrainingDiscountBasis.PER_DECISION_TRANSITION,
    )
    alternate_policy = build_eight_factor_shaping_policy(
        lambda_value=0.03, discount=alternate_discount
    )
    assert alternate_discount.content_hash != discount.content_hash
    assert alternate_policy.content_hash != policy.content_hash
    production_amount = derive_joint_eight_factor_shaping(
        base_record=_base(), analysis_before=_analysis(0),
        analysis_after=_analysis(1, blue_health=50.0), policy=policy,
        discount=discount,
    ).red_amount
    alternate_amount = derive_joint_eight_factor_shaping(
        base_record=_base(), analysis_before=_analysis(0),
        analysis_after=_analysis(1, blue_health=50.0), policy=alternate_policy,
        discount=alternate_discount,
    ).red_amount
    assert alternate_amount != production_amount
    assert load_reward_policy().content_hash == (
        "5653b7653eb46d3f091d5d0c5c04ab7dd5ce380236a4d2455214ab735a39b0cf"
    )


def test_s10_s18_s25_s26_draw_snapshot_side_swap_and_parallel_determinism() -> None:
    discount, policy, composite = _authorities()
    before = _analysis(0, red_health=100.0, blue_health=50.0)
    after = _analysis(1, red_health=50.0, blue_health=100.0)
    draw = _base(terminated=True, winner=None, reason="mutual_elimination")
    terminal = derive_joint_eight_factor_shaping(
        base_record=draw, analysis_before=before, analysis_after=None,
        policy=policy, discount=discount,
    )
    assert terminal.red_amount == -policy.lambda_value * (
        potential_from_frame_analysis(before).phi_red
    )
    base = _base()

    def derive_once():
        shaping = derive_joint_eight_factor_shaping(
            base_record=base,
            analysis_before=deepcopy(before),
            analysis_after=deepcopy(after),
            policy=policy,
            discount=discount,
        )
        return compose_joint_training_reward(
            base_record=base, shaping=shaping, composite_policy=composite
        )

    restored = derive_once()
    assert restored == derive_once()
    with ThreadPoolExecutor(max_workers=4) as pool:
        parallel = tuple(pool.map(lambda _index: derive_once(), range(8)))
    assert all(value == restored for value in parallel)
    assert potential_from_frame_analysis(after).phi_red == pytest.approx(
        -potential_from_frame_analysis(before).phi_red
    )


class _MatchFactory:
    def __init__(self, root: Path) -> None:
        self.root = root

    def __call__(self, seed: int, episode_sequence: int):
        bundle = ScenarioBundle.from_path(SCENARIO)
        facade = WargameFacade(
            ScenarioCatalog.from_bundles({bundle.scenario_identity.scenario_id: bundle}),
            storage_root=self.root / str(episode_sequence),
        )
        return facade.create_match(
            scenario_id=bundle.scenario_identity.scenario_id,
            red_controller_id="shape-red", blue_controller_id="shape-blue",
            seed=seed, decision_interval_ms=1_000, frame_interval_ms=1_000,
        )


def test_s4_disabled_mode_never_enters_shaping_calculator(monkeypatch, tmp_path) -> None:
    def forbidden(**_kwargs):
        raise AssertionError("disabled shaping entered calculator")

    monkeypatch.setattr(
        multi_seat_module, "derive_joint_eight_factor_shaping", forbidden
    )
    env = NativeMultiSeatEnv(
        _MatchFactory(tmp_path), run_namespace="shaping-disabled",
        max_decision_windows=1, enable_eight_factor_shaping=False,
    )
    try:
        env.reset(42)
        result = env.step(SeatDecision(), SeatDecision())
        assert len(result.joint_reward_record.red.components) == 1
        assert result.joint_reward_record.composite_policy is None
    finally:
        env.close()


def test_s18_s20_s26_i8_i9_i10_i11_i12_multiseat_integration(monkeypatch, tmp_path) -> None:
    env = NativeMultiSeatEnv(
        _MatchFactory(tmp_path), run_namespace="shaping-integration",
        max_decision_windows=1, enable_eight_factor_shaping=True,
    )
    try:
        env.reset(42)
        analyses = {0: _analysis(0), 1: _analysis(1, blue_health=50.0)}
        manager = env.match.gateway.manager
        monkeypatch.setattr(manager, "frame_analysis", lambda _sid, index: analyses[index])
        result = env.step(SeatDecision(), SeatDecision())
        record = result.joint_reward_record
        assert len(record.red.components) == len(record.blue.components) == 2
        provenance = dict(record.red.components[1].provenance)
        assert provenance["phi_before"] == 0.0
        assert provenance["phi_after_effective"] > 0.0
        assert provenance["terminal_override"] is False
        assert provenance["discount_config_hash"] == (
            load_shared_training_discount_config().content_hash
        )
        assert record.red.total_reward + record.blue.total_reward == 0.0
        assert record.red.transition_identity.same_joint_transition(
            record.blue.transition_identity
        )
        assert result.transitions[0].reward_record == record.red
        assert result.transitions[1].reward_record == record.blue
        session = env._session  # noqa: SLF001 - contract identity probe
        assert session is not None
        replayed = verify_composite_reward_sequence(
            result.transitions,
            frame_analyses=analyses,
            terminal=False,
            truncated=True,
            winner=None,
            terminal_reason_code=None,
            base_policy=session.base_reward_policy,
            shaping_policy=session.shaping_policy,
            discount=session.discount_config,
            composite_policy=session.composite_reward_policy,
        )
        assert replayed.matches
    finally:
        env.close()
