from __future__ import annotations

from copy import deepcopy

import pytest

from wargame_native_engine.analysis.advantage import (
    ADVANTAGE_FORMULA_ID,
    calculate_green_advantage,
)
from wargame_native_engine.analysis.green_frame_contract import (
    build_green_frame_analysis,
)
from wargame_native_engine.analysis.models import SideAnalysis


def _side(side: str, eff: float | None, *,
          coverage: float | None = 1.0,
          unit_count: int = 1,
          ) -> SideAnalysis:
    return SideAnalysis(
        side=side,
        unit_ids=(1,) if unit_count > 0 else (),
        unit_count=unit_count,
        observed_unit_count=unit_count,
        effective_power=eff,
        coverage=coverage,
        confidence=None,
        warnings=(),
    )


# ── 1. Red stronger → RED ────────────────────────────────────────────


def test_red_stronger():
    result = calculate_green_advantage(
        _side("red", 800.0), _side("blue", 200.0),
    )
    assert result.raw_advantage == 600.0
    assert result.normalized_advantage == 600.0 / 1000.0
    assert result.label == "RED"
    assert result.warnings == ()


# ── 2. Blue stronger → BLUE ──────────────────────────────────────────


def test_blue_stronger():
    result = calculate_green_advantage(
        _side("red", 200.0), _side("blue", 800.0),
    )
    assert result.raw_advantage == -600.0
    assert result.normalized_advantage == pytest.approx(-600.0 / 1000.0)
    assert result.label == "BLUE"


# ── 3. Equal → EVEN ──────────────────────────────────────────────────


def test_equal_even():
    result = calculate_green_advantage(
        _side("red", 500.0), _side("blue", 500.0),
    )
    assert result.raw_advantage == 0.0
    assert result.normalized_advantage == 0.0
    assert result.label == "EVEN"


# ── 4. Normalized formula for various ratios ──────────────────────────


def test_normalized_dominance():
    result = calculate_green_advantage(
        _side("red", 1000.0), _side("blue", 0.0),
    )
    assert result.normalized_advantage == 1.0
    assert result.label == "RED"


def test_normalized_dominated():
    result = calculate_green_advantage(
        _side("red", 0.0), _side("blue", 1000.0),
    )
    assert result.normalized_advantage == -1.0
    assert result.label == "BLUE"


# ── 5. Both zero → EVEN, normalized=0 ────────────────────────────────


def test_both_zero_even():
    result = calculate_green_advantage(
        _side("red", 0.0), _side("blue", 0.0),
    )
    assert result.raw_advantage == 0.0
    assert result.normalized_advantage == 0.0
    assert result.label == "EVEN"


# ── 6. Red effective_power=None → UNKNOWN ─────────────────────────────


def test_red_unavailable():
    result = calculate_green_advantage(
        _side("red", None), _side("blue", 500.0),
    )
    assert result.raw_advantage is None
    assert result.normalized_advantage is None
    assert result.label == "UNKNOWN"
    assert "advantage_unavailable" in result.warnings


# ── 7. Blue effective_power=None → UNKNOWN ────────────────────────────


def test_blue_unavailable():
    result = calculate_green_advantage(
        _side("red", 500.0), _side("blue", None),
    )
    assert result.label == "UNKNOWN"
    assert "advantage_unavailable" in result.warnings


# ── 8. Both unavailable → UNKNOWN ─────────────────────────────────────


def test_both_unavailable():
    result = calculate_green_advantage(
        _side("red", None), _side("blue", None),
    )
    assert result.label == "UNKNOWN"
    assert "advantage_unavailable" in result.warnings


# ── 9. Partial coverage → warning ─────────────────────────────────────


def test_partial_coverage_warning():
    result = calculate_green_advantage(
        _side("red", 500.0, coverage=0.5),
        _side("blue", 200.0, coverage=1.0),
    )
    assert result.label == "RED"
    assert "partial_side_coverage" in result.warnings


def test_partial_coverage_both_sides():
    result = calculate_green_advantage(
        _side("red", 500.0, coverage=0.5),
        _side("blue", 200.0, coverage=0.6),
    )
    assert "partial_side_coverage" in result.warnings


# ── 10. Full coverage → no warning ────────────────────────────────────


def test_full_coverage_no_warning():
    result = calculate_green_advantage(
        _side("red", 500.0, coverage=1.0),
        _side("blue", 200.0, coverage=1.0),
    )
    assert result.warnings == ()


# ── 11. Coverage=None (empty side) → no partial warning ───────────────


def test_empty_side_no_partial_warning():
    result = calculate_green_advantage(
        _side("red", 500.0, coverage=1.0),
        _side("blue", 0.0, coverage=None, unit_count=0),
    )
    # Empty side has coverage=None, which is not < 1.0
    assert result.label == "RED"
    assert "partial_side_coverage" not in result.warnings


# ── 12. Wrong side labels → ValueError ────────────────────────────────


def test_red_not_red_raises():
    with pytest.raises(ValueError, match="red.side"):
        calculate_green_advantage(
            _side("blue", 100.0), _side("blue", 100.0),
        )


def test_blue_not_blue_raises():
    with pytest.raises(ValueError, match="blue.side"):
        calculate_green_advantage(
            _side("red", 100.0), _side("red", 100.0),
        )


# ── 13. Input not modified ────────────────────────────────────────────


def test_input_not_modified():
    red = _side("red", 500.0)
    blue = _side("blue", 200.0)
    red_before = deepcopy(red)
    blue_before = deepcopy(blue)
    calculate_green_advantage(red, blue)
    assert red == red_before
    assert blue == blue_before


# ── 14. FrameAnalysis integration ─────────────────────────────────────


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


def test_frame_analysis_produces_advantage():
    frame = {"operators": [_unit(1, 0), _unit(2, 1)], "passengers": []}
    result = build_green_frame_analysis(frame, frame_index=0)

    adv = result.advantage
    assert adv.raw_advantage is not None
    assert adv.normalized_advantage is not None
    assert adv.label in ("RED", "BLUE", "EVEN")


def test_frame_analysis_symmetric_equal():
    """Two identical units on each side → EVEN."""
    frame = {
        "operators": [_unit(1, 0, value=5000), _unit(2, 1, value=5000)],
        "passengers": [],
    }
    result = build_green_frame_analysis(frame, frame_index=0)

    # Both units identical except for side → forces should be equal
    assert result.advantage.label == "EVEN"


def test_frame_analysis_red_stronger():
    frame = {
        "operators": [
            _unit(1, 0, value=10000),
            _unit(2, 1, value=1000),
        ],
        "passengers": [],
    }
    result = build_green_frame_analysis(frame, frame_index=0)
    assert result.advantage.label == "RED"
    assert result.advantage.raw_advantage > 0


def test_frame_analysis_partial_unknown_warning():
    # Red has 1 full unit + 1 partial; blue has 1 full unit.
    # Red coverage < 1.0 → partial_side_coverage.
    unit_partial = {"obj_id": 2, "color": 0, "type": 2, "sub_type": 7,
                    "cur_hex": 102, "value": 100}
    frame = {
        "operators": [_unit(1, 0), unit_partial, _unit(3, 1)],
        "passengers": [],
    }
    result = build_green_frame_analysis(frame, frame_index=0)
    assert "partial_side_coverage" in result.advantage.warnings


# ── 15. FrameAnalysis no advantage_unresolved ─────────────────────────


def test_frame_analysis_no_advantage_unresolved():
    frame = {"operators": [_unit(1, 0), _unit(2, 1)], "passengers": []}
    result = build_green_frame_analysis(frame, frame_index=0)
    assert "advantage_unresolved" not in result.warnings


# ── 16. Confidence still None ─────────────────────────────────────────


def test_confidence_still_none():
    result = calculate_green_advantage(
        _side("red", 500.0), _side("blue", 200.0),
    )
    assert result.confidence is None


# ── 17. FORMULA_ID constant ───────────────────────────────────────────


def test_formula_id_constant():
    assert ADVANTAGE_FORMULA_ID == "demo-compatible.green-relative-advantage.v1"


# ── 18. Both empty sides → EVEN ───────────────────────────────────────


def test_both_empty_sides_even():
    result = calculate_green_advantage(
        _side("red", 0.0, coverage=None, unit_count=0),
        _side("blue", 0.0, coverage=None, unit_count=0),
    )
    assert result.raw_advantage == 0.0
    assert result.normalized_advantage == 0.0
    assert result.label == "EVEN"


# ── 19. Destroyed forces → EVEN ────────────────────────────────────────


def test_destroyed_forces():
    """destroyed unit with E=0 on each side."""
    result = calculate_green_advantage(
        _side("red", 0.0), _side("blue", 0.0),
    )
    assert result.raw_advantage == 0.0
    assert result.normalized_advantage == 0.0
    assert result.label == "EVEN"


# ── finite numeric contract ───────────────────────────────────────────


@pytest.mark.parametrize("non_finite", [
    float("nan"), float("inf"), float("-inf"),
])
@pytest.mark.parametrize("side_name", ["red", "blue"])
def test_non_finite_side_power_rejected(non_finite, side_name):
    red_power = non_finite if side_name == "red" else 10.0
    blue_power = non_finite if side_name == "blue" else 10.0

    with pytest.raises(ValueError, match=f"advantage {side_name.upper()}"):
        calculate_green_advantage(
            _side("red", red_power),
            _side("blue", blue_power),
        )


def test_bool_side_power_rejected():
    with pytest.raises(TypeError, match="advantage RED effective_power"):
        calculate_green_advantage(
            _side("red", True),  # type: ignore[arg-type]
            _side("blue", 1.0),
        )


def test_non_finite_known_side_is_rejected_when_other_side_unknown():
    with pytest.raises(ValueError, match="advantage BLUE effective_power"):
        calculate_green_advantage(
            _side("red", None),
            _side("blue", float("inf")),
        )


def test_raw_advantage_overflow_rejected():
    with pytest.raises(ValueError, match="advantage raw_advantage"):
        calculate_green_advantage(
            _side("red", 1e308),
            _side("blue", -1e308),
        )


def test_total_power_denominator_overflow_rejected():
    with pytest.raises(ValueError, match="advantage total power denominator"):
        calculate_green_advantage(
            _side("red", 1e308),
            _side("blue", 1e308),
        )
