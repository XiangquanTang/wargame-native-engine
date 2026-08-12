"""Pure timeline construction from completed GREEN frame analyses.

This module does not read replays or frames, run factor formulas, inspect
events, or write sidecar data.  It only projects ordered ``FrameAnalysis``
values into immutable advantage series.
"""

from __future__ import annotations

from collections.abc import Sequence

from wargame_native_engine.analysis._numeric import require_finite_number
from wargame_native_engine.analysis.models import (
    AnalysisTimeline,
    FrameAnalysis,
    _validate_frame_index,
)


def build_analysis_timeline(
    frame_analyses: Sequence[FrameAnalysis],
) -> AnalysisTimeline:
    """Build an ordered GREEN ``AnalysisTimeline``.

    Input order is authoritative and must have strictly increasing frame
    indices.  Non-contiguous indices are allowed so sampled analyses remain
    representable without inferring omitted frames.  ``delta_advantages`` is
    the adjacent normalized-advantage difference only; it is neither a raw
    delta nor a rate, and frame indices are not treated as elapsed time.
    """

    if not isinstance(frame_analyses, Sequence) or isinstance(
        frame_analyses, (str, bytes, bytearray)
    ):
        raise TypeError("frame_analyses must be a sequence of FrameAnalysis")

    frames = tuple(frame_analyses)
    _validate_frames(frames)

    frame_indices = tuple(frame.provenance.frame_index for frame in frames)
    raw_advantages = tuple(
        frame.advantage.raw_advantage for frame in frames
    )
    normalized_advantages = tuple(
        frame.advantage.normalized_advantage for frame in frames
    )
    delta_advantages = _normalized_deltas(normalized_advantages)

    return AnalysisTimeline(
        frame_analyses=frames,
        frame_indices=frame_indices,
        raw_advantages=raw_advantages,
        normalized_advantages=normalized_advantages,
        delta_advantages=delta_advantages,
    )


def _validate_frames(frames: tuple[FrameAnalysis, ...]) -> None:
    previous_index: int | None = None
    run_identity: tuple[str | None, str, str, str | None] | None = None

    for position, frame in enumerate(frames):
        if not isinstance(frame, FrameAnalysis):
            raise TypeError(
                "every frame_analyses item must be a FrameAnalysis, "
                f"got {type(frame).__name__} at position {position}"
            )

        provenance = frame.provenance
        if provenance.perspective != "GREEN":
            raise ValueError(
                "AnalysisTimeline currently accepts GREEN analyses only, "
                f"got {provenance.perspective!r} at position {position}"
            )

        frame_index = provenance.frame_index
        _validate_frame_index(frame_index)
        if previous_index is not None and frame_index <= previous_index:
            raise ValueError(
                "frame indices must be strictly increasing in input order, "
                f"got {frame_index} after {previous_index}"
            )
        previous_index = frame_index

        raw_advantage = frame.advantage.raw_advantage
        if raw_advantage is not None:
            require_finite_number(
                raw_advantage,
                stage=f"timeline raw_advantage at frame_index {frame_index}",
            )
        normalized_advantage = frame.advantage.normalized_advantage
        if normalized_advantage is not None:
            require_finite_number(
                normalized_advantage,
                stage=(
                    "timeline normalized_advantage at "
                    f"frame_index {frame_index}"
                ),
            )

        identity = (
            provenance.scenario_hash,
            provenance.model_id,
            provenance.model_version,
            provenance.model_hash,
        )
        if run_identity is None:
            run_identity = identity
        elif identity != run_identity:
            raise ValueError(
                "all frame analyses must share scenario and model provenance"
            )


def _normalized_deltas(
    normalized_advantages: tuple[float | None, ...],
) -> tuple[float | None, ...]:
    if not normalized_advantages:
        return ()

    deltas: list[float | None] = [None]
    for previous, current in zip(
        normalized_advantages, normalized_advantages[1:]
    ):
        if previous is None or current is None:
            deltas.append(None)
        else:
            delta = current - previous
            require_finite_number(
                delta,
                stage="timeline normalized advantage delta",
            )
            deltas.append(delta)
    return tuple(deltas)
