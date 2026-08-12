"""Pure JSONL text generation from ``FrameAnalysis`` sequences.

Produces a deterministic newline-delimited string — no file I/O,
no raw frame access, no sidecar writer.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from wargame_native_engine.analysis.models import (
    AnalysisEvent,
    AnalysisSidecarLoadResult,
    FrameAnalysis,
)
from wargame_native_engine.analysis.serialization import (
    AnalysisSidecarFormatError,
    _frame_analysis_from_record,
    frame_analysis_to_record,
)
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


def analysis_sidecar_from_jsonl(
    payload: str | bytes,
) -> AnalysisSidecarLoadResult:
    """Strictly parse canonical analysis JSONL without file or ZIP access.

    The payload must contain at least one object record, use LF line endings,
    and end with exactly the writer's required trailing LF.  Persisted frame
    analyses rebuild their timeline through :func:`build_analysis_timeline`;
    persisted events are returned unchanged in JSONL and line-local order.
    """

    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise AnalysisSidecarFormatError(
                f"invalid UTF-8: {exc}", path="$"
            ) from exc
    elif isinstance(payload, str):
        text = payload
    else:
        raise TypeError(
            "payload must be str or bytes, "
            f"got {type(payload).__name__}"
        )

    if not text or not text.strip():
        raise AnalysisSidecarFormatError(
            "payload must contain at least one record", path="$"
        )
    carriage_return = text.find("\r")
    if carriage_return >= 0:
        line_number = text.count("\n", 0, carriage_return) + 1
        raise AnalysisSidecarFormatError(
            "only canonical LF line endings are accepted",
            path="$",
            line_number=line_number,
        )
    if not text.endswith("\n"):
        raise AnalysisSidecarFormatError(
            "payload must end with a trailing LF", path="$"
        )

    lines = text[:-1].split("\n")
    frames: list[FrameAnalysis] = []
    events: list[AnalysisEvent] = []
    previous_index: int | None = None
    run_identity: tuple[str | None, str, str, str | None] | None = None

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise AnalysisSidecarFormatError(
                "blank JSONL lines are not allowed",
                path="$",
                line_number=line_number,
            )
        record = _json_object_from_line(line, line_number=line_number)
        frame, frame_events = _frame_analysis_from_record(
            record,
            line_number=line_number,
        )

        frame_index = frame.provenance.frame_index
        if previous_index is not None and frame_index <= previous_index:
            raise AnalysisSidecarFormatError(
                "frame indices must be strictly increasing in input order; "
                f"got {frame_index} after {previous_index}",
                path="$.provenance.frame_index",
                line_number=line_number,
            )
        previous_index = frame_index

        identity = (
            frame.provenance.scenario_hash,
            frame.provenance.model_id,
            frame.provenance.model_version,
            frame.provenance.model_hash,
        )
        if run_identity is None:
            run_identity = identity
        elif identity != run_identity:
            raise AnalysisSidecarFormatError(
                "all records must share scenario and model provenance",
                path="$.provenance",
                line_number=line_number,
            )

        frames.append(frame)
        events.extend(frame_events)

    try:
        timeline = build_analysis_timeline(tuple(frames))
    except (TypeError, ValueError) as exc:
        raise AnalysisSidecarFormatError(
            str(exc),
            path="$",
            line_number=len(lines),
        ) from exc

    return AnalysisSidecarLoadResult(
        timeline=timeline,
        events=tuple(events),
    )


class _DuplicateJsonKey(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


class _InvalidJsonConstant(ValueError):
    def __init__(self, value: str) -> None:
        self.value = value
        super().__init__(value)


def _json_object_from_line(line: str, *, line_number: int) -> dict[str, Any]:
    try:
        value = json.loads(
            line,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateJsonKey as exc:
        raise AnalysisSidecarFormatError(
            f"duplicate JSON object key {exc.key!r}",
            path="$",
            line_number=line_number,
        ) from exc
    except _InvalidJsonConstant as exc:
        raise AnalysisSidecarFormatError(
            f"non-finite JSON constant {exc.value!r} is not allowed",
            path="$",
            line_number=line_number,
        ) from exc
    except json.JSONDecodeError as exc:
        raise AnalysisSidecarFormatError(
            f"invalid JSON at column {exc.colno}: {exc.msg}",
            path="$",
            line_number=line_number,
        ) from exc

    if type(value) is not dict:
        raise AnalysisSidecarFormatError(
            f"expected top-level object, got {type(value).__name__}",
            path="$",
            line_number=line_number,
        )
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise _InvalidJsonConstant(value)
