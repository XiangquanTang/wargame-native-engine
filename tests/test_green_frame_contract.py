from __future__ import annotations

from copy import deepcopy

import pytest

from wargame_native_engine.analysis.eight_factor_contract import (
    FACTOR_ORDER,
    FactorStatus,
)
from wargame_native_engine.analysis.models import (
    AdvantageAnalysis,
    AnalysisProvenance,
    FactorValue,
    FrameAnalysis,
    SideAnalysis,
    UnitFactorAnalysis,
)
from wargame_native_engine.analysis.green_frame_contract import (
    build_green_frame_analysis,
)


# ── helpers ───────────────────────────────────────────────────────────


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


def _green_frame(**overrides) -> dict:
    return {
        "operators": [_unit(1, 0), _unit(2, 1)],
        "passengers": [],
        **overrides,
    }


def _find_unit(analysis: FrameAnalysis, operator_id: int) -> UnitFactorAnalysis:
    for ua in analysis.unit_analyses:
        if ua.operator_id == operator_id:
            return ua
    raise AssertionError(f"unit {operator_id} not found")


# ── 1. Complete GREEN frame → FrameAnalysis ───────────────────────────


def test_complete_green_frame_produces_frame_analysis():
    frame = _green_frame()
    result = build_green_frame_analysis(frame, frame_index=0)

    assert isinstance(result, FrameAnalysis)
    assert isinstance(result.provenance, AnalysisProvenance)
    assert len(result.unit_analyses) == 2
    assert len(result.side_analyses) == 2
    assert isinstance(result.advantage, AdvantageAnalysis)


# ── 2. Operators and passengers merged, sorted by obj_id ──────────────


def test_operators_and_passengers_merged_sorted():
    frame = _green_frame(
        operators=[_unit(3, 0), _unit(1, 0)],
        passengers=[_unit(2, 1), _unit(4, 1)],
    )
    result = build_green_frame_analysis(frame, frame_index=0)

    ids = [ua.operator_id for ua in result.unit_analyses]
    assert ids == [1, 2, 3, 4]


# ── 3. Red / blue side mapping ────────────────────────────────────────


def test_side_mapping():
    frame = _green_frame(
        operators=[
            _unit(1, 0),  # red
            _unit(2, 1),  # blue
            _unit(3, 2),  # UNKNOWN
            _unit(4, -1),  # UNKNOWN
        ],
        passengers=[],
    )
    result = build_green_frame_analysis(frame, frame_index=0)

    assert _find_unit(result, 1).side == "red"
    assert _find_unit(result, 2).side == "blue"
    assert _find_unit(result, 3).side == "UNKNOWN"
    assert _find_unit(result, 4).side == "UNKNOWN"


def test_missing_color_is_unknown():
    frame = _green_frame(
        operators=[{"obj_id": 99, "blood": 10, "max_blood": 10}],
        passengers=[],
    )
    result = build_green_frame_analysis(frame, frame_index=0)
    assert result.unit_analyses[0].side == "UNKNOWN"


# ── 4. Factor evidence order matches FACTOR_ORDER ─────────────────────


def test_factor_evidence_preserves_fixed_order():
    frame = _green_frame()
    result = build_green_frame_analysis(frame, frame_index=0)

    for ua in result.unit_analyses:
        names = [ev.name for ev in ua.factor_evidence]
        assert names == list(FACTOR_ORDER)


# ── 5. observed_factor_count correct ──────────────────────────────────


def test_observed_factor_count():
    frame = _green_frame()
    result = build_green_frame_analysis(frame, frame_index=0)

    for ua in result.unit_analyses:
        # All 8 factors populated — all should be OBSERVED
        assert ua.observed_factor_count == 8


def test_observed_factor_count_with_missing_fields():
    frame = _green_frame(
        operators=[{"obj_id": 1, "color": 0, "value": 100}],
        passengers=[],
    )
    result = build_green_frame_analysis(frame, frame_index=0)

    ua = result.unit_analyses[0]
    # Only base_value is present (value field); 7 factors are UNKNOWN
    # But actually health has blood & max_blood both missing → UNKNOWN
    # weapon: carry_weapon_ids missing → UNKNOWN
    # ammo, readiness, mobility, information, status all missing
    obs = [ev.name for ev in ua.factor_evidence
           if ev.status == FactorStatus.OBSERVED]
    assert ua.observed_factor_count == len(obs)


# ── 6. Missing value not default-filled ───────────────────────────────


def test_missing_value_not_defaulted():
    # Unit missing 'value' field entirely
    frame = _green_frame(
        operators=[_unit(1, 0, value=None)],
        passengers=[],
    )
    del frame["operators"][0]["value"]
    result = build_green_frame_analysis(frame, frame_index=0)

    ua = result.unit_analyses[0]
    bv_evidence = ua.factor_evidence[0]
    assert bv_evidence.name == "base_value"
    assert bv_evidence.status == FactorStatus.UNKNOWN

    bv_value = ua.factor_values[0]
    assert bv_value.value is None
    assert bv_value.status == FactorStatus.UNKNOWN


# ── 7. effective_power computed ──────────────────────────────────────


def test_effective_power_computed_for_complete_units():
    frame = _green_frame()
    result = build_green_frame_analysis(frame, frame_index=0)

    for ua in result.unit_analyses:
        assert ua.effective_power is not None
        assert ua.valid is True

    for sa in result.side_analyses:
        if sa.unit_count > 0:
            assert sa.effective_power is not None
            assert sa.coverage == 1.0


# ── 8. Advantage computed ────────────────────────────────────────────


def test_advantage_computed():
    frame = _green_frame()
    result = build_green_frame_analysis(frame, frame_index=0)

    adv = result.advantage
    assert adv.raw_advantage is not None
    assert adv.normalized_advantage is not None
    assert adv.confidence is None
    assert adv.label in ("RED", "BLUE", "EVEN")


# ── 9. Provenance fields pass through ─────────────────────────────────


def test_provenance_fields_preserved():
    frame = _green_frame()
    result = build_green_frame_analysis(
        frame,
        frame_index=42,
        frame_hash="abc123",
        scenario_hash="def456",
        model_id="test-model",
        model_version="9.9.9-test",
        model_hash="xyz789",
    )

    p = result.provenance
    assert p.perspective == "GREEN"
    assert p.frame_index == 42
    assert p.frame_hash == "abc123"
    assert p.scenario_hash == "def456"
    assert p.model_id == "test-model"
    assert p.model_version == "9.9.9-test"
    assert p.model_hash == "xyz789"


# ── 10. Duplicate obj_id raises ValueError ────────────────────────────


def test_duplicate_obj_id_raises_valueerror():
    frame = _green_frame(
        operators=[_unit(7, 0)],
        passengers=[_unit(7, 0)],
    )
    with pytest.raises(ValueError, match="duplicate obj_id"):
        build_green_frame_analysis(frame, frame_index=0)


# ── 11. Non-Mapping frame / unit raises TypeError ─────────────────────


def test_non_mapping_frame_raises_typeerror():
    with pytest.raises(TypeError):
        build_green_frame_analysis([1, 2, 3], frame_index=0)  # type: ignore[arg-type]


def test_non_sequence_collection_raises_typeerror():
    frame = {"operators": "not-a-list", "passengers": []}
    with pytest.raises(TypeError):
        build_green_frame_analysis(frame, frame_index=0)


def test_non_mapping_unit_raises_typeerror():
    frame = {"operators": [1, 2, 3], "passengers": []}
    with pytest.raises(TypeError):
        build_green_frame_analysis(frame, frame_index=0)


# ── 12. Input frame unmodified ────────────────────────────────────────


def test_input_frame_deeply_unchanged():
    frame = _green_frame(
        operators=[_unit(1, 0, carry_weapon_ids=[42, 43])],
        passengers=[],
    )
    before = deepcopy(frame)
    build_green_frame_analysis(frame, frame_index=0)
    assert frame == before


# ── 13. Empty frame → deterministic empty analysis ────────────────────


def test_empty_frame_produces_empty_analysis():
    frame: dict = {}
    result = build_green_frame_analysis(frame, frame_index=0)

    assert len(result.unit_analyses) == 0
    assert result.side_analyses[0].unit_count == 0
    assert result.side_analyses[1].unit_count == 0
    assert result.advantage.label == "EVEN"
    assert result.advantage.raw_advantage == 0.0


def test_empty_operators_and_passengers():
    frame = {"operators": [], "passengers": []}
    result = build_green_frame_analysis(frame, frame_index=0)

    assert len(result.unit_analyses) == 0
    assert result.side_analyses[0].unit_ids == ()
    assert result.side_analyses[1].unit_ids == ()


# ── 14. Side analyses are always (red, blue) ──────────────────────────


def test_side_analyses_fixed_order_red_blue():
    frame = _green_frame()
    result = build_green_frame_analysis(frame, frame_index=0)

    assert result.side_analyses[0].side == "red"
    assert result.side_analyses[1].side == "blue"


# ── 15. Missing collections treated as empty ──────────────────────────


def test_missing_operators_key_treated_as_empty():
    frame: dict = {"passengers": []}
    result = build_green_frame_analysis(frame, frame_index=0)
    assert len(result.unit_analyses) == 0


# ── 16. All eight factors resolved in complete frame ──────────────────


def test_all_eight_factors_resolved_in_complete_frame():
    frame = _green_frame()
    result = build_green_frame_analysis(frame, frame_index=0)

    for ua in result.unit_analyses:
        for fv in ua.factor_values:
            assert fv.value is not None, fv.factor_name
            assert fv.formula_id != "UNRESOLVED", fv.factor_name


# ── 17. Side unit_ids match actual units ──────────────────────────────


def test_side_unit_ids_match():
    frame = _green_frame(
        operators=[
            _unit(1, 0),
            _unit(2, 1),
            _unit(3, 0),
            _unit(4, 1),
        ],
        passengers=[_unit(5, 0)],
    )
    result = build_green_frame_analysis(frame, frame_index=0)

    red = result.side_analyses[0]
    blue = result.side_analyses[1]
    assert red.unit_ids == (1, 3, 5)
    assert blue.unit_ids == (2, 4)


# ── 18. Deterministic ordering ────────────────────────────────────────


def test_deterministic_ordering():
    frame = _green_frame()
    r1 = build_green_frame_analysis(frame, frame_index=0)
    r2 = build_green_frame_analysis(frame, frame_index=0)

    assert r1 == r2

    # Also verify identical structure across calls
    assert [ua.operator_id for ua in r1.unit_analyses] == \
           [ua.operator_id for ua in r2.unit_analyses]


# ── frame_index runtime contract ──────────────────────────────────────


@pytest.mark.parametrize("bad_index", [True, False, 1.0, "1", None])
def test_build_green_frame_rejects_non_int_frame_index(bad_index):
    with pytest.raises(TypeError, match="frame_index"):
        build_green_frame_analysis(
            _green_frame(),
            frame_index=bad_index,  # type: ignore[arg-type]
        )


def test_build_green_frame_rejects_negative_frame_index():
    with pytest.raises(ValueError, match="frame_index"):
        build_green_frame_analysis(_green_frame(), frame_index=-1)


@pytest.mark.parametrize("valid_index", [0, 1, 42])
def test_build_green_frame_accepts_non_negative_exact_int(valid_index):
    result = build_green_frame_analysis(
        _green_frame(),
        frame_index=valid_index,
    )
    assert result.provenance.frame_index == valid_index


@pytest.mark.parametrize("bad_index", [True, False, 1.0, "1", None])
def test_analysis_provenance_rejects_non_int_frame_index(bad_index):
    with pytest.raises(TypeError, match="frame_index"):
        AnalysisProvenance(
            perspective="GREEN",
            frame_index=bad_index,  # type: ignore[arg-type]
            frame_hash=None,
            scenario_hash=None,
            model_id="test",
            model_version="1",
            model_hash=None,
        )


def test_analysis_provenance_rejects_negative_frame_index():
    with pytest.raises(ValueError, match="frame_index"):
        AnalysisProvenance(
            perspective="GREEN",
            frame_index=-1,
            frame_hash=None,
            scenario_hash=None,
            model_id="test",
            model_version="1",
            model_hash=None,
        )
