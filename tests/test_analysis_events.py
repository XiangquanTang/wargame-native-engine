from __future__ import annotations

from copy import deepcopy

import pytest

from wargame_native_engine.analysis.events import (
    EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
    detect_advantage_label_changes,
)
from wargame_native_engine.analysis.models import (
    AdvantageAnalysis,
    AnalysisEvent,
    AnalysisProvenance,
    AnalysisTimeline,
    FrameAnalysis,
)
from wargame_native_engine.analysis.timeline import build_analysis_timeline


# ── helpers ───────────────────────────────────────────────────────────


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
        warnings: tuple[str, ...] = ("advantage_unavailable",)
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


def _timeline(*frames: FrameAnalysis) -> AnalysisTimeline:
    """Shorthand: build timeline from frames."""
    return build_analysis_timeline(frames)


# ── empty / single frame ──────────────────────────────────────────────


def test_empty_timeline_produces_no_events():
    assert detect_advantage_label_changes(_timeline()) == ()


def test_single_frame_produces_no_events():
    assert detect_advantage_label_changes(
        _timeline(_frame(0, raw=10.0, normalized=0.1))
    ) == ()


# ── label unchanged → no events ───────────────────────────────────────


@pytest.mark.parametrize("label_kwargs", [
    {"raw": 10.0, "normalized": 0.1},   # RED → RED
    {"raw": -10.0, "normalized": -0.1},  # BLUE → BLUE
    {"raw": 0.0, "normalized": 0.0},     # EVEN → EVEN
])
def test_same_label_produces_no_events(label_kwargs):
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, **label_kwargs),
            _frame(1, **label_kwargs),
        )
    )
    assert result == ()


# ── label changes → events ────────────────────────────────────────────


def test_red_to_blue_emits_event():
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, raw=10.0, normalized=0.1),
            _frame(1, raw=-5.0, normalized=-0.05),
        )
    )
    assert len(result) == 1
    e = result[0]
    assert e.event_type == EVENT_TYPE_ADVANTAGE_LABEL_CHANGE
    assert e.previous_frame_index == 0
    assert e.frame_index == 1
    assert e.from_label == "RED"
    assert e.to_label == "BLUE"
    assert e.delta_advantage == pytest.approx(-0.15)


def test_blue_to_red_emits_event():
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, raw=-5.0, normalized=-0.05),
            _frame(1, raw=10.0, normalized=0.1),
        )
    )
    assert len(result) == 1
    e = result[0]
    assert e.from_label == "BLUE"
    assert e.to_label == "RED"
    assert e.delta_advantage == pytest.approx(0.15)


def test_red_to_even_emits_event():
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, raw=10.0, normalized=0.1),
            _frame(1, raw=0.0, normalized=0.0),
        )
    )
    assert len(result) == 1
    e = result[0]
    assert e.from_label == "RED"
    assert e.to_label == "EVEN"
    assert e.delta_advantage == pytest.approx(-0.1)


def test_even_to_red_emits_event():
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, raw=0.0, normalized=0.0),
            _frame(1, raw=10.0, normalized=0.1),
        )
    )
    assert len(result) == 1
    e = result[0]
    assert e.from_label == "EVEN"
    assert e.to_label == "RED"
    assert e.delta_advantage == pytest.approx(0.1)


def test_blue_to_even_emits_event():
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, raw=-5.0, normalized=-0.05),
            _frame(1, raw=0.0, normalized=0.0),
        )
    )
    assert len(result) == 1
    e = result[0]
    assert e.from_label == "BLUE"
    assert e.to_label == "EVEN"
    assert e.delta_advantage == pytest.approx(0.05)


def test_even_to_blue_emits_event():
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, raw=0.0, normalized=0.0),
            _frame(1, raw=-5.0, normalized=-0.05),
        )
    )
    assert len(result) == 1
    e = result[0]
    assert e.from_label == "EVEN"
    assert e.to_label == "BLUE"
    assert e.delta_advantage == pytest.approx(-0.05)


# ── UNKNOWN boundaries → no events ────────────────────────────────────


def test_unknown_to_known_produces_no_event():
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, raw=None, normalized=None),
            _frame(1, raw=10.0, normalized=0.1),
        )
    )
    assert result == ()


def test_known_to_unknown_produces_no_event():
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, raw=10.0, normalized=0.1),
            _frame(1, raw=None, normalized=None),
        )
    )
    assert result == ()


def test_unknown_to_unknown_produces_no_event():
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, raw=None, normalized=None),
            _frame(1, raw=None, normalized=None),
        )
    )
    assert result == ()


def test_unknown_does_not_bridge_adjacent_valid_frames():
    """RED → UNKNOWN → BLUE: no events — detector never crosses UNKNOWN."""
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, raw=10.0, normalized=0.1),
            _frame(1, raw=None, normalized=None),
            _frame(2, raw=-5.0, normalized=-0.05),
        )
    )
    assert result == ()


def test_multiple_valid_after_unknown_are_detected():
    """UNKNOWN → RED → BLUE: only the RED→BLUE pair emits."""
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, raw=None, normalized=None),
            _frame(1, raw=10.0, normalized=0.1),
            _frame(2, raw=-5.0, normalized=-0.05),
        )
    )
    assert len(result) == 1
    assert result[0].from_label == "RED"
    assert result[0].to_label == "BLUE"


# ── non-contiguous frame indices ──────────────────────────────────────


def test_non_contiguous_frames_record_actual_indices():
    """Frame 10→25 label change records (10, 25) without inferring gaps."""
    result = detect_advantage_label_changes(
        _timeline(
            _frame(10, raw=10.0, normalized=0.1),
            _frame(25, raw=-5.0, normalized=-0.05),
        )
    )
    assert len(result) == 1
    e = result[0]
    assert e.previous_frame_index == 10
    assert e.frame_index == 25
    assert e.from_label == "RED"
    assert e.to_label == "BLUE"


def test_non_contiguous_with_no_change_produces_no_event():
    result = detect_advantage_label_changes(
        _timeline(
            _frame(5, raw=10.0, normalized=0.1),
            _frame(99, raw=20.0, normalized=0.2),
        )
    )
    assert result == ()


# ── multiple events in sequence ───────────────────────────────────────


def test_multiple_label_changes_in_sequence():
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, raw=10.0, normalized=0.1),   # RED
            _frame(1, raw=-5.0, normalized=-0.05),  # BLUE
            _frame(2, raw=0.0, normalized=0.0),     # EVEN
            _frame(3, raw=15.0, normalized=0.15),   # RED
        )
    )
    assert len(result) == 3
    assert result[0].from_label == "RED"
    assert result[0].to_label == "BLUE"
    assert result[1].from_label == "BLUE"
    assert result[1].to_label == "EVEN"
    assert result[2].from_label == "EVEN"
    assert result[2].to_label == "RED"


def test_events_ordered_by_current_frame_index():
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, raw=10.0, normalized=0.1),   # RED
            _frame(1, raw=-5.0, normalized=-0.05),  # BLUE
            _frame(2, raw=10.0, normalized=0.1),    # RED
        )
    )
    assert len(result) == 2
    assert result[0].frame_index == 1
    assert result[1].frame_index == 2


# ── immutability ──────────────────────────────────────────────────────


def test_analysis_event_is_immutable():
    e = AnalysisEvent(
        event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
        previous_frame_index=0,
        frame_index=1,
        from_label="RED",
        to_label="BLUE",
        delta_advantage=-0.15,
    )
    with pytest.raises(Exception):  # FrozenInstanceError or similar
        e.from_label = "EVEN"  # type: ignore[misc]


def test_analysis_event_is_hashable():
    e = AnalysisEvent(
        event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
        previous_frame_index=0,
        frame_index=1,
        from_label="RED",
        to_label="BLUE",
        delta_advantage=-0.15,
    )
    # If frozen and slots-based, instances are hashable.
    assert hash(e) is not None


# ── input unchanged & determinism ─────────────────────────────────────


def test_timeline_not_modified_by_detection():
    timeline = _timeline(
        _frame(0, raw=10.0, normalized=0.1),
        _frame(1, raw=-5.0, normalized=-0.05),
    )
    before = deepcopy(timeline)

    detect_advantage_label_changes(timeline)

    assert timeline == before


def test_detection_is_deterministic():
    timeline = _timeline(
        _frame(0, raw=10.0, normalized=0.1),
        _frame(1, raw=-5.0, normalized=-0.05),
        _frame(2, raw=0.0, normalized=0.0),
    )
    r1 = detect_advantage_label_changes(timeline)
    r2 = detect_advantage_label_changes(timeline)
    assert r1 == r2


# ── event tuple is a tuple ────────────────────────────────────────────


def test_result_is_tuple():
    result = detect_advantage_label_changes(
        _timeline(
            _frame(0, raw=10.0, normalized=0.1),
            _frame(1, raw=-5.0, normalized=-0.05),
        )
    )
    assert isinstance(result, tuple)


@pytest.mark.parametrize("bad_index", [True, False, 1.0, "1", None])
def test_analysis_event_rejects_non_int_frame_index(bad_index):
    with pytest.raises(TypeError, match="frame_index"):
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0,
            frame_index=bad_index,  # type: ignore[arg-type]
            from_label="RED",
            to_label="BLUE",
            delta_advantage=-0.1,
        )


def test_analysis_event_rejects_negative_frame_index():
    with pytest.raises(ValueError, match="frame_index"):
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0,
            frame_index=-1,
            from_label="RED",
            to_label="BLUE",
            delta_advantage=-0.1,
        )
