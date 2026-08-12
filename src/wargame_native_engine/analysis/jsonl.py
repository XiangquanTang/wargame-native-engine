"""Pure JSONL text generation from ``FrameAnalysis`` sequences.

Produces a deterministic newline-delimited string — no file I/O,
no raw frame access, no sidecar writer.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from wargame_native_engine.analysis.models import AnalysisEvent, FrameAnalysis
from wargame_native_engine.analysis.serialization import frame_analysis_to_record
from wargame_native_engine.analysis.timeline import build_analysis_timeline


def analysis_frames_to_jsonl(
    frames: Sequence[FrameAnalysis],
    *,
    events: Sequence[AnalysisEvent] = (),
) -> str:
    """Convert *frames* into a deterministic JSONL string.

    1. Validates via ``build_analysis_timeline`` (GREEN-only, strictly
       increasing frame indices, consistent provenance).
    2. Groups *events* by ``frame_index``; events targeting a
       ``frame_index`` not present in *frames* raise ``ValueError``.
    3. Deduplicates events: two events with the same
       ``(previous_frame_index, frame_index, event_type, from_label,
       to_label, delta_advantage)`` tuple are considered identical;
       only the first occurrence is kept.
    4. Each frame is serialized with ``frame_analysis_to_record``.
    5. Records are formatted with ``json.dumps(..., separators=(",", ":"))``
       and joined by ``\\n``.
    6. An empty input produces ``""``; non-empty output ends with a
       trailing newline.
    """

    if len(frames) == 0:
        return ""

    # Validate: GREEN perspective, strictly increasing indices,
    # consistent provenance.
    timeline = build_analysis_timeline(frames)

    # Collect frame indices for event belonging validation.
    frame_index_set = {f.provenance.frame_index for f in frames}

    # Group events by frame_index; deduplicate by full identity key.
    events_by_frame: dict[int, list[AnalysisEvent]] = {
        idx: [] for idx in frame_index_set
    }
    seen: set[tuple[int, int, str, str, str, float | None]] = set()
    for event in events:
        if event.frame_index not in frame_index_set:
            raise ValueError(
                f"event.frame_index {event.frame_index} does not "
                f"belong to any input frame"
            )
        key = (
            event.previous_frame_index,
            event.frame_index,
            event.event_type,
            event.from_label,
            event.to_label,
            event.delta_advantage,
        )
        if key not in seen:
            seen.add(key)
            events_by_frame[event.frame_index].append(event)

    lines: list[str] = []
    for frame in timeline.frame_analyses:
        idx = frame.provenance.frame_index
        frame_events = tuple(events_by_frame.get(idx, ()))
        record = frame_analysis_to_record(frame, events=frame_events)
        line = json.dumps(
            record,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        lines.append(line)

    return "\n".join(lines) + "\n"
