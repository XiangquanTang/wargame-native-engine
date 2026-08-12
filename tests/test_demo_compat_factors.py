from __future__ import annotations

from copy import deepcopy

import pytest

from wargame_native_engine.analysis.demo_compat_factors import (
    base_value_factor,
    health_factor,
)
from wargame_native_engine.analysis.green_frame_contract import (
    build_green_frame_analysis,
)
from wargame_native_engine.analysis.models import FactorStatus


# ── health_factor ─────────────────────────────────────────────────────


def test_health_normal():
    assert health_factor({"blood": 75, "max_blood": 100}) == pytest.approx(
        0.25 + 0.75 * 0.75
    )


def test_health_full():
    assert health_factor({"blood": 100, "max_blood": 100}) == 1.0


def test_health_zero_blood_destroyed():
    assert health_factor({"blood": 0, "max_blood": 100}) == 0.0


def test_health_negative_blood():
    assert health_factor({"blood": -5, "max_blood": 100}) == 0.0


def test_health_blood_exceeds_max_ratio_clamped():
    assert health_factor({"blood": 150, "max_blood": 100}) == 1.0


def test_health_max_blood_zero_blood_positive():
    """Demo edge-case: max_blood <= 0 and blood > 0 → 1.0."""
    assert health_factor({"blood": 50, "max_blood": 0}) == 1.0


def test_health_max_blood_negative():
    assert health_factor({"blood": 50, "max_blood": -10}) == 1.0


def test_health_max_blood_zero_blood_zero():
    assert health_factor({"blood": 0, "max_blood": 0}) == 0.0


def test_health_max_blood_zero_blood_negative():
    assert health_factor({"blood": -5, "max_blood": 0}) == 0.0


def test_health_missing_blood_returns_none():
    assert health_factor({"max_blood": 100}) is None


def test_health_missing_max_blood_returns_none():
    assert health_factor({"blood": 100}) is None


def test_health_both_missing_returns_none():
    assert health_factor({}) is None


def test_health_blood_none_returns_none():
    assert health_factor({"blood": None, "max_blood": 100}) is None


def test_health_max_blood_none_returns_none():
    assert health_factor({"blood": 100, "max_blood": None}) is None


def test_health_small_ratio():
    result = health_factor({"blood": 1, "max_blood": 100})
    assert result == pytest.approx(0.25 + 0.75 * 0.01)


# ── base_value_factor ─────────────────────────────────────────────────


def test_base_value_normal():
    assert base_value_factor({"value": 8500}) == 8500.0


def test_base_value_small_clamped_to_one():
    assert base_value_factor({"value": 0.5}) == 1.0


def test_base_value_negative_clamped_to_one():
    assert base_value_factor({"value": -100}) == 1.0


def test_base_value_zero_clamped_to_one():
    assert base_value_factor({"value": 0}) == 1.0


def test_base_value_exactly_one():
    assert base_value_factor({"value": 1.0}) == 1.0


def test_base_value_missing_returns_none():
    assert base_value_factor({"blood": 100}) is None


def test_base_value_none_returns_none():
    assert base_value_factor({"value": None}) is None


def test_base_value_empty_unit_returns_none():
    assert base_value_factor({}) is None


def test_base_value_non_numeric_raises():
    with pytest.raises((ValueError, TypeError)):
        base_value_factor({"value": "not-a-number"})


def test_base_value_dict_value_raises():
    with pytest.raises((ValueError, TypeError)):
        base_value_factor({"value": {"nested": 1}})


def test_base_value_list_value_raises():
    with pytest.raises((ValueError, TypeError)):
        base_value_factor({"value": [1, 2, 3]})


def test_base_value_nan_raises():
    with pytest.raises(ValueError):
        base_value_factor({"value": float("nan")})


def test_base_value_inf_raises():
    with pytest.raises(ValueError):
        base_value_factor({"value": float("inf")})


# ── formula_id on FactorValue ─────────────────────────────────────────


def _unit(obj_id: int, color: int, **overrides) -> dict:
    base = {
        "obj_id": obj_id,
        "color": color,
        "type": 2,
        "sub_type": 7,
        "cur_hex": 100 + obj_id,
        "blood": 100,
        "max_blood": 100,
        "value": 8500,
        "carry_weapon_ids": [42],
        "remain_bullet_nums": 30,
        "weapon_cool_time": 0,
        "weapon_unfold_state": 2,
        "move_state": 0,
        "stop": 1,
        "can_to_move": True,
        "on_board": False,
        "see_enemy_bop_ids": [201, 202],
        "keep": 0,
        "tire": 0,
        "lose_control": False,
        "close_combat": False,
    }
    base.update(overrides)
    return base


def _green(**overrides) -> dict:
    return {"operators": [_unit(1, 0)], "passengers": [], **overrides}


def test_base_value_formula_id():
    result = build_green_frame_analysis(_green(), frame_index=0)
    bv = result.unit_analyses[0].factor_values[0]
    assert bv.factor_name == "base_value"
    assert bv.formula_id == "demo-compatible.base_value.v1"


def test_health_formula_id():
    result = build_green_frame_analysis(_green(), frame_index=0)
    hv = result.unit_analyses[0].factor_values[1]
    assert hv.factor_name == "health"
    assert hv.formula_id == "demo-compatible.health.v1"


def test_all_eight_factors_have_formula_ids():
    result = build_green_frame_analysis(_green(), frame_index=0)
    for fv in result.unit_analyses[0].factor_values:
        assert fv.formula_id != "UNRESOLVED", fv.factor_name
        assert fv.formula_id.startswith("demo-compatible."), fv.factor_name


# ── UNKNOWN not converted to 0 or 1 ────────────────────────────────────


def test_unknown_base_value_not_converted():
    # Missing 'value' → evidence UNKNOWN → FactorValue.value must be None
    unit_no_value = {
        "obj_id": 1, "color": 0, "type": 2, "sub_type": 7,
        "cur_hex": 101, "blood": 100, "max_blood": 100,
    }
    frame = {"operators": [unit_no_value], "passengers": []}
    result = build_green_frame_analysis(frame, frame_index=0)

    bv = result.unit_analyses[0].factor_values[0]
    assert bv.factor_name == "base_value"
    assert bv.status == FactorStatus.UNKNOWN
    assert bv.value is None


def test_unknown_health_not_converted():
    # Missing blood/max_blood → evidence UNKNOWN
    unit_no_health = {
        "obj_id": 1, "color": 0, "type": 2, "sub_type": 7,
        "cur_hex": 101, "value": 100,
    }
    frame = {"operators": [unit_no_health], "passengers": []}
    result = build_green_frame_analysis(frame, frame_index=0)

    hv = result.unit_analyses[0].factor_values[1]
    assert hv.factor_name == "health"
    assert hv.status == FactorStatus.UNKNOWN
    assert hv.value is None


# ── Other six factors stay None ────────────────────────────────────────


def test_all_eight_factors_have_values():
    result = build_green_frame_analysis(_green(), frame_index=0)
    fvs = result.unit_analyses[0].factor_values
    for fv in fvs:
        assert fv.value is not None, fv.factor_name


# ── effective_power still None, valid still False ──────────────────────


def test_effective_power_now_computed():
    result = build_green_frame_analysis(_green(), frame_index=0)
    ua = result.unit_analyses[0]
    assert ua.effective_power is not None
    assert ua.effective_power > 0
    assert ua.valid is True


# ── Input unchanged ────────────────────────────────────────────────────


def test_unit_input_unchanged():
    unit = _unit(1, 0)
    before = deepcopy(unit)
    health_factor(unit)
    assert unit == before


def test_base_value_input_unchanged():
    unit = {"value": 8500}
    before = deepcopy(unit)
    base_value_factor(unit)
    assert unit == before


def test_frame_input_unchanged():
    frame = _green()
    before = deepcopy(frame)
    build_green_frame_analysis(frame, frame_index=0)
    assert frame == before


# ── Minimal source-compatible unit drives V/H ─────────────────────────


def test_minimal_source_compatible_unit_drives_vh():
    """Prove that value, blood, max_blood from GREEN unit drive V and H."""
    unit = {"obj_id": 1, "color": 0, "value": 5000,
            "blood": 80, "max_blood": 100,
            "type": 2, "sub_type": 7, "cur_hex": 1234}
    frame = {"operators": [unit], "passengers": []}
    result = build_green_frame_analysis(frame, frame_index=0)

    ua = result.unit_analyses[0]
    bv = ua.factor_values[0]
    hv = ua.factor_values[1]

    assert bv.value == 5000.0
    assert bv.status == FactorStatus.OBSERVED
    assert hv.value == pytest.approx(0.25 + 0.75 * 0.8)
    assert hv.status == FactorStatus.OBSERVED


# ── status preserved from evidence ────────────────────────────────────


def test_status_observed_preserved():
    result = build_green_frame_analysis(_green(), frame_index=0)
    for fv in result.unit_analyses[0].factor_values:
        assert fv.status == FactorStatus.OBSERVED


def test_status_unknown_preserved_for_missing_fields():
    unit = {"obj_id": 1, "color": 0, "value": 100,
            "type": 2, "sub_type": 7, "cur_hex": 101}
    frame = {"operators": [unit], "passengers": []}
    result = build_green_frame_analysis(frame, frame_index=0)

    ua = result.unit_analyses[0]
    health_fv = ua.factor_values[1]
    assert health_fv.factor_name == "health"
    assert health_fv.status == FactorStatus.UNKNOWN
    assert health_fv.value is None


# ── weapon_factor ─────────────────────────────────────────────────────

from wargame_native_engine.analysis.demo_compat_factors import (
    weapon_factor as _wf,
    ammo_factor as _af,
    readiness_factor as _rf,
    mobility_factor as _mf,
    information_factor as _inf,
    status_factor as _sf,
)


def test_weapon_normal():
    assert _wf({"carry_weapon_ids": [36]}) == 2.5


def test_weapon_max_of_multiple():
    assert _wf({"carry_weapon_ids": [1, 36, 3]}) == 2.5


def test_weapon_empty_list_defaults_06():
    assert _wf({"carry_weapon_ids": []}) == 0.6


def test_weapon_missing_field_defaults_06():
    assert _wf({}) == 0.6


def test_weapon_unknown_id_defaults_1():
    assert _wf({"carry_weapon_ids": [99999]}) == 1.0


def test_weapon_mixed_known_and_unknown():
    assert _wf({"carry_weapon_ids": [36, 99999]}) == 2.5


# ── ammo_factor ───────────────────────────────────────────────────────


def test_ammo_normal():
    result = _af({"remain_bullet_nums": {"0": 100}})
    assert 0.55 < result <= 1.25


def test_ammo_non_dict_defaults_07():
    assert _af({"remain_bullet_nums": [1, 2, 3]}) == 0.7


def test_ammo_missing_field_defaults_03():
    # missing field → {} → total 0 → 0.3
    assert _af({}) == 0.3


def test_ammo_zero_total_defaults_03():
    assert _af({"remain_bullet_nums": {"0": 0, "1": 0}}) == 0.3


def test_ammo_negative_values_clamped():
    # _as_float(-5) = -5.0, max(0, -5) = 0
    assert _af({"remain_bullet_nums": {"0": -999}}) == 0.3


def test_ammo_upper_bound_125():
    result = _af({"remain_bullet_nums": {"0": 1_000_000}})
    assert result == 1.25


def test_ammo_log_scaling():
    a1 = _af({"remain_bullet_nums": {"0": 10}})
    a2 = _af({"remain_bullet_nums": {"0": 100}})
    assert a2 > a1


# ── readiness_factor ──────────────────────────────────────────────────


def test_readiness_no_cooldown_no_unfold():
    assert _rf({"weapon_cool_time": 0, "weapon_unfold_state": 2}) == 1.0


def test_readiness_cooldown_reduces():
    assert _rf({"weapon_cool_time": 90, "weapon_unfold_state": 2}) == pytest.approx(
        max(0.55, 1.0 - 90 / 180.0))


def test_readiness_cooldown_floor_055():
    assert _rf({"weapon_cool_time": 999, "weapon_unfold_state": 2}) == 0.55


def test_readiness_cooldown_cap_at_100():
    # min(cool, 100) — values above 100 are capped
    r1 = _rf({"weapon_cool_time": 100, "weapon_unfold_state": 2})
    r2 = _rf({"weapon_cool_time": 500, "weapon_unfold_state": 2})
    assert r1 == r2


def test_readiness_unfold_0_multiplies_09():
    assert _rf({"weapon_cool_time": 0, "weapon_unfold_state": 0}) == 0.9


def test_readiness_unfold_1_multiplies_105():
    assert _rf({"weapon_cool_time": 0, "weapon_unfold_state": 1}) == 1.05


def test_readiness_missing_fields_defaults():
    assert _rf({}) == 1.0


# ── mobility_factor ───────────────────────────────────────────────────


def test_mobility_state_0():
    assert _mf({"move_state": 0}) == 1.0


def test_mobility_state_1():
    assert _mf({"move_state": 1}) == 1.05


def test_mobility_state_2():
    assert _mf({"move_state": 2}) == 1.1


def test_mobility_state_3():
    assert _mf({"move_state": 3}) == 1.15


def test_mobility_state_4():
    assert _mf({"move_state": 4}) == 0.9


def test_mobility_state_5():
    assert _mf({"move_state": 5}) == 0.95


def test_mobility_unknown_state_defaults_1():
    assert _mf({"move_state": 99}) == 1.0


def test_mobility_stop_multiplier():
    assert _mf({"move_state": 0, "stop": 1}) == 1.0 * 0.95


def test_mobility_can_to_move_false():
    assert _mf({"move_state": 0, "can_to_move": False}) == 1.0 * 0.85


def test_mobility_on_board():
    assert _mf({"move_state": 0, "on_board": True}) == 1.0 * 0.9


def test_mobility_combined():
    result = _mf({"move_state": 1, "stop": 1, "can_to_move": False, "on_board": True})
    assert result == pytest.approx(1.05 * 0.95 * 0.85 * 0.9)


def test_mobility_missing_defaults():
    assert _mf({}) == 1.0


# ── information_factor ────────────────────────────────────────────────


def test_information_normal():
    assert _inf({"see_enemy_bop_ids": [1, 2, 3]}) == 1.0 + 0.05 * 3


def test_information_empty_list():
    assert _inf({"see_enemy_bop_ids": []}) == 1.0


def test_information_missing_field_defaults_1():
    assert _inf({}) == 1.0


def test_information_upper_bound_125():
    assert _inf({"see_enemy_bop_ids": list(range(100))}) == 1.25


# ── status_factor ─────────────────────────────────────────────────────


def test_status_keep():
    assert _sf({"keep": 1}) == 0.65


def test_status_tire_1():
    assert _sf({"tire": 1}) == 0.9


def test_status_tire_2():
    assert _sf({"tire": 2}) == 0.75


def test_status_tire_3():
    assert _sf({"tire": 3}) == 0.75


def test_status_lose_control():
    assert _sf({"lose_control": True}) == 0.2


def test_status_close_combat():
    assert _sf({"close_combat": True}) == 1.05


def test_status_combined():
    result = _sf({"keep": 1, "tire": 2, "lose_control": True, "close_combat": True})
    assert result == pytest.approx(0.65 * 0.75 * 0.2 * 1.05)


def test_status_baseline():
    assert _sf({}) == 1.0


def test_status_keep_false_no_effect():
    assert _sf({"keep": 0}) == 1.0


# ── formula_id for all factors ────────────────────────────────────────


def test_all_formula_ids_correct():
    result = build_green_frame_analysis(_green(), frame_index=0)
    expected = {
        "base_value": "demo-compatible.base_value.v1",
        "health": "demo-compatible.health.v1",
        "weapon": "demo-compatible.weapon.v1",
        "ammo": "demo-compatible.ammo.v1",
        "readiness": "demo-compatible.readiness.v1",
        "mobility": "demo-compatible.mobility.v1",
        "information": "demo-compatible.information.v1",
        "status": "demo-compatible.status.v1",
    }
    for fv in result.unit_analyses[0].factor_values:
        assert fv.formula_id == expected[fv.factor_name], fv.factor_name


# ── observed_unit_count ───────────────────────────────────────────────


def test_observed_unit_count_all_complete():
    frame = _green(
        operators=[_unit(1, 0), _unit(2, 1)],
        passengers=[],
    )
    result = build_green_frame_analysis(frame, frame_index=0)
    assert result.side_analyses[0].observed_unit_count == 1  # unit 1 red
    assert result.side_analyses[1].observed_unit_count == 1  # unit 2 blue


def test_observed_unit_count_partial():
    unit_complete = _unit(1, 0)
    unit_partial = {"obj_id": 2, "color": 0, "type": 2, "sub_type": 7,
                    "cur_hex": 102, "value": 100}  # only base_value present
    frame = {"operators": [unit_complete, unit_partial], "passengers": []}
    result = build_green_frame_analysis(frame, frame_index=0)
    # red has 2 units, only 1 is fully observed
    assert result.side_analyses[0].observed_unit_count == 1
    assert result.side_analyses[0].unit_count == 2


def test_observed_unit_count_zero_when_none_full():
    unit = {"obj_id": 1, "color": 1, "type": 2, "sub_type": 7,
            "cur_hex": 101, "value": 100}
    frame = {"operators": [unit], "passengers": []}
    result = build_green_frame_analysis(frame, frame_index=0)
    assert result.side_analyses[1].observed_unit_count == 0
    assert result.side_analyses[1].unit_count == 1


# ── input immutability for all factors ────────────────────────────────


def test_weapon_input_unchanged():
    unit = {"carry_weapon_ids": [36, 1]}
    before = deepcopy(unit)
    _wf(unit)
    assert unit == before


def test_ammo_input_unchanged():
    unit = {"remain_bullet_nums": {"0": 100}}
    before = deepcopy(unit)
    _af(unit)
    assert unit == before


def test_readiness_input_unchanged():
    unit = {"weapon_cool_time": 90, "weapon_unfold_state": 0}
    before = deepcopy(unit)
    _rf(unit)
    assert unit == before


def test_mobility_input_unchanged():
    unit = {"move_state": 1, "stop": 1}
    before = deepcopy(unit)
    _mf(unit)
    assert unit == before


def test_information_input_unchanged():
    unit = {"see_enemy_bop_ids": [1, 2]}
    before = deepcopy(unit)
    _inf(unit)
    assert unit == before


def test_status_input_unchanged():
    unit = {"keep": 1, "tire": 2}
    before = deepcopy(unit)
    _sf(unit)
    assert unit == before


# ── finite numeric contract ───────────────────────────────────────────


@pytest.mark.parametrize("non_finite", [
    float("nan"), float("inf"), float("-inf"),
])
@pytest.mark.parametrize(
    ("factor", "unit_factory", "stage"),
    [
        (base_value_factor, lambda value: {"value": value}, "base_value_factor"),
        (
            health_factor,
            lambda value: {"blood": value, "max_blood": 100},
            "health_factor",
        ),
        (_wf, lambda value: {"carry_weapon_ids": [value]}, "weapon_factor"),
        (
            _af,
            lambda value: {"remain_bullet_nums": {"weapon": value}},
            "ammo_factor",
        ),
        (
            _rf,
            lambda value: {
                "weapon_cool_time": value,
                "weapon_unfold_state": 2,
            },
            "readiness_factor",
        ),
        (_mf, lambda value: {"move_state": value}, "mobility_factor"),
        (
            _inf,
            lambda value: {"see_enemy_bop_ids": value},
            "information_factor",
        ),
        (_sf, lambda value: {"tire": value}, "status_factor"),
    ],
)
def test_numeric_factor_inputs_reject_nan_and_infinity(
    factor, unit_factory, stage, non_finite,
):
    with pytest.raises(ValueError, match=stage):
        factor(unit_factory(non_finite))


@pytest.mark.parametrize(
    ("factor", "unit", "stage"),
    [
        (base_value_factor, {"value": True}, "base_value_factor"),
        (health_factor, {"blood": True, "max_blood": 100}, "health_factor"),
        (_wf, {"carry_weapon_ids": [True]}, "weapon_factor"),
        (_af, {"remain_bullet_nums": {"weapon": True}}, "ammo_factor"),
        (
            _rf,
            {"weapon_cool_time": True, "weapon_unfold_state": 2},
            "readiness_factor",
        ),
        (_mf, {"move_state": True}, "mobility_factor"),
        (_inf, {"see_enemy_bop_ids": True}, "information_factor"),
        (_sf, {"tire": True}, "status_factor"),
    ],
)
def test_numeric_factor_inputs_reject_bool(factor, unit, stage):
    with pytest.raises(TypeError, match=stage):
        factor(unit)


def test_finite_integer_float_zero_and_numeric_strings_remain_supported():
    assert base_value_factor({"value": "2.5"}) == 2.5
    assert health_factor({"blood": 0, "max_blood": 100.0}) == 0.0
    assert _wf({"carry_weapon_ids": ["36"]}) == 2.5
    assert _af({"remain_bullet_nums": {"weapon": "10"}}) > 0.55
    assert _rf({"weapon_cool_time": "0", "weapon_unfold_state": 2}) == 1.0
    assert _mf({"move_state": 1.0}) == 1.05
    assert _inf({"see_enemy_bop_ids": [1, 2.0, "3"]}) == 1.15
    assert _sf({"tire": "2"}) == 0.75


def test_state_switch_booleans_remain_valid():
    assert _mf({
        "move_state": 0,
        "stop": True,
        "can_to_move": False,
        "on_board": True,
    }) == pytest.approx(0.95 * 0.85 * 0.9)
    assert _sf({
        "keep": True,
        "tire": 0,
        "lose_control": True,
        "close_combat": True,
    }) == pytest.approx(0.65 * 0.2 * 1.05)


@pytest.mark.parametrize("non_finite", [
    float("nan"), float("inf"), float("-inf"),
])
def test_scalar_non_finite_ammo_input_is_not_demo_defaulted(non_finite):
    with pytest.raises(ValueError, match="ammo_factor"):
        _af({"remain_bullet_nums": non_finite})


def test_health_intermediate_overflow_rejected():
    with pytest.raises(ValueError, match="health_factor ratio"):
        health_factor({"blood": 1e308, "max_blood": 1e-308})


def test_ammo_sum_overflow_rejected():
    with pytest.raises(ValueError, match="ammo_factor total"):
        _af({"remain_bullet_nums": {"a": 1e308, "b": 1e308}})
