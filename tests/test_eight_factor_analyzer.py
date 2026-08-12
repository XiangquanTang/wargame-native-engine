from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from wargame_native_engine.analysis import (
    EightFactorAnalyzer,
    GreenFrameAnalysisInput,
)
from wargame_native_engine.analysis.green_frame_contract import (
    build_green_frame_analysis,
)
from wargame_native_engine.analysis.models import FrameAnalysis
from wargame_native_engine.analysis.timeline import build_analysis_timeline


def _unit(obj_id: int, color: int, **overrides: object) -> dict[str, object]:
    unit: dict[str, object] = {
        "obj_id": obj_id,
        "color": color,
        "value": 100.0,
        "blood": 100.0,
        "max_blood": 100.0,
        "carry_weapon_ids": [36],
        "remain_bullet_nums": {"36": 100},
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
    unit.update(overrides)
    return unit


def _frame(*units: dict[str, object]) -> dict[str, object]:
    return {
        "operators": list(units),
        "passengers": [],
        "metadata": {"nested": ["caller-owned"]},
    }


def _input(
    frame_index: int,
    frame: dict[str, object],
    *,
    frame_hash: str | None = None,
    scenario_hash: str | None = "scenario-1",
) -> GreenFrameAnalysisInput:
    return GreenFrameAnalysisInput(
        frame_index=frame_index,
        frame=frame,
        frame_hash=frame_hash,
        scenario_hash=scenario_hash,
    )


def _manual_timeline(
    inputs: tuple[GreenFrameAnalysisInput, ...],
    analyzer: EightFactorAnalyzer,
):
    analyses = tuple(
        build_green_frame_analysis(
            item.frame,
            frame_index=item.frame_index,
            frame_hash=item.frame_hash,
            scenario_hash=item.scenario_hash,
            model_id=analyzer.model_id,
            model_version=analyzer.model_version,
            model_hash=analyzer.model_hash,
        )
        for item in inputs
    )
    return build_analysis_timeline(analyses)


def test_empty_batch_equals_existing_empty_timeline():
    result = EightFactorAnalyzer().analyze_green_frames(())

    assert result == build_analysis_timeline(())
    assert result.frame_analyses == ()


def test_single_frame_has_analysis_delta_and_complete_provenance():
    analyzer = EightFactorAnalyzer(
        model_id="model-custom",
        model_version="2.0-test",
        model_hash="model-hash",
    )
    item = _input(
        7,
        _frame(_unit(1, 0), _unit(2, 1)),
        frame_hash="frame-hash-7",
        scenario_hash="scenario-hash",
    )

    result = analyzer.analyze_green_frames((item,))

    assert len(result.frame_analyses) == 1
    assert isinstance(result.frame_analyses[0], FrameAnalysis)
    assert result.frame_indices == (7,)
    assert result.delta_advantages == (None,)
    provenance = result.frame_analyses[0].provenance
    assert provenance.frame_hash == "frame-hash-7"
    assert provenance.scenario_hash == "scenario-hash"
    assert provenance.model_id == "model-custom"
    assert provenance.model_version == "2.0-test"
    assert provenance.model_hash == "model-hash"


def test_multiple_frames_equal_manual_public_function_composition():
    analyzer = EightFactorAnalyzer(
        model_id="model-custom",
        model_version="2.0-test",
        model_hash="model-hash",
    )
    inputs = (
        _input(0, _frame(_unit(1, 0), _unit(2, 1)), frame_hash="f0"),
        _input(
            2,
            _frame(_unit(1, 0, value=300.0), _unit(2, 1)),
            frame_hash="f2",
        ),
        _input(
            5,
            _frame(_unit(1, 0), _unit(2, 1, value=400.0)),
            frame_hash="f5",
        ),
    )

    result = analyzer.analyze_green_frames(inputs)
    expected = _manual_timeline(inputs, analyzer)

    assert result == expected
    assert result.frame_indices == (0, 2, 5)
    assert result.raw_advantages == expected.raw_advantages
    assert result.normalized_advantages == expected.normalized_advantages
    assert result.delta_advantages == expected.delta_advantages


@pytest.mark.parametrize("indices", [(1, 1), (2, 1)])
def test_duplicate_or_out_of_order_indices_use_timeline_error(indices):
    inputs = tuple(
        _input(index, _frame(_unit(position + 1, 0)))
        for position, index in enumerate(indices)
    )

    with pytest.raises(ValueError, match="strictly increasing"):
        EightFactorAnalyzer().analyze_green_frames(inputs)


def test_strictly_increasing_indices_with_gaps_are_preserved():
    inputs = (
        _input(3, _frame(_unit(1, 0))),
        _input(100, _frame(_unit(2, 0))),
    )

    result = EightFactorAnalyzer().analyze_green_frames(inputs)

    assert result.frame_indices == (3, 100)


@pytest.mark.parametrize("bad_index", [True, False, 1.0, "1"])
def test_non_exact_int_frame_index_rejected_by_shared_contract(bad_index):
    with pytest.raises(TypeError, match="frame_index"):
        GreenFrameAnalysisInput(
            frame_index=bad_index,  # type: ignore[arg-type]
            frame={},
        )


def test_negative_frame_index_rejected_by_shared_contract():
    with pytest.raises(ValueError, match="frame_index"):
        GreenFrameAnalysisInput(frame_index=-1, frame={})


def test_unknown_advantage_delta_propagates_then_recovers():
    incomplete_red = _unit(1, 0)
    del incomplete_red["blood"]
    inputs = (
        _input(0, _frame(_unit(1, 0), _unit(2, 1))),
        _input(1, _frame(incomplete_red, _unit(2, 1))),
        _input(2, _frame(_unit(1, 0), _unit(2, 1))),
        _input(3, _frame(_unit(1, 0, value=300.0), _unit(2, 1))),
    )
    analyzer = EightFactorAnalyzer()

    result = analyzer.analyze_green_frames(inputs)
    expected = _manual_timeline(inputs, analyzer)

    assert result == expected
    assert result.normalized_advantages[1] is None
    assert result.delta_advantages[:3] == (None, None, None)
    assert result.delta_advantages[3] is not None


def test_per_frame_hashes_and_scenario_hashes_pass_through_unchanged():
    inputs = (
        _input(
            0,
            _frame(_unit(1, 0)),
            frame_hash="",
            scenario_hash="scenario-exact",
        ),
        _input(
            1,
            _frame(_unit(2, 1)),
            frame_hash=None,
            scenario_hash="scenario-exact",
        ),
    )

    result = EightFactorAnalyzer().analyze_green_frames(inputs)
    provenances = tuple(frame.provenance for frame in result.frame_analyses)

    assert tuple(p.frame_hash for p in provenances) == ("", None)
    assert tuple(p.scenario_hash for p in provenances) == (
        "scenario-exact",
        "scenario-exact",
    )


def test_default_analyzer_model_provenance_matches_single_frame_builder():
    frame = _frame(_unit(1, 0), _unit(2, 1))
    analyzer_result = EightFactorAnalyzer().analyze_green_frames((
        _input(0, frame, scenario_hash=None),
    ))
    direct_result = build_green_frame_analysis(frame, frame_index=0)

    assert analyzer_result.frame_analyses[0].provenance == direct_result.provenance


def test_input_sequence_descriptors_and_nested_frames_are_unchanged():
    frames = [
        _frame(_unit(1, 0, carry_weapon_ids=[36, 1])),
        _frame(_unit(2, 1, remain_bullet_nums={"36": 7})),
    ]
    inputs = [
        _input(0, frames[0], frame_hash="f0"),
        _input(1, frames[1], frame_hash="f1"),
    ]
    frames_before = deepcopy(frames)
    inputs_before = tuple(inputs)

    EightFactorAnalyzer().analyze_green_frames(inputs)

    assert frames == frames_before
    assert tuple(inputs) == inputs_before
    with pytest.raises(FrozenInstanceError):
        inputs[0].frame_index = 9  # type: ignore[misc]


def test_repeated_calls_are_deterministic_and_batches_do_not_accumulate():
    analyzer = EightFactorAnalyzer()
    first_inputs = (
        _input(0, _frame(_unit(1, 0))),
        _input(1, _frame(_unit(2, 1))),
    )
    second_inputs = (
        _input(9, _frame(_unit(9, 0))),
    )

    first = analyzer.analyze_green_frames(first_inputs)
    repeated = analyzer.analyze_green_frames(first_inputs)
    second = analyzer.analyze_green_frames(second_inputs)

    assert first == repeated
    assert first.frame_indices == (0, 1)
    assert second.frame_indices == (9,)
    assert len(second.frame_analyses) == 1


def test_failed_batch_does_not_pollute_following_valid_call():
    analyzer = EightFactorAnalyzer()
    invalid = (
        _input(2, _frame(_unit(1, 0))),
        _input(1, _frame(_unit(2, 1))),
    )

    with pytest.raises(ValueError, match="strictly increasing"):
        analyzer.analyze_green_frames(invalid)

    valid = analyzer.analyze_green_frames((
        _input(4, _frame(_unit(4, 0))),
    ))
    assert valid.frame_indices == (4,)


def test_single_frame_contract_exception_propagates_unchanged():
    duplicate_frame = _frame(_unit(7, 0), _unit(7, 1))
    item = _input(0, duplicate_frame)

    with pytest.raises(ValueError, match="duplicate obj_id 7"):
        EightFactorAnalyzer().analyze_green_frames((item,))
