from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from types import MappingProxyType

import pytest

from wargame_native_engine.analysis import (
    ANALYZER_MODEL_HASH,
    ANALYZER_MODEL_ID,
    ANALYZER_MODEL_VERSION,
    ANALYZER_SEMANTIC_SCHEMA_VERSION,
    DEFAULT_ANALYZER_AUTHORITY,
    EightFactorAnalyzer,
    GreenFrameAnalysisInput,
    AnalyzerAuthorityMetadata,
    analyzer_semantic_hash,
    analyzer_semantic_spec,
    frame_analysis_from_record,
    frame_analysis_to_record,
    require_analyzer_authority,
)
from wargame_native_engine.analysis.semantic_identity import (
    FACTOR_FORMULA_IDS,
    FACTOR_SOURCE_FIELDS,
    MOBILITY_STATE,
    WEAPON_WEIGHT,
)
from wargame_native_engine.training.discount import (
    TRAINING_DISCOUNT_CONFIG_SCHEMA_VERSION,
    SharedTrainingDiscountConfig,
    TrainingDiscountBasis,
    TrainingDiscountContractError,
    TrainingDiscountIdentity,
    build_shared_training_discount_config,
    load_shared_training_discount_config,
    require_discount_identity,
)
from wargame_native_engine.training.eight_factor_shaping_contract import (
    EIGHT_FACTOR_SHAPING_POLICY_SCHEMA_VERSION,
    EightFactorShapingContractError,
    build_eight_factor_shaping_policy,
    require_shaping_prerequisites,
)


def _unit(obj_id: int, color: int) -> dict[str, object]:
    return {
        "obj_id": obj_id,
        "color": color,
        "value": 100.0,
        "blood": 100.0,
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


def _frame() -> dict[str, object]:
    return {
        "operators": [_unit(2, 1), _unit(1, 0)],
        "passengers": [],
    }


def _discount(
    *,
    gamma: float = 0.99,
    basis: TrainingDiscountBasis = (
        TrainingDiscountBasis.PER_DECISION_TRANSITION
    ),
    reference_interval_ms: int | None = None,
) -> SharedTrainingDiscountConfig:
    return build_shared_training_discount_config(
        config_id="test-shared-training-discount",
        config_version="fixture-1",
        gamma=gamma,
        basis=basis,
        reference_interval_ms=reference_interval_ms,
    )


def _policy(*, lambda_value: float = 0.03, discount=None):
    return build_eight_factor_shaping_policy(
        lambda_value=lambda_value,
        discount=_discount() if discount is None else discount,
    )


def _mutated_hash(path: tuple[str, ...], value: object) -> str:
    spec = analyzer_semantic_spec()
    target = spec
    for name in path[:-1]:
        target = target[name]  # type: ignore[assignment,index]
    target[path[-1]] = value  # type: ignore[index]
    return analyzer_semantic_hash(spec)


def test_as1_as2_same_spec_and_mapping_order_have_same_hash() -> None:
    first = analyzer_semantic_spec()
    second = dict(reversed(tuple(first.items())))

    assert analyzer_semantic_hash(first) == ANALYZER_MODEL_HASH
    assert analyzer_semantic_hash(second) == ANALYZER_MODEL_HASH
    assert analyzer_semantic_hash() == analyzer_semantic_hash()


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        (("factors", "base_value", "floor"), 2.0),
        (("factors", "health", "alive_floor"), 0.2),
        (("factors", "weapon", "weapon_weights", "36"), 9.0),
        (("factors", "ammo", "cap"), 1.5),
        (("factors", "readiness", "cooldown_floor"), 0.5),
        (("factors", "mobility", "state_weights", "0"), 0.8),
        (("factors", "information", "per_contact"), 0.1),
        (("factors", "status", "suppressed_multiplier"), 0.5),
        (("side_aggregation", "formula"), "MAX_VALID_UNIT"),
        (
            ("relative_advantage", "positive_total_formula"),
            "E_RED-E_BLUE",
        ),
    ),
)
def test_as3_through_as12_every_semantic_axis_changes_hash(
    path: tuple[str, ...], replacement: object,
) -> None:
    assert _mutated_hash(path, replacement) != ANALYZER_MODEL_HASH


def test_source_whitespace_comments_and_paths_are_not_hash_inputs() -> None:
    spec = analyzer_semantic_spec()
    compact = json.loads(json.dumps(spec, sort_keys=True, separators=(",", ":")))
    pretty = json.loads(json.dumps(spec, sort_keys=False, indent=4))

    assert "path" not in spec
    assert "comments" not in spec
    assert analyzer_semantic_hash(compact) == analyzer_semantic_hash(pretty)


def test_as8_runtime_tables_and_formula_ids_equal_semantic_spec() -> None:
    spec = analyzer_semantic_spec()

    assert spec["schema_version"] == ANALYZER_SEMANTIC_SCHEMA_VERSION
    assert spec["model_id"] == ANALYZER_MODEL_ID
    assert spec["model_version"] == ANALYZER_MODEL_VERSION
    assert spec["factors"]["weapon"]["weapon_weights"] == {
        str(key): value for key, value in WEAPON_WEIGHT.items()
    }
    assert spec["factors"]["mobility"]["state_weights"] == {
        str(key): value for key, value in MOBILITY_STATE.items()
    }
    for name, formula_id in FACTOR_FORMULA_IDS.items():
        assert spec["factors"][name]["formula_id"] == formula_id


def test_as13_default_frame_analysis_carries_canonical_identity() -> None:
    frame = EightFactorAnalyzer().analyze_green_frame(
        GreenFrameAnalysisInput(0, _frame(), "a" * 64, "b" * 64)
    )

    assert frame.provenance.model_id == DEFAULT_ANALYZER_AUTHORITY.model_id
    assert frame.provenance.model_version == DEFAULT_ANALYZER_AUTHORITY.model_version
    assert frame.provenance.model_hash == DEFAULT_ANALYZER_AUTHORITY.model_hash
    assert frame.provenance.model_hash == ANALYZER_MODEL_HASH


def test_as14_serialized_replay_identity_matches_runtime_authority() -> None:
    frame = EightFactorAnalyzer().analyze_green_frame(
        GreenFrameAnalysisInput(7, _frame(), "c" * 64, "d" * 64)
    )
    restored, events = frame_analysis_from_record(frame_analysis_to_record(frame))
    assert events == ()
    provenance = restored.provenance
    actual = AnalyzerAuthorityMetadata(
        analysis_schema_version=DEFAULT_ANALYZER_AUTHORITY.analysis_schema_version,
        model_id=provenance.model_id,
        model_version=provenance.model_version,
        model_hash=provenance.model_hash or "",
    )

    require_analyzer_authority(actual)
    with pytest.raises(ValueError, match="identity mismatch"):
        require_analyzer_authority(
            AnalyzerAuthorityMetadata(
                actual.analysis_schema_version,
                actual.model_id,
                actual.model_version,
                "f" * 64,
            )
        )


def test_as15_runtime_tables_are_immutable_and_export_is_detached() -> None:
    for value in (
        WEAPON_WEIGHT, MOBILITY_STATE, FACTOR_SOURCE_FIELDS, FACTOR_FORMULA_IDS
    ):
        assert isinstance(value, MappingProxyType)
        with pytest.raises(TypeError):
            value[next(iter(value))] = 123  # type: ignore[index]

    detached = analyzer_semantic_spec()
    detached["model_id"] = "mutated"
    assert analyzer_semantic_spec()["model_id"] == ANALYZER_MODEL_ID
    assert analyzer_semantic_hash() == ANALYZER_MODEL_HASH


def test_discount_schema_contract_is_explicit() -> None:
    schema = json.loads(Path(
        "config/schemas/native_training_discount_config.schema.json"
    ).read_text(encoding="utf-8"))

    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == (
        TRAINING_DISCOUNT_CONFIG_SCHEMA_VERSION
    )
    assert set(schema["properties"]["basis"]["enum"]) == {
        value.value for value in TrainingDiscountBasis
    }


def test_d1_same_config_and_loader_are_hash_stable(tmp_path: Path) -> None:
    first = _discount()
    second = _discount()
    source = tmp_path / "discount.json"
    source.write_text(json.dumps(first.semantic_payload(), indent=2), encoding="utf-8")

    assert first == second == load_shared_training_discount_config(source)
    assert first.content_hash == second.content_hash


def test_d2_gamma_change_changes_hash() -> None:
    assert _discount(gamma=0.9).content_hash != _discount(gamma=0.99).content_hash


def test_d3_basis_change_changes_hash() -> None:
    per_step = _discount()
    per_time = _discount(
        basis=TrainingDiscountBasis.PER_SIMULATION_TIME_REFERENCE,
        reference_interval_ms=1_000,
    )
    assert per_step.content_hash != per_time.content_hash


def test_d4_reference_interval_changes_hash_and_effective_gamma() -> None:
    first = _discount(
        basis=TrainingDiscountBasis.PER_SIMULATION_TIME_REFERENCE,
        reference_interval_ms=1_000,
    )
    second = _discount(
        basis=TrainingDiscountBasis.PER_SIMULATION_TIME_REFERENCE,
        reference_interval_ms=2_000,
    )

    assert first.content_hash != second.content_hash
    assert first.effective_gamma(delta_time_ms=2_000) == pytest.approx(0.99**2)
    assert second.effective_gamma(delta_time_ms=2_000) == pytest.approx(0.99)


@pytest.mark.parametrize("value", [True, False])
def test_d5_bool_gamma_rejected(value: bool) -> None:
    with pytest.raises(TypeError, match="not bool"):
        _discount(gamma=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_d6_nonfinite_gamma_rejected(value: float) -> None:
    with pytest.raises(TrainingDiscountContractError, match="finite"):
        _discount(gamma=value)


@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_d7_d8_out_of_range_gamma_rejected(value: float) -> None:
    with pytest.raises(TrainingDiscountContractError, match=r"\[0, 1\]"):
        _discount(gamma=value)


def test_gamma_endpoints_and_per_decision_semantics_are_valid() -> None:
    assert _discount(gamma=0.0).effective_gamma(delta_time_ms=1) == 0.0
    assert _discount(gamma=1.0).effective_gamma(delta_time_ms=99_999) == 1.0


def test_d9_shaping_references_exact_shared_discount_identity() -> None:
    discount = _discount()
    policy = _policy(discount=discount)

    assert policy.discount == discount.identity
    require_shaping_prerequisites(
        policy,
        discount=discount,
        analyzer=DEFAULT_ANALYZER_AUTHORITY,
    )


def test_d10_mismatched_discount_identity_fails_closed() -> None:
    discount = _discount()
    policy = _policy(discount=discount)
    other = _discount(gamma=0.9)

    with pytest.raises(TrainingDiscountContractError, match="identity mismatch"):
        require_discount_identity(other, policy.discount)
    with pytest.raises(TrainingDiscountContractError, match="identity mismatch"):
        require_shaping_prerequisites(
            policy,
            discount=other,
            analyzer=DEFAULT_ANALYZER_AUTHORITY,
        )


def test_p1_through_p13_fixed_policy_semantics_and_provenance() -> None:
    discount = _discount()
    policy = _policy(discount=discount)
    value = policy.to_dict()

    assert value["schema_version"] == EIGHT_FACTOR_SHAPING_POLICY_SCHEMA_VERSION
    assert value["potential"] == {
        "source": "ANALYZER_NORMALIZED_RELATIVE_ADVANTAGE",
        "canonical_side": "RED",
        "blue_projection": "NEGATE_CANONICAL_RED",
        "transform": "IDENTITY",
        "range": [-1.0, 1.0],
    }
    assert value["formula"] == (
        "LAMBDA_TIMES_GAMMA_PHI_NEXT_MINUS_PHI_BEFORE"
    )
    assert value["terminal_handling"] == "ABSORBING_PHI_NEXT_ZERO"
    assert value["truncation_handling"] == "ANALYZE_COMMITTED_POST_STATE"
    assert value["missing_analysis_policy"] == "FAIL_CLOSED"
    assert value["coverage_policy"] == "REQUIRE_COMPLETE_NONEMPTY_SIDES"
    assert value["confidence_policy"] == "IGNORE_UNAVAILABLE_CONFIDENCE_V1"
    assert value["future_information"] == "FORBIDDEN"
    assert value["reward_clipping"] == "NONE"
    assert value["reward_normalization"] == "NONE"
    assert value["analyzer"]["model_hash"] == ANALYZER_MODEL_HASH
    assert value["discount_reference"] == discount.identity.to_dict()
    assert "gamma" not in value


@pytest.mark.parametrize(
    "value",
    [True, False, math.nan, math.inf, -math.inf, -0.01],
)
def test_p14_lambda_validation(value: object) -> None:
    expected = TypeError if isinstance(value, bool) else EightFactorShapingContractError
    with pytest.raises(expected):
        _policy(lambda_value=value)  # type: ignore[arg-type]


def test_p15_policy_hash_is_deterministic_and_covers_lambda_and_authorities() -> None:
    first = _policy()
    repeated = _policy()
    different_lambda = _policy(lambda_value=0.1)
    different_discount = _policy(discount=_discount(gamma=0.9))

    assert first.content_hash == repeated.content_hash
    assert first.content_hash != different_lambda.content_hash
    assert first.content_hash != different_discount.content_hash
    assert len(first.content_hash) == 64


def test_shaping_analyzer_identity_mismatch_fails_closed() -> None:
    policy = _policy()
    different = AnalyzerAuthorityMetadata(
        DEFAULT_ANALYZER_AUTHORITY.analysis_schema_version,
        DEFAULT_ANALYZER_AUTHORITY.model_id,
        DEFAULT_ANALYZER_AUTHORITY.model_version,
        "e" * 64,
    )

    with pytest.raises(EightFactorShapingContractError, match="identity mismatch"):
        require_shaping_prerequisites(
            policy,
            discount=_discount(),
            analyzer=different,
        )


def test_shaping_schema_freezes_non_hyperparameter_fields() -> None:
    schema = json.loads(Path(
        "config/schemas/native_eight_factor_reward_shaping_policy.schema.json"
    ).read_text(encoding="utf-8"))

    assert schema["additionalProperties"] is False
    assert schema["properties"]["potential"]["properties"]["source"][
        "const"
    ] == "ANALYZER_NORMALIZED_RELATIVE_ADVANTAGE"
    assert schema["properties"]["terminal_handling"]["const"] == (
        "ABSORBING_PHI_NEXT_ZERO"
    )
    assert schema["properties"]["missing_analysis_policy"]["const"] == (
        "FAIL_CLOSED"
    )
