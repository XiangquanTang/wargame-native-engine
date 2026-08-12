from __future__ import annotations

from copy import deepcopy

import pytest

from wargame_native_engine.analysis.eight_factor_contract import (
    FACTOR_ORDER,
    FactorEvidence,
    FactorStatus,
)
from wargame_native_engine.analysis.effective_power import (
    EFFECTIVE_POWER_FORMULA_ID,
    calculate_unit_effective_power,
)
from wargame_native_engine.analysis.green_frame_contract import (
    build_green_frame_analysis,
)
from wargame_native_engine.analysis.models import FactorValue


def _fv(name: str, value: float | None) -> FactorValue:
    return FactorValue(
        factor_name=name,
        value=value,
        status=FactorStatus.OBSERVED,
        source_paths=(f"unit.{name}",),
        formula_id="demo-compatible.test.v1",
        warnings=(),
        missing_fields=(),
    )


def _all_fvs(**values: float) -> tuple[FactorValue, ...]:
    return tuple(_fv(name, values[name]) for name in FACTOR_ORDER)


# ── 1. Normal product ─────────────────────────────────────────────────


def test_normal_product():
    fvs = _all_fvs(
        base_value=8500.0, health=1.0, weapon=2.5, ammo=1.0,
        readiness=1.0, mobility=1.0, information=1.0, status=1.0,
    )
    assert calculate_unit_effective_power(fvs) == 8500.0 * 2.5


def test_product_matches_demo_expected():
    fvs = _all_fvs(
        base_value=5000.0, health=0.85, weapon=1.8, ammo=0.9,
        readiness=0.95, mobility=1.05, information=1.1, status=0.8,
    )
    expected = 5000.0 * 0.85 * 1.8 * 0.9 * 0.95 * 1.05 * 1.1 * 0.8
    assert calculate_unit_effective_power(fvs) == pytest.approx(expected)


# ── 2. Destroyed unit H=0 → effective_power=0, valid=True ────────────


def test_destroyed_unit_zero_power():
    fvs = _all_fvs(
        base_value=8500.0, health=0.0, weapon=2.5, ammo=1.0,
        readiness=1.0, mobility=1.0, information=1.0, status=1.0,
    )
    result = calculate_unit_effective_power(fvs)
    assert result == 0.0
    # 0.0 is a valid result, not None


# ── 3. Any factor None → result None ─────────────────────────────────


def test_any_none_returns_none():
    for i, name in enumerate(FACTOR_ORDER):
        fvs_list = [_fv(n, 1.0) for n in FACTOR_ORDER]
        fvs_list[i] = _fv(name, None)
        assert calculate_unit_effective_power(tuple(fvs_list)) is None, name


# ── 4. Missing factor raises ValueError ────────────────────────────────


def test_too_few_factors_raises():
    fvs = tuple(_fv(name, 1.0) for name in FACTOR_ORDER[:7])
    with pytest.raises(ValueError, match="exactly"):
        calculate_unit_effective_power(fvs)


def test_too_many_factors_raises():
    fvs = tuple(_fv(name, 1.0) for name in FACTOR_ORDER) + (_fv("extra", 1.0),)
    with pytest.raises(ValueError, match="exactly"):
        calculate_unit_effective_power(fvs)


# ── 5. Duplicate factor raises ValueError ─────────────────────────────


def test_duplicate_factor_raises():
    fvs_list = list(_all_fvs(**{k: 1.0 for k in FACTOR_ORDER}))
    fvs_list[4] = _fv("base_value", 999.0)  # duplicate base_value
    with pytest.raises(ValueError, match="duplicate"):
        calculate_unit_effective_power(tuple(fvs_list))


# ── 6. Unknown factor name raises ValueError ─────────────────────────


def test_unknown_factor_name_raises():
    fvs_list = list(_all_fvs(**{k: 1.0 for k in FACTOR_ORDER}))
    fvs_list[3] = _fv("not_a_factor", 1.0)
    with pytest.raises(ValueError, match="unknown factor"):
        calculate_unit_effective_power(tuple(fvs_list))


# ── 7. Out-of-order input: normalised to FACTOR_ORDER ────────────────


def test_out_of_order_input_normalised():
    # Reverse order input — should still produce correct product
    fvs = tuple(_fv(name, float(i + 1)) for i, name in enumerate(reversed(FACTOR_ORDER)))
    # FACTOR_ORDER values would be 8,7,6,5,4,3,2,1
    expected = 1.0
    for i in range(8):
        expected *= float(8 - i)
    assert calculate_unit_effective_power(fvs) == pytest.approx(expected)


# ── 8. Input not modified ─────────────────────────────────────────────


def test_input_not_modified():
    fvs = _all_fvs(**{k: 1.0 for k in FACTOR_ORDER})
    before = deepcopy(fvs)
    calculate_unit_effective_power(fvs)
    assert fvs == before


# ── 9. EFFECTIVE_POWER_FORMULA_ID constant ────────────────────────────


def test_constant_defined():
    assert EFFECTIVE_POWER_FORMULA_ID == "demo-compatible.unit-effective-power.v1"


# ── 10. FrameAnalysis: complete unit gets effective_power ─────────────


def _unit(obj_id: int, color: int, **overrides):
    base = {
        "obj_id": obj_id, "color": color,
        "type": 2, "sub_type": 7, "cur_hex": 100 + obj_id,
        "blood": 100, "max_blood": 100, "value": 5000,
        "carry_weapon_ids": [36],
        "remain_bullet_nums": 100,
        "weapon_cool_time": 0, "weapon_unfold_state": 2,
        "move_state": 0, "stop": 1, "can_to_move": True, "on_board": False,
        "see_enemy_bop_ids": [201, 202],
        "keep": 0, "tire": 0, "lose_control": False, "close_combat": False,
    }
    base.update(overrides)
    return base


def _green(**overrides):
    return {"operators": [_unit(1, 0)], "passengers": [], **overrides}


def test_frame_analysis_has_effective_power():
    result = build_green_frame_analysis(_green(), frame_index=0)
    ua = result.unit_analyses[0]
    assert ua.effective_power is not None
    assert ua.effective_power > 0
    assert ua.valid is True
    assert ua.warnings == ()


# ── 11. FrameAnalysis: incomplete unit valid=False ────────────────────


def test_frame_analysis_incomplete_unit_invalid():
    unit = {"obj_id": 1, "color": 0, "type": 2, "sub_type": 7,
            "cur_hex": 101, "value": 100}  # only base_value present
    frame = {"operators": [unit], "passengers": []}
    result = build_green_frame_analysis(frame, frame_index=0)

    ua = result.unit_analyses[0]
    assert ua.effective_power is None
    assert ua.valid is False
    assert "effective_power_unavailable" in ua.warnings


# ── 12. SideAnalysis.effective_power still None ───────────────────────


def test_side_effective_power_computed_for_complete_units():
    result = build_green_frame_analysis(_green(), frame_index=0)
    for sa in result.side_analyses:
        if sa.unit_count > 0:
            assert sa.effective_power is not None
            assert sa.effective_power > 0


# ── 13. AdvantageAnalysis still UNRESOLVED ────────────────────────────


def test_advantage_now_computed():
    result = build_green_frame_analysis(_green(), frame_index=0)
    assert result.advantage.raw_advantage is not None
    assert result.advantage.label in ("RED", "BLUE", "EVEN")


# ── 14. Frame-level warnings ──────────────────────────────────────────


def test_frame_warnings_current_state():
    result = build_green_frame_analysis(_green(), frame_index=0)
    assert "effective_power_unresolved" not in result.warnings
    assert "side_aggregation_unresolved" not in result.warnings
    assert "advantage_unresolved" not in result.warnings


# ── 15. Destroyed unit in FrameAnalysis ───────────────────────────────


def test_frame_analysis_destroyed_unit():
    unit = _unit(1, 0, blood=0, max_blood=100)
    frame = {"operators": [unit], "passengers": []}
    result = build_green_frame_analysis(frame, frame_index=0)

    ua = result.unit_analyses[0]
    assert ua.effective_power == 0.0
    assert ua.valid is True  # 0.0 is valid
    assert ua.warnings == ()


# ── 16. demo_default_applied warning ──────────────────────────────────


def test_demo_default_applied_warning():
    # carry_weapon_ids exists but is None → weapon_factor returns 0.6 (demo default)
    unit = _unit(1, 0, carry_weapon_ids=None)
    frame = {"operators": [unit], "passengers": []}
    result = build_green_frame_analysis(frame, frame_index=0)

    weapon_fv = result.unit_analyses[0].factor_values[2]
    assert weapon_fv.factor_name == "weapon"
    assert weapon_fv.value == 0.6
    assert "demo_default_applied" in weapon_fv.warnings


def test_no_demo_default_when_values_present():
    result = build_green_frame_analysis(_green(), frame_index=0)
    for fv in result.unit_analyses[0].factor_values:
        assert "demo_default_applied" not in fv.warnings


# ── finite numeric contract ───────────────────────────────────────────


@pytest.mark.parametrize("non_finite", [
    float("nan"), float("inf"), float("-inf"),
])
def test_non_finite_factor_value_rejected(non_finite):
    fvs = list(_all_fvs(**{name: 1.0 for name in FACTOR_ORDER}))
    fvs[3] = _fv("ammo", non_finite)

    with pytest.raises(ValueError, match="effective_power factor ammo"):
        calculate_unit_effective_power(tuple(fvs))


def test_bool_factor_value_rejected():
    fvs = list(_all_fvs(**{name: 1.0 for name in FACTOR_ORDER}))
    fvs[5] = _fv("mobility", True)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="effective_power factor mobility"):
        calculate_unit_effective_power(tuple(fvs))


def test_finite_factor_product_overflow_rejected():
    fvs = _all_fvs(
        base_value=1e308,
        health=1e308,
        weapon=1.0,
        ammo=1.0,
        readiness=1.0,
        mobility=1.0,
        information=1.0,
        status=1.0,
    )

    with pytest.raises(ValueError, match="effective_power product after health"):
        calculate_unit_effective_power(fvs)


def test_unknown_still_returns_none_under_finite_contract():
    fvs = list(_all_fvs(**{name: 1.0 for name in FACTOR_ORDER}))
    fvs[7] = _fv("status", None)
    assert calculate_unit_effective_power(tuple(fvs)) is None


def test_zero_remains_valid_under_finite_contract():
    fvs = _all_fvs(**{
        name: (0.0 if name == "health" else 1.0)
        for name in FACTOR_ORDER
    })
    assert calculate_unit_effective_power(fvs) == 0.0
