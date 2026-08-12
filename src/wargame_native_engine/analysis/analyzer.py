"""Stateless in-memory orchestration for ordered detached GREEN frames."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from inspect import signature
from typing import Any, cast

from wargame_native_engine.analysis.green_frame_contract import (
    build_green_frame_analysis,
)
from wargame_native_engine.analysis.models import (
    AnalysisTimeline,
    _validate_frame_index,
)
from wargame_native_engine.analysis.timeline import build_analysis_timeline


_BUILDER_PARAMETERS = signature(build_green_frame_analysis).parameters
_DEFAULT_MODEL_ID = cast(str, _BUILDER_PARAMETERS["model_id"].default)
_DEFAULT_MODEL_VERSION = cast(
    str,
    _BUILDER_PARAMETERS["model_version"].default,
)
_DEFAULT_MODEL_HASH = cast(
    str | None,
    _BUILDER_PARAMETERS["model_hash"].default,
)


@dataclass(frozen=True, slots=True)
class GreenFrameAnalysisInput:
    """One caller-supplied detached GREEN frame and its provenance.

    The dataclass is immutable, but its caller-owned ``frame`` mapping is not
    deep-frozen.  The analyzer does not modify it or retain it as batch state.
    """

    frame_index: int
    frame: Mapping[str, Any]
    frame_hash: str | None = None
    scenario_hash: str | None = None

    def __post_init__(self) -> None:
        _validate_frame_index(self.frame_index)


@dataclass(frozen=True, slots=True)
class EightFactorAnalyzer:
    """Stateless batch orchestrator for existing GREEN analysis functions."""

    model_id: str = _DEFAULT_MODEL_ID
    model_version: str = _DEFAULT_MODEL_VERSION
    model_hash: str | None = _DEFAULT_MODEL_HASH

    def analyze_green_frames(
        self,
        inputs: Sequence[GreenFrameAnalysisInput],
    ) -> AnalysisTimeline:
        """Analyze ordered detached GREEN frames into one timeline.

        Input order remains authoritative.  The existing single-frame and
        timeline builders validate all facts, provenance and index ordering;
        their exceptions are intentionally allowed to propagate unchanged.
        """

        frame_analyses = tuple(
            build_green_frame_analysis(
                item.frame,
                frame_index=item.frame_index,
                frame_hash=item.frame_hash,
                scenario_hash=item.scenario_hash,
                model_id=self.model_id,
                model_version=self.model_version,
                model_hash=self.model_hash,
            )
            for item in inputs
        )
        return build_analysis_timeline(frame_analyses)
