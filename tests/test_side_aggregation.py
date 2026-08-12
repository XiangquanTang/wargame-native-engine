from __future__ import annotations

from copy import deepcopy

import pytest

from wargame_native_engine.analysis.eight_factor_contract import (
    FACTOR_ORDER,
    FactorEvidence,
    FactorStatus,
)
from wargame_native_engine.analysis.green_frame_contract import (
    build_green_frame_analysis,
)
from wargame_native_engine.analysis.models import (
    FactorValue,
    UnitFactorAnalysis,
)
from wargame_native_engine.analysis.side_aggregation import (
    aggregate_side_analysis,
)


def _fv(name: str, value: float | None) -> FactorValue:
    return FactorValue(
        factor_name=name, value=value, status=FactorStatus.OBSERVED,
        source_paths=(f"unit.{name}",),
        formula_id="demo-compatible.test.v1",
        warnings=(), missing_fields=(),
    )


def _all_fvs(**values: float) -> tuple[FactorValue, ...]:
    return tuple(_fv(name, values[name]) for name in FACTOR_ORDER)


def _all_evidence() -> tuple[FactorEvidence, ...]:
    return tuple(
        FactorEvidence(name=name, raw_value=1.0,
                       source_paths=(f"unit.{name}",),
                       status=FactorStatus.OBSERVED, missing_fields=())
        for name in FACTOR_ORDER
    )


def _ua(operator_id: int, side: str, eff: float | None, *,
        valid: bool | None = None,
        evidence: tuple[FactorEvidence, ...] | None = None,
        ) -> UnitFactorAnalysis:
    if valid is None:
        valid = eff is not None
    return UnitFactorAnalysis(
        operator_id=operator_id, side=side,
        factor_evidence=evidence or _all_evidence(),
        factor_values=_all_fvs(**{k: 1.0 for k in FACTOR_ORDER}),
        effective_power=eff, valid=valid,
        observed_factor_count=8,
        warnings=() if valid else ("effective_power_unavailable",),
    )


# ── 1. Single red unit ───────────────────────────────────────────────


def test_single_red_unit():
    uas = (_ua(1, "red", 500.0),)
    result = aggregate_side_analysis(uas, side="red")
    assert result.effective_power == 500.0
    assert result.unit_ids == (1,)
    assert result.unit_count == 1
    assert result.coverage == 1.0
    assert result.warnings == ()


# ── 2. Multiple red units — sorted, summed ────────────────────────────


def test_multiple_red_units_sorted_sum():
    uas = (
        _ua(30, "red", 100.0),
        _ua(10, "red", 200.0),
        _ua(20, "red", 300.0),
    )
    result = aggregate_side_analysis(uas, side="red")
    assert result.unit_ids == (10, 20, 30)
    assert result.effective_power == 600.0
    assert result.unit_count == 3


# ── 3. Blue side independent ──────────────────────────────────────────


def test_blue_side_independent():
    uas = (
        _ua(1, "red", 100.0),
        _ua(2, "blue", 200.0),
        _ua(3, "red", 300.0),
        _ua(4, "blue", 400.0),
    )
    red = aggregate_side_analysis(uas, side="red")
    blue = aggregate_side_analysis(uas, side="blue")

    assert red.effective_power == 400.0
    assert red.unit_ids == (1, 3)
    assert blue.effective_power == 600.0
    assert blue.unit_ids == (2, 4)


# ── 4. Destroyed unit (eff=0) counted ─────────────────────────────────


def test_destroyed_unit_counted():
    uas = (
        _ua(1, "red", 500.0),
        _ua(2, "red", 0.0, valid=True),
    )
    result = aggregate_side_analysis(uas, side="red")
    assert result.effective_power == 500.0
    assert result.unit_count == 2
    assert result.coverage == 1.0


# ── 5. Partial UNKNOWN units skipped ──────────────────────────────────


def test_partial_unknown_skipped():
    evidence_partial = tuple(
        FactorEvidence(name=name, raw_value=None,
                       source_paths=(f"unit.{name}",),
                       status=FactorStatus.UNKNOWN, missing_fields=(name,))
        for name in FACTOR_ORDER
    )
    uas = (
        _ua(1, "red", 500.0),
        _ua(2, "red", None, valid=False, evidence=evidence_partial),
    )
    result = aggregate_side_analysis(uas, side="red")
    assert result.effective_power == 500.0
    assert result.unit_count == 2
    assert result.coverage == 0.5
    assert "unknown_units_skipped" in result.warnings


# ── 6. All UNKNOWN → effective_power=None, coverage=0.0 ───────────────


def test_all_unknown():
    evidence_partial = tuple(
        FactorEvidence(name=name, raw_value=None,
                       source_paths=(f"unit.{name}",),
                       status=FactorStatus.UNKNOWN, missing_fields=(name,))
        for name in FACTOR_ORDER
    )
    uas = (
        _ua(1, "red", None, valid=False, evidence=evidence_partial),
        _ua(2, "red", None, valid=False, evidence=evidence_partial),
    )
    result = aggregate_side_analysis(uas, side="red")
    assert result.effective_power is None
    assert result.coverage == 0.0
    assert result.unit_count == 2
    assert "no_valid_units" in result.warnings


# ── 7. Empty side → effective_power=0.0, coverage=None ───────────────


def test_empty_side():
    uas: tuple[UnitFactorAnalysis, ...] = ()
    result = aggregate_side_analysis(uas, side="red")
    assert result.effective_power == 0.0
    assert result.unit_count == 0
    assert result.observed_unit_count == 0
    assert result.coverage is None
    assert result.warnings == ()


def test_empty_side_with_other_side_units():
    uas = (_ua(1, "blue", 100.0),)
    result = aggregate_side_analysis(uas, side="red")
    assert result.effective_power == 0.0
    assert result.unit_count == 0
    assert result.coverage is None


# ── 8. Full coverage ──────────────────────────────────────────────────


def test_full_coverage():
    uas = (
        _ua(1, "red", 100.0),
        _ua(2, "red", 200.0),
    )
    result = aggregate_side_analysis(uas, side="red")
    assert result.coverage == 1.0


# ── 9. observed_unit_count ────────────────────────────────────────────


def test_observed_unit_count():
    evidence_partial = tuple(
        FactorEvidence(name=name, raw_value=None,
                       source_paths=(f"unit.{name}",),
                       status=FactorStatus.UNKNOWN, missing_fields=(name,))
        for name in FACTOR_ORDER
    )
    uas = (
        _ua(1, "red", 500.0, evidence=_all_evidence()),
        _ua(2, "red", None, valid=False, evidence=evidence_partial),
        _ua(3, "red", 300.0, evidence=_all_evidence()),
    )
    result = aggregate_side_analysis(uas, side="red")
    assert result.observed_unit_count == 2


# ── 10. Invalid side raises ValueError ────────────────────────────────


def test_invalid_side_raises():
    with pytest.raises(ValueError, match="side must be"):
        aggregate_side_analysis((), side="green")
    with pytest.raises(ValueError, match="side must be"):
        aggregate_side_analysis((), side="RED")
    with pytest.raises(ValueError, match="side must be"):
        aggregate_side_analysis((), side="")


# ── 11. Input not modified ────────────────────────────────────────────


def test_input_not_modified():
    uas = (_ua(1, "red", 100.0), _ua(2, "red", 200.0))
    before = deepcopy(uas)
    aggregate_side_analysis(uas, side="red")
    for i, ua in enumerate(uas):
        assert ua == before[i]


# ── 12. AdvantageAnalysis still UNRESOLVED ────────────────────────────


def test_advantage_computed_for_green_frame():
    frame = {
        "operators": [_unit(1, 0), _unit(2, 1)],
        "passengers": [],
    }
    result = build_green_frame_analysis(frame, frame_index=0)
    assert result.advantage.raw_advantage is not None
    assert result.advantage.label in ("RED", "BLUE", "EVEN")


def _unit(obj_id: int, color: int, **overrides):
    base = {
        "obj_id": obj_id, "color": color,
        "type": 2, "sub_type": 7, "cur_hex": 100 + obj_id,
        "blood": 100, "max_blood": 100, "value": 5000,
        "carry_weapon_ids": [36], "remain_bullet_nums": 100,
        "weapon_cool_time": 0, "weapon_unfold_state": 2,
        "move_state": 0, "stop": 1, "can_to_move": True, "on_board": False,
        "see_enemy_bop_ids": [201, 202],
        "keep": 0, "tire": 0, "lose_control": False, "close_combat": False,
    }
    base.update(overrides)
    return base


# ── 13. FrameAnalysis side aggregation integration ────────────────────


def test_frame_analysis_side_aggregation():
    frame = {
        "operators": [_unit(1, 0), _unit(2, 0), _unit(3, 1)],
        "passengers": [],
    }
    result = build_green_frame_analysis(frame, frame_index=0)

    red = result.side_analyses[0]
    blue = result.side_analyses[1]

    assert red.side == "red"
    assert red.unit_ids == (1, 2)
    assert red.unit_count == 2
    assert red.effective_power is not None
    assert red.effective_power > 0

    assert blue.side == "blue"
    assert blue.unit_ids == (3,)
    assert blue.unit_count == 1
    assert blue.effective_power is not None
    assert blue.effective_power > 0


def test_frame_analysis_coverage_full():
    frame = {
        "operators": [_unit(1, 0), _unit(2, 1)],
        "passengers": [],
    }
    result = build_green_frame_analysis(frame, frame_index=0)
    for sa in result.side_analyses:
        if sa.unit_count > 0:
            assert sa.coverage == 1.0


def test_frame_analysis_unresolved_warnings_removed():
    frame = {
        "operators": [_unit(1, 0)],
        "passengers": [],
    }
    result = build_green_frame_analysis(frame, frame_index=0)
    assert "side_aggregation_unresolved" not in result.warnings
    assert "advantage_unresolved" not in result.warnings


# ── 14. Consistency with demo faction_power() sum semantics ───────────


def test_aggregation_matches_demo_sum():
    """Sum of unit effective_power matches demo faction_power summation."""
    # demo faction_power: sum(unit_power(op).total) for op in faction units
    # Our equivalent: sum(UnitFactorAnalysis.effective_power) for side units
    uas = (
        _ua(1, "red", 6217.75),
        _ua(2, "red", 8500.0),
        _ua(3, "red", 3400.0),
    )
    result = aggregate_side_analysis(uas, side="red")
    assert result.effective_power == pytest.approx(6217.75 + 8500.0 + 3400.0)


def test_aggregation_matches_frame_analysis_sum():
    """Side effective_power equals sum of its unit effective_powers."""
    frame = {
        "operators": [
            _unit(1, 0, value=5000),
            _unit(2, 0, value=3000),
        ],
        "passengers": [],
    }
    result = build_green_frame_analysis(frame, frame_index=0)

    red = result.side_analyses[0]
    unit_sum = sum(
        ua.effective_power for ua in result.unit_analyses
        if ua.side == "red" and ua.effective_power is not None
    )
    assert red.effective_power == pytest.approx(unit_sum)


# ── 15. confidence stays None ─────────────────────────────────────────


def test_confidence_remains_none():
    uas = (_ua(1, "red", 500.0),)
    result = aggregate_side_analysis(uas, side="red")
    assert result.confidence is None


# ── finite numeric contract ───────────────────────────────────────────


@pytest.mark.parametrize("non_finite", [
    float("nan"), float("inf"), float("-inf"),
])
def test_hand_built_non_finite_unit_power_rejected(non_finite):
    with pytest.raises(ValueError, match="side aggregation red unit 1"):
        aggregate_side_analysis((_ua(1, "red", non_finite),), side="red")


def test_hand_built_bool_unit_power_rejected():
    unit = _ua(1, "red", True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="effective_power"):
        aggregate_side_analysis((unit,), side="red")


def test_side_sum_overflow_rejected():
    units = (
        _ua(1, "red", 1e308),
        _ua(2, "red", 1e308),
    )
    with pytest.raises(ValueError, match="side aggregation red effective_power sum"):
        aggregate_side_analysis(units, side="red")


def test_non_finite_power_rejected_even_when_unit_marked_invalid():
    unit = _ua(1, "red", float("nan"), valid=False)
    with pytest.raises(ValueError, match="side aggregation red unit 1"):
        aggregate_side_analysis((unit,), side="red")
