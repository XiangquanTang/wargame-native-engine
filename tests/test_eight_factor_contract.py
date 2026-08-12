from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from wargame_native_engine.analysis.eight_factor_contract import (
    FACTOR_ORDER,
    FactorEvidence,
    FactorStatus,
    extract_unit_factor_evidence,
)


def _complete_unit(**overrides):
    base = {
        "value": 8500,
        "blood": 100,
        "max_blood": 100,
        "carry_weapon_ids": [42],
        "remain_bullet_nums": 30,
        "weapon_cool_time": 0,
        "weapon_unfold_state": 2,
        "move_state": "STOPPED",
        "stop": True,
        "can_to_move": True,
        "on_board": False,
        "see_enemy_bop_ids": [101, 102],
        "keep": 0,
        "tire": 0,
        "lose_control": False,
        "close_combat": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Complete input: eight factors, fixed order
# ---------------------------------------------------------------------------
def test_complete_input_returns_eight_factors_in_order():
    unit = _complete_unit()
    results = extract_unit_factor_evidence(unit)

    assert len(results) == 8
    for i, name in enumerate(FACTOR_ORDER):
        assert results[i].name == name


# ---------------------------------------------------------------------------
# 2. source_paths are correct per factor
# ---------------------------------------------------------------------------
def test_source_paths_per_factor():
    unit = _complete_unit()
    results = extract_unit_factor_evidence(unit)

    expected = {
        "base_value": ("unit.value",),
        "health": ("unit.blood", "unit.max_blood"),
        "weapon": ("unit.carry_weapon_ids",),
        "ammo": ("unit.remain_bullet_nums",),
        "readiness": ("unit.weapon_cool_time", "unit.weapon_unfold_state"),
        "mobility": ("unit.move_state", "unit.stop", "unit.can_to_move", "unit.on_board"),
        "information": ("unit.see_enemy_bop_ids",),
        "status": ("unit.keep", "unit.tire", "unit.lose_control", "unit.close_combat"),
    }

    for ev in results:
        assert ev.source_paths == expected[ev.name]


# ---------------------------------------------------------------------------
# 3. Missing fields => UNKNOWN status
# ---------------------------------------------------------------------------
def test_missing_field_yields_unknown_status():
    unit = {
        "value": 8500,
        "blood": 100,
        # "max_blood" intentionally missing
        "carry_weapon_ids": [42],
        "remain_bullet_nums": 30,
        "weapon_cool_time": 0,
        "weapon_unfold_state": 2,
        "move_state": "STOPPED",
        "stop": True,
        "can_to_move": True,
        "on_board": False,
        "see_enemy_bop_ids": [101],
        "keep": 0,
        "tire": 0,
        "lose_control": False,
        "close_combat": False,
    }
    results = extract_unit_factor_evidence(unit)

    # health should be UNKNOWN because max_blood is missing
    health = results[1]
    assert health.name == "health"
    assert health.status == FactorStatus.UNKNOWN
    assert "max_blood" in health.missing_fields

    # All others should be OBSERVED
    for ev in results:
        if ev.name == "health":
            continue
        assert ev.status == FactorStatus.OBSERVED, f"{ev.name} should be OBSERVED"


# ---------------------------------------------------------------------------
# 4. missing_fields content stable and sorted
# ---------------------------------------------------------------------------
def test_missing_fields_sorted_and_stable():
    unit: dict = {}  # no fields at all
    results = extract_unit_factor_evidence(unit)

    for ev in results:
        assert ev.status == FactorStatus.UNKNOWN
        assert ev.missing_fields == tuple(sorted(ev.missing_fields))

    # Check known missing lists are sorted
    health = results[1]
    assert health.missing_fields == ("blood", "max_blood")

    mobility = results[5]
    assert mobility.missing_fields == ("can_to_move", "move_state", "on_board", "stop")

    status_ev = results[7]
    assert status_ev.missing_fields == ("close_combat", "keep", "lose_control", "tire")


# ---------------------------------------------------------------------------
# 5. None values are preserved, not replaced
# ---------------------------------------------------------------------------
def test_none_value_preserved():
    unit = _complete_unit(blood=None, max_blood=None)
    results = extract_unit_factor_evidence(unit)

    health = results[1]
    assert health.name == "health"
    assert health.status == FactorStatus.OBSERVED
    raw = health.raw_value
    assert raw[0] is None
    assert raw[1] is None


# ---------------------------------------------------------------------------
# 6. Input Mapping unchanged after call
# ---------------------------------------------------------------------------
def test_input_unchanged():
    unit = _complete_unit()
    before = deepcopy(unit)
    extract_unit_factor_evidence(unit)
    assert unit == before

    # Also with a plain dict not created by us
    plain = {"value": 1, "blood": 50}
    before2 = deepcopy(plain)
    extract_unit_factor_evidence(plain)
    assert plain == before2


# ---------------------------------------------------------------------------
# 7. Non-Mapping raises TypeError
# ---------------------------------------------------------------------------
def test_non_mapping_raises_typeerror():
    with pytest.raises(TypeError):
        extract_unit_factor_evidence([1, 2, 3])  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        extract_unit_factor_evidence("not a mapping")  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        extract_unit_factor_evidence(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 8. No effective_force, V_combat, advantage, or score in output
# ---------------------------------------------------------------------------
def test_no_derived_fields_in_output():
    unit = _complete_unit()
    results = extract_unit_factor_evidence(unit)

    for ev in results:
        assert not hasattr(ev, "effective_force")
        assert not hasattr(ev, "V_combat")
        assert not hasattr(ev, "advantage")
        assert not hasattr(ev, "score")

    # Also verify the fields don't leak into dict representation
    for ev in results:
        d = ev.__dict__ if hasattr(ev, "__dict__") else {}
        assert "effective_force" not in d
        assert "V_combat" not in d
        assert "advantage" not in d
        assert "score" not in d


# ---------------------------------------------------------------------------
# 9. Repeated calls return same results
# ---------------------------------------------------------------------------
def test_repeatable_results():
    unit = _complete_unit()
    r1 = extract_unit_factor_evidence(unit)
    r2 = extract_unit_factor_evidence(unit)
    assert r1 == r2

    # Partial unit
    partial = {"value": 100}
    r3 = extract_unit_factor_evidence(partial)
    r4 = extract_unit_factor_evidence(partial)
    assert r3 == r4


# ---------------------------------------------------------------------------
# Extra: raw_value shape for multi-field factors
# ---------------------------------------------------------------------------
def test_multi_field_raw_value_is_tuple():
    unit = _complete_unit()
    results = extract_unit_factor_evidence(unit)

    for ev in results:
        n = len(ev.source_paths)
        if n == 1:
            assert not isinstance(ev.raw_value, tuple)
        else:
            assert isinstance(ev.raw_value, tuple)
            assert len(ev.raw_value) == n


# ---------------------------------------------------------------------------
# Extra: partial multi-field where some are missing
# ---------------------------------------------------------------------------
def test_partial_multi_field_raw_value():
    unit = {"blood": 100}  # max_blood missing
    results = extract_unit_factor_evidence(unit)

    health = results[1]
    assert health.name == "health"
    assert health.status == FactorStatus.UNKNOWN
    # raw_value is a tuple with (100, None) since blood=100 present, max_blood=None filled
    assert isinstance(health.raw_value, tuple)
    assert health.raw_value[0] == 100
    assert health.raw_value[1] is None


# ---------------------------------------------------------------------------
# Extra: empty dict still returns eight factors
# ---------------------------------------------------------------------------
def test_empty_unit_returns_eight_factors_all_unknown():
    results = extract_unit_factor_evidence({})

    assert len(results) == 8
    for ev in results:
        assert ev.status == FactorStatus.UNKNOWN
        assert len(ev.missing_fields) > 0


# ---------------------------------------------------------------------------
# Mutation isolation: modifying input after extraction does not change
# already-returned raw_value
# ---------------------------------------------------------------------------
def test_raw_value_isolated_from_carry_weapon_ids_mutation():
    unit = _complete_unit()
    results = extract_unit_factor_evidence(unit)

    weapon = results[2]
    assert weapon.name == "weapon"
    original_ids = list(weapon.raw_value)

    # Mutate original input list
    unit["carry_weapon_ids"].append(999)

    assert weapon.raw_value == original_ids
    assert 999 not in weapon.raw_value


def test_raw_value_isolated_from_see_enemy_bop_ids_mutation():
    unit = _complete_unit()
    results = extract_unit_factor_evidence(unit)

    info = results[6]
    assert info.name == "information"
    original_ids = list(info.raw_value)

    unit["see_enemy_bop_ids"].clear()

    assert info.raw_value == original_ids
    assert len(info.raw_value) > 0


def test_raw_value_isolated_from_remain_bullet_nums_mutation():
    unit = _complete_unit(remain_bullet_nums={"42": 30})
    results = extract_unit_factor_evidence(unit)

    ammo = results[3]
    assert ammo.name == "ammo"
    assert ammo.raw_value == {"42": 30}

    # Mutate original dict in-place
    unit["remain_bullet_nums"]["42"] = 0
    unit["remain_bullet_nums"]["99"] = 10

    assert ammo.raw_value == {"42": 30}
    assert 99 not in ammo.raw_value


def test_tuple_containing_mutable_deep_copied():
    unit: dict = {"value": ([1, 2],)}
    results = extract_unit_factor_evidence(unit)

    base_value = results[0]
    assert base_value.name == "base_value"
    raw = base_value.raw_value
    assert isinstance(raw, tuple)
    assert raw[0] == [1, 2]

    # Mutate original nested list
    unit["value"][0].append(3)

    assert raw[0] == [1, 2]
    assert 3 not in raw[0]


def test_factor_evidence_fields_still_frozen():
    unit = _complete_unit()
    results = extract_unit_factor_evidence(unit)

    ev = results[0]
    with pytest.raises(FrozenInstanceError):
        ev.raw_value = "hacked"  # type: ignore[misc]
