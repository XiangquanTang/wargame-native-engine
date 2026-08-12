from __future__ import annotations

from copy import deepcopy

import pytest

from wargame_native_engine.analysis.models import (
    AdvantageAnalysis,
    AnalysisProvenance,
    AnalysisTimeline,
    FrameAnalysis,
)
from wargame_native_engine.analysis.timeline import build_analysis_timeline


def _frame(
    frame_index: int,
    *,
    raw: float | None,
    normalized: float | None,
    perspective: str = "GREEN",
    scenario_hash: str | None = "scenario-1",
    model_id: str = "native-eight-factor",
    model_version: str = "0.1.0-contract",
    model_hash: str | None = "model-1",
) -> FrameAnalysis:
    if raw is None or normalized is None:
        label = "UNKNOWN"
        warnings = ("advantage_unavailable",)
    elif raw > 0:
        label = "RED"
        warnings = ()
    elif raw < 0:
        label = "BLUE"
        warnings = ()
    else:
        label = "EVEN"
        warnings = ()

    return FrameAnalysis(
        provenance=AnalysisProvenance(
            perspective=perspective,
            frame_index=frame_index,
            frame_hash=f"frame-{frame_index}",
            scenario_hash=scenario_hash,
            model_id=model_id,
            model_version=model_version,
            model_hash=model_hash,
        ),
        unit_analyses=(),
        side_analyses=(),
        advantage=AdvantageAnalysis(
            raw_advantage=raw,
            normalized_advantage=normalized,
            label=label,
            confidence=None,
            warnings=warnings,
        ),
        warnings=(),
    )


def test_empty_sequence_produces_empty_timeline():
    result = build_analysis_timeline(())

    assert isinstance(result, AnalysisTimeline)
    assert result.frame_analyses == ()
    assert result.frame_indices == ()
    assert result.raw_advantages == ()
    assert result.normalized_advantages == ()
    assert result.delta_advantages == ()


def test_advantage_series_preserve_frame_order():
    frames = (
        _frame(0, raw=-20.0, normalized=-0.2),
        _frame(2, raw=10.0, normalized=0.1),
        _frame(5, raw=40.0, normalized=0.4),
    )

    result = build_analysis_timeline(frames)

    assert result.frame_analyses == frames
    assert result.frame_indices == (0, 2, 5)
    assert result.raw_advantages == (-20.0, 10.0, 40.0)
    assert result.normalized_advantages == (-0.2, 0.1, 0.4)
    assert result.delta_advantages[0] is None
    assert result.delta_advantages[1:] == pytest.approx((0.3, 0.3))


def test_first_delta_is_none():
    result = build_analysis_timeline((
        _frame(7, raw=50.0, normalized=0.5),
    ))

    assert result.delta_advantages == (None,)


def test_unknown_advantage_breaks_only_adjacent_deltas():
    result = build_analysis_timeline((
        _frame(0, raw=10.0, normalized=0.1),
        _frame(1, raw=None, normalized=None),
        _frame(2, raw=30.0, normalized=0.3),
        _frame(3, raw=40.0, normalized=0.4),
    ))

    assert result.raw_advantages == (10.0, None, 30.0, 40.0)
    assert result.normalized_advantages == (0.1, None, 0.3, 0.4)
    assert result.delta_advantages[:3] == (None, None, None)
    assert result.delta_advantages[3] == pytest.approx(0.1)


def test_list_input_is_frozen_to_tuple_without_modifying_input():
    frames = [
        _frame(0, raw=0.0, normalized=0.0),
        _frame(1, raw=10.0, normalized=0.1),
    ]
    before = deepcopy(frames)

    result = build_analysis_timeline(frames)

    assert frames == before
    assert isinstance(result.frame_analyses, tuple)


@pytest.mark.parametrize("bad_input", [None, 1, "frames", b"frames"])
def test_non_sequence_input_raises_typeerror(bad_input):
    with pytest.raises(TypeError, match="sequence"):
        build_analysis_timeline(bad_input)  # type: ignore[arg-type]


def test_non_frame_item_raises_typeerror():
    with pytest.raises(TypeError, match="position 1"):
        build_analysis_timeline((
            _frame(0, raw=0.0, normalized=0.0),
            object(),
        ))  # type: ignore[arg-type]


@pytest.mark.parametrize("indices", [(0, 0), (2, 1)])
def test_frame_indices_must_be_strictly_increasing(indices):
    frames = tuple(
        _frame(index, raw=float(index), normalized=0.0)
        for index in indices
    )

    with pytest.raises(ValueError, match="strictly increasing"):
        build_analysis_timeline(frames)


def test_non_contiguous_frame_indices_are_allowed():
    result = build_analysis_timeline((
        _frame(10, raw=1.0, normalized=0.1),
        _frame(20, raw=2.0, normalized=0.2),
    ))

    assert result.frame_indices == (10, 20)
    assert result.delta_advantages[1] == pytest.approx(0.1)


def test_non_green_frame_is_rejected():
    with pytest.raises(ValueError, match="GREEN"):
        build_analysis_timeline((
            _frame(0, raw=1.0, normalized=0.1, perspective="RED"),
        ))


@pytest.mark.parametrize(
    "changed",
    [
        {"scenario_hash": "scenario-2"},
        {"model_id": "other-model"},
        {"model_version": "2.0"},
        {"model_hash": "model-2"},
    ],
)
def test_mixed_scenario_or_model_provenance_is_rejected(changed):
    frames = (
        _frame(0, raw=1.0, normalized=0.1),
        _frame(1, raw=2.0, normalized=0.2, **changed),
    )

    with pytest.raises(ValueError, match="scenario and model provenance"):
        build_analysis_timeline(frames)


def test_repeated_construction_is_deterministic():
    frames = (
        _frame(0, raw=-10.0, normalized=-0.1),
        _frame(1, raw=20.0, normalized=0.2),
    )

    assert build_analysis_timeline(frames) == build_analysis_timeline(frames)


# ── Real GREEN-frame timeline tests ────────────────────────────────────
# These tests use build_green_frame_analysis to create real FrameAnalysis
# objects and feed them into build_analysis_timeline, verifying the full
# pipeline from GREEN frame dicts through to the advantage series.


from wargame_native_engine.analysis.green_frame_contract import (
    build_green_frame_analysis,
)


def _make_unit(obj_id: int, color: int, **overrides) -> dict:
    """Create a complete unit dict with all eight factor fields populated."""
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


def _make_frame(**overrides) -> dict:
    """Create a minimal GREEN frame dict with empty collections."""
    return {"operators": [], "passengers": [], **overrides}


def test_real_green_frames_to_timeline():
    """Build >=3 real FrameAnalysis via build_green_frame_analysis, then timeline.

    Verifies frame_indices, raw_advantages, normalized_advantages and
    adjacent normalized deltas through the full GREEN pipeline.
    """
    frames_input = [
        _make_frame(operators=[
            _make_unit(1, 0),  # red
            _make_unit(2, 1),  # blue
        ]),
        _make_frame(operators=[
            _make_unit(1, 0),
            _make_unit(2, 1),
            _make_unit(3, 0),  # second red → red-favouring
        ]),
        _make_frame(operators=[
            _make_unit(1, 0),
            _make_unit(2, 1),
        ]),
    ]

    analyses = [
        build_green_frame_analysis(f, frame_index=i)
        for i, f in enumerate(frames_input)
    ]

    result = build_analysis_timeline(analyses)

    # -- frame_indices match input order --
    assert result.frame_indices == (0, 1, 2)

    # -- raw_advantages and normalized_advantages are all present --
    assert all(ra is not None for ra in result.raw_advantages)
    assert all(na is not None for na in result.normalized_advantages)

    # -- first delta is always None --
    assert result.delta_advantages[0] is None

    # -- adjacent deltas are computed for valid frames --
    assert result.delta_advantages[1] is not None
    assert result.delta_advantages[2] is not None

    # -- delta == normalized[current] - normalized[previous] --
    for i in range(1, len(result.normalized_advantages)):
        prev = result.normalized_advantages[i - 1]
        cur = result.normalized_advantages[i]
        expected = cur - prev
        assert result.delta_advantages[i] == pytest.approx(expected)


def test_unknown_advantage_in_real_timeline():
    """UNKNOWN frame advantage produces None deltas on both adjacent edges.

    A frame where one side has no valid units yields UNKNOWN advantage;
    the timeline must not compute deltas across that boundary.
    """
    # Frame 0: complete red & blue → valid advantage
    f0 = _make_frame(operators=[
        _make_unit(1, 0),
        _make_unit(2, 1),
    ])

    # Frame 1: red unit missing 'blood' → health UNKNOWN →
    #          effective_power None → red side has no valid units →
    #          advantage UNKNOWN
    invalid_red = _make_unit(1, 0)
    del invalid_red["blood"]
    f1 = _make_frame(operators=[
        invalid_red,
        _make_unit(2, 1),  # blue complete → valid
    ])

    # Frame 2: complete red & blue → valid advantage again
    f2 = _make_frame(operators=[
        _make_unit(1, 0),
        _make_unit(2, 1),
    ])

    frames_input = [f0, f1, f2]
    analyses = [
        build_green_frame_analysis(f, frame_index=i)
        for i, f in enumerate(frames_input)
    ]

    result = build_analysis_timeline(analyses)

    # Frame 0 and 2 have valid advantages; frame 1 is UNKNOWN
    assert result.raw_advantages[0] is not None
    assert result.raw_advantages[1] is None
    assert result.raw_advantages[2] is not None
    assert result.normalized_advantages[0] is not None
    assert result.normalized_advantages[1] is None
    assert result.normalized_advantages[2] is not None

    # All three deltas must be None:
    #   delta[0] = None (always)
    #   delta[1] = None (adjacent to UNKNOWN normalized)
    #   delta[2] = None (adjacent to UNKNOWN normalized)
    assert result.delta_advantages == (None, None, None)


def test_input_frames_unmodified_after_timeline_pipeline():
    """Original GREEN frame dicts are not mutated after analysis + timeline."""
    frames_input = [
        _make_frame(operators=[
            _make_unit(1, 0),
            _make_unit(2, 1),
        ]),
        _make_frame(operators=[
            _make_unit(3, 0),
            _make_unit(4, 1),
        ]),
        _make_frame(operators=[
            _make_unit(5, 0),
            _make_unit(6, 1),
        ]),
    ]

    before = deepcopy(frames_input)

    analyses = [
        build_green_frame_analysis(f, frame_index=i)
        for i, f in enumerate(frames_input)
    ]
    build_analysis_timeline(analyses)

    assert frames_input == before


# ── normalized-delta and finite numeric contracts ─────────────────────


def test_delta_advantages_are_normalized_deltas_not_raw_deltas_or_rates():
    result = build_analysis_timeline((
        _frame(10, raw=100.0, normalized=0.25),
        _frame(20, raw=900.0, normalized=0.75),
    ))

    assert result.delta_advantages == (None, 0.5)
    assert result.delta_advantages[1] != 800.0


@pytest.mark.parametrize("field", ["raw", "normalized"])
@pytest.mark.parametrize("non_finite", [
    float("nan"), float("inf"), float("-inf"),
])
def test_timeline_rejects_non_finite_advantage_values(field, non_finite):
    values = {"raw": 1.0, "normalized": 0.1}
    values[field] = non_finite
    frame = _frame(0, **values)

    with pytest.raises(ValueError, match=f"timeline {field}_advantage"):
        build_analysis_timeline((frame,))


@pytest.mark.parametrize("field", ["raw", "normalized"])
def test_timeline_rejects_bool_advantage_values(field):
    values = {"raw": 1.0, "normalized": 0.1}
    values[field] = True
    frame = _frame(0, **values)

    with pytest.raises(TypeError, match=f"timeline {field}_advantage"):
        build_analysis_timeline((frame,))


def test_timeline_rejects_normalized_delta_overflow():
    frames = (
        _frame(0, raw=-1.0, normalized=-1e308),
        _frame(1, raw=1.0, normalized=1e308),
    )
    with pytest.raises(ValueError, match="timeline normalized advantage delta"):
        build_analysis_timeline(frames)


@pytest.mark.parametrize("bad_index", [True, 1.0, "1", None, -1])
def test_timeline_defensively_rejects_corrupted_frame_index(bad_index):
    frame = _frame(0, raw=0.0, normalized=0.0)
    object.__setattr__(frame.provenance, "frame_index", bad_index)
    error = TypeError if type(bad_index) is not int else ValueError

    with pytest.raises(error, match="frame_index"):
        build_analysis_timeline((frame,))
