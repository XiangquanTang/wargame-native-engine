from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from wargame_native_engine.analysis.events import (
    EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
)
from wargame_native_engine.analysis.green_frame_contract import (
    build_green_frame_analysis,
)
from wargame_native_engine.analysis.jsonl import analysis_frames_to_jsonl
from wargame_native_engine.analysis.models import AnalysisEvent


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
    return {"operators": [_unit(1, 0), _unit(2, 1)], "passengers": [], **overrides}


def _make_analysis(frame_index: int) -> object:
    """Shorthand: build a FrameAnalysis from a default GREEN frame."""
    return build_green_frame_analysis(_green_frame(), frame_index=frame_index)


# ── empty input ───────────────────────────────────────────────────────


def test_empty_frames_returns_empty_string():
    assert analysis_frames_to_jsonl(()) == ""


def test_empty_list_returns_empty_string():
    assert analysis_frames_to_jsonl([]) == ""


# ── single frame ──────────────────────────────────────────────────────


def test_single_frame_produces_one_line():
    frames = [_make_analysis(0)]
    result = analysis_frames_to_jsonl(frames)

    assert result.endswith("\n")
    lines = result.splitlines()
    assert len(lines) == 1

    # Line is valid JSON
    record = json.loads(lines[0])
    assert record["provenance"]["frame_index"] == 0


# ── multi-frame ───────────────────────────────────────────────────────


def test_multi_frame_produces_multiple_lines():
    frames = [
        _make_analysis(0),
        _make_analysis(1),
        _make_analysis(2),
    ]
    result = analysis_frames_to_jsonl(frames)

    assert result.endswith("\n")
    lines = result.splitlines()
    assert len(lines) == 3

    for i, line in enumerate(lines):
        record = json.loads(line)
        assert record["provenance"]["frame_index"] == i


# ── events grouped by frame_index ─────────────────────────────────────


def test_events_grouped_by_frame_index():
    frames = [
        _make_analysis(0),
        _make_analysis(1),
        _make_analysis(2),
    ]
    events: list[AnalysisEvent] = [
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=1,
            from_label="RED", to_label="BLUE",
            delta_advantage=-0.15,
        ),
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=1, frame_index=2,
            from_label="BLUE", to_label="EVEN",
            delta_advantage=0.05,
        ),
        # Second event arriving at frame 2
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=2,
            from_label="RED", to_label="EVEN",
            delta_advantage=-0.10,
        ),
    ]
    result = analysis_frames_to_jsonl(frames, events=events)
    lines = result.splitlines()

    # Frame 0: no events
    r0 = json.loads(lines[0])
    assert r0["events"] == []

    # Frame 1: one event (prev=0 → 1)
    r1 = json.loads(lines[1])
    assert len(r1["events"]) == 1
    assert r1["events"][0]["from_label"] == "RED"
    assert r1["events"][0]["to_label"] == "BLUE"

    # Frame 2: two events, sorted
    r2 = json.loads(lines[2])
    assert len(r2["events"]) == 2
    # Sorted by previous_frame_index → prev=0 first, prev=1 second
    assert r2["events"][0]["previous_frame_index"] == 0
    assert r2["events"][1]["previous_frame_index"] == 1


# ── event deduplication ───────────────────────────────────────────────


def test_identical_events_deduplicated():
    """Three copies of the same event → only one kept for that frame."""
    frames = [_make_analysis(0), _make_analysis(1)]
    # Three identical events all targeting frame 1
    events: list[AnalysisEvent] = [
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=1,
            from_label="RED", to_label="BLUE", delta_advantage=-0.1,
        )
        for _ in range(3)
    ]
    result = analysis_frames_to_jsonl(frames, events=events)
    lines = result.splitlines()

    # Frame 0: no events
    assert json.loads(lines[0])["events"] == []
    # Frame 1: exactly one event (duplicates removed)
    assert len(json.loads(lines[1])["events"]) == 1


def test_same_event_different_frame_index_both_kept():
    """Identical content but different frame_index → both kept."""
    frames = [_make_analysis(0), _make_analysis(1), _make_analysis(2)]
    events = [
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=1,
            from_label="EVEN", to_label="RED", delta_advantage=0.2,
        ),
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=2,
            from_label="EVEN", to_label="RED", delta_advantage=0.2,
        ),
    ]
    result = analysis_frames_to_jsonl(frames, events=events)
    lines = result.splitlines()

    # Frame 1: one event
    assert len(json.loads(lines[1])["events"]) == 1
    # Frame 2: one event (same content, different target)
    assert len(json.loads(lines[2])["events"]) == 1


def test_none_delta_advantage_deduplicated():
    """Two events with delta_advantage=None are considered duplicates."""
    frames = [_make_analysis(0), _make_analysis(1)]
    events = [
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=1,
            from_label="RED", to_label="BLUE", delta_advantage=None,
        ),
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=1,
            from_label="RED", to_label="BLUE", delta_advantage=None,
        ),
    ]
    result = analysis_frames_to_jsonl(frames, events=events)
    lines = result.splitlines()
    assert len(json.loads(lines[1])["events"]) == 1


def test_different_events_not_deduplicated():
    """Events differing in any key field are both kept."""
    frames = [_make_analysis(0), _make_analysis(1)]
    events = [
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=1,
            from_label="RED", to_label="BLUE", delta_advantage=-0.1,
        ),
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=1,
            from_label="RED", to_label="EVEN", delta_advantage=-0.05,
        ),
    ]
    result = analysis_frames_to_jsonl(frames, events=events)
    lines = result.splitlines()
    assert len(json.loads(lines[1])["events"]) == 2


def test_dedup_is_deterministic():
    """Repeated calls with duplicate events produce identical output."""
    frames = [_make_analysis(0), _make_analysis(1)]
    events = [
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=1,
            from_label="RED", to_label="BLUE", delta_advantage=-0.1,
        ),
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=1,
            from_label="RED", to_label="BLUE", delta_advantage=-0.1,
        ),
    ]
    r1 = analysis_frames_to_jsonl(frames, events=events)
    r2 = analysis_frames_to_jsonl(frames, events=events)
    assert r1 == r2


def test_event_with_unknown_frame_index_raises():
    frames = [_make_analysis(0), _make_analysis(1)]
    bad_event = AnalysisEvent(
        event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
        previous_frame_index=0, frame_index=99,
        from_label="RED", to_label="BLUE",
        delta_advantage=0.1,
    )

    with pytest.raises(ValueError, match="does not belong"):
        analysis_frames_to_jsonl(frames, events=[bad_event])


# ── non-contiguous frame indices ──────────────────────────────────────


def test_non_contiguous_frame_indices_accepted():
    frames = [
        _make_analysis(10),
        _make_analysis(25),
        _make_analysis(99),
    ]
    result = analysis_frames_to_jsonl(frames)

    lines = result.splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["provenance"]["frame_index"] == 10
    assert json.loads(lines[1])["provenance"]["frame_index"] == 25
    assert json.loads(lines[2])["provenance"]["frame_index"] == 99


def test_non_contiguous_event_targeting_middle_frame():
    frames = [
        _make_analysis(5),
        _make_analysis(10),
        _make_analysis(20),
    ]
    event = AnalysisEvent(
        event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
        previous_frame_index=5, frame_index=10,
        from_label="EVEN", to_label="RED",
        delta_advantage=0.3,
    )
    result = analysis_frames_to_jsonl(frames, events=[event])
    lines = result.splitlines()

    # Frame 5: no events
    assert json.loads(lines[0])["events"] == []
    # Frame 10: one event
    assert len(json.loads(lines[1])["events"]) == 1
    # Frame 20: no events
    assert json.loads(lines[2])["events"] == []


# ── JSON round-trip ───────────────────────────────────────────────────


def test_every_line_is_valid_json():
    frames = [_make_analysis(i) for i in range(5)]
    result = analysis_frames_to_jsonl(frames)

    for i, line in enumerate(result.splitlines()):
        record = json.loads(line)
        assert isinstance(record, dict)
        assert "analysis_schema" in record
        assert record["provenance"]["frame_index"] == i


# ── format: sort_keys, separators, trailing newline ───────────────────


def test_output_has_compact_separators():
    """json.dumps with separators=(",", ":") — no spaces."""
    frames = [_make_analysis(0)]
    result = analysis_frames_to_jsonl(frames)

    # Compact format: no ": " or ", "
    assert '": "' not in result  # no space after colon


def test_output_ends_with_exactly_one_newline():
    frames = [_make_analysis(0), _make_analysis(1)]
    result = analysis_frames_to_jsonl(frames)

    assert result.endswith("\n")
    assert not result.endswith("\n\n")


def test_sort_keys_is_stable():
    """Repeated calls with same input produce identical strings."""
    frames = [_make_analysis(0), _make_analysis(1)]
    r1 = analysis_frames_to_jsonl(frames)
    r2 = analysis_frames_to_jsonl(frames)
    assert r1 == r2

    # Specific check: keys in each line are sorted alphabetically
    for line in r1.splitlines():
        record = json.loads(line)
        keys = list(record.keys())
        assert keys == sorted(keys)


# ── GREEN / strict-increasing / provenance validation ─────────────────


def test_non_green_frame_rejected_by_timeline():
    from wargame_native_engine.analysis.models import (
        AdvantageAnalysis,
        AnalysisProvenance,
        FrameAnalysis,
    )

    bad_frame = FrameAnalysis(
        provenance=AnalysisProvenance(
            perspective="RED", frame_index=0, frame_hash=None,
            scenario_hash=None, model_id="test", model_version="1",
            model_hash=None,
        ),
        unit_analyses=(),
        side_analyses=(),
        advantage=AdvantageAnalysis(
            raw_advantage=0.0, normalized_advantage=0.0,
            label="EVEN", confidence=None, warnings=(),
        ),
        warnings=(),
    )

    with pytest.raises(ValueError, match="GREEN"):
        analysis_frames_to_jsonl([bad_frame])


def test_non_increasing_indices_rejected():
    frames = [_make_analysis(5), _make_analysis(3)]  # 3 after 5

    with pytest.raises(ValueError, match="strictly increasing"):
        analysis_frames_to_jsonl(frames)


# ── input unchanged & determinism ─────────────────────────────────────


def test_input_frames_not_modified():
    frames = [_make_analysis(0), _make_analysis(1)]
    before = deepcopy(frames)

    analysis_frames_to_jsonl(frames)

    assert frames == before


def test_input_events_not_modified():
    frames = [_make_analysis(0), _make_analysis(1)]
    events = [
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=1,
            from_label="RED", to_label="BLUE", delta_advantage=-0.1,
        ),
    ]
    before_events = deepcopy(events)

    analysis_frames_to_jsonl(frames, events=events)

    assert events == before_events


def test_deterministic_output():
    frames = [_make_analysis(0), _make_analysis(1), _make_analysis(2)]
    events = [
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=1,
            from_label="EVEN", to_label="RED", delta_advantage=0.2,
        ),
    ]

    r1 = analysis_frames_to_jsonl(frames, events=events)
    r2 = analysis_frames_to_jsonl(frames, events=events)

    assert r1 == r2
    assert isinstance(r1, str)


# ── events empty default ──────────────────────────────────────────────


def test_events_default_is_empty():
    frames = [_make_analysis(0), _make_analysis(1)]
    result = analysis_frames_to_jsonl(frames)

    for line in result.splitlines():
        assert json.loads(line)["events"] == []


# ── strict non-finite and raw-value serialization ─────────────────────


@pytest.mark.parametrize("non_finite", [
    float("nan"), float("inf"), float("-inf"),
])
def test_jsonl_rejects_non_finite_frame_analysis(non_finite):
    analysis = _make_analysis(0)
    bad_analysis = replace(
        analysis,
        advantage=replace(
            analysis.advantage,
            raw_advantage=non_finite,
        ),
    )

    with pytest.raises(ValueError, match="timeline raw_advantage"):
        analysis_frames_to_jsonl([bad_analysis])


@pytest.mark.parametrize("non_finite", [
    float("nan"), float("inf"), float("-inf"),
])
def test_jsonl_rejects_non_finite_event_delta(non_finite):
    event = AnalysisEvent(
        event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
        previous_frame_index=0,
        frame_index=0,
        from_label="EVEN",
        to_label="RED",
        delta_advantage=non_finite,
    )

    with pytest.raises(ValueError, match="Non-finite float"):
        analysis_frames_to_jsonl([_make_analysis(0)], events=[event])


def test_jsonl_rejects_unserializable_raw_value_without_repr_fallback():
    analysis = _make_analysis(0)
    first_unit = analysis.unit_analyses[0]
    first_evidence = first_unit.factor_evidence[0]
    bad_evidence = replace(first_evidence, raw_value={1, 2, 3})
    bad_unit = replace(
        first_unit,
        factor_evidence=(bad_evidence, *first_unit.factor_evidence[1:]),
    )
    bad_analysis = replace(
        analysis,
        unit_analyses=(bad_unit, *analysis.unit_analyses[1:]),
    )

    with pytest.raises(TypeError, match="JSON"):
        analysis_frames_to_jsonl([bad_analysis])
