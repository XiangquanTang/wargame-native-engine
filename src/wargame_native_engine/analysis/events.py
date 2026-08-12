"""Detection of discrete advantage-label change events from a timeline.

Only adjacent-frame comparisons are made.  UNKNOWN labels and missing
deltas suppress event generation — the detector never looks across an
UNKNOWN frame to a further valid one.
"""

from __future__ import annotations

from wargame_native_engine.analysis.models import AnalysisEvent, AnalysisTimeline

_VALID_LABELS = frozenset({"RED", "BLUE", "EVEN"})

EVENT_TYPE_ADVANTAGE_LABEL_CHANGE = "ADVANTAGE_LABEL_CHANGE"


def detect_advantage_label_changes(
    timeline: AnalysisTimeline,
) -> tuple[AnalysisEvent, ...]:
    """Detect adjacent-frame advantage label changes.

    An event is emitted for every adjacent pair where:

    * Both frames carry a known label (RED / BLUE / EVEN).
    * The labels differ.
    * The timeline's normalized delta for the transition is not
      ``None``.

    UNKNOWN frames do not participate — no event is generated on
    either edge adjacent to an UNKNOWN label, and the detector never
    looks across an UNKNOWN frame to reach a further valid one.

    Non-contiguous frame indices are recorded as-is; missing frames
    are not inferred.
    """

    frames = timeline.frame_analyses
    if len(frames) < 2:
        return ()

    deltas = timeline.delta_advantages
    events: list[AnalysisEvent] = []

    for i in range(1, len(frames)):
        from_label = frames[i - 1].advantage.label
        to_label = frames[i].advantage.label
        delta = deltas[i]

        if from_label not in _VALID_LABELS:
            continue
        if to_label not in _VALID_LABELS:
            continue
        if from_label == to_label:
            continue
        if delta is None:
            continue

        events.append(
            AnalysisEvent(
                event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
                previous_frame_index=frames[i - 1].provenance.frame_index,
                frame_index=frames[i].provenance.frame_index,
                from_label=from_label,
                to_label=to_label,
                delta_advantage=delta,
            )
        )

    return tuple(events)
