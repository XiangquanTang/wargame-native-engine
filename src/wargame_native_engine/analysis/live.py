"""Synchronous observer-only analysis for Source-compatible live sessions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
from typing import Any, Mapping, Sequence

from wargame_native_engine.analysis.analyzer import (
    EightFactorAnalyzer,
    GreenFrameAnalysisInput,
)
from wargame_native_engine.analysis.jsonl import analysis_frames_to_jsonl
from wargame_native_engine.analysis.models import AnalysisTimeline, FrameAnalysis
from wargame_native_engine.analysis.timeline import build_analysis_timeline
from wargame_native_engine.compat.source_frame.serialization import (
    canonical_green_frame_payload,
)
from wargame_native_engine.core.action import EngineAction
from wargame_native_engine.io.serialization import json_clone
from wargame_native_engine.observations.perspective import ObservationBundle
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)


@dataclass(frozen=True, slots=True)
class LiveAnalysisUpdate:
    """One completed Source environment operation and its current analysis.

    ``source_result`` contains detached RED, BLUE and GREEN views from the
    underlying setup or step.  ``done`` is ``None`` for setup and the exact
    underlying step result otherwise.  Returned mappings are detached copies;
    the dataclass itself does not claim to deep-freeze them.
    """

    frame_index: int
    source_result: ObservationBundle
    done: bool | None
    green_frame: Mapping[str, Any]
    frame_analysis: FrameAnalysis
    timeline: AnalysisTimeline


class LiveAnalyzedSession:
    """Wrap one Source environment without adding another advancement path."""

    def __init__(
        self,
        environment: SourceCompatibleEnvironment,
        *,
        analyzer: EightFactorAnalyzer | None = None,
    ) -> None:
        if not isinstance(environment, SourceCompatibleEnvironment):
            raise TypeError("environment must be a SourceCompatibleEnvironment")
        self.environment = environment
        self.analyzer = analyzer if analyzer is not None else EightFactorAnalyzer()
        self._frame_analyses: list[FrameAnalysis] = []
        self._timeline = build_analysis_timeline(())
        self._setup = False
        self._desynchronized = False

    def setup(self, seed: int = 42) -> LiveAnalysisUpdate:
        """Reset the underlying environment and analyze its initial GREEN frame."""

        bundle = self.environment.setup_views(seed)
        self._frame_analyses.clear()
        self._timeline = build_analysis_timeline(())
        self._setup = True
        self._desynchronized = False
        try:
            return self._append(bundle, done=None)
        except BaseException:
            self._desynchronized = True
            raise

    def reset(self, seed: int = 42) -> LiveAnalysisUpdate:
        """Start a new analyzed episode; no prior analysis is retained."""

        return self.setup(seed)

    def step(
        self,
        actions: Sequence[EngineAction],
        *,
        delta_time_seconds: int | float = 1,
    ) -> LiveAnalysisUpdate:
        """Advance exactly once, then analyze the returned detached GREEN frame.

        If analysis raises after ``step_views`` returns, the underlying step is
        already committed.  The original exception propagates and this wrapper
        rejects further steps until :meth:`setup` is called again.
        """

        if not self._setup:
            raise RuntimeError("live_analysis_not_setup")
        if self._desynchronized:
            raise RuntimeError("live_analysis_desynchronized_reset_required")
        bundle, done = self.environment.step_views(
            actions,
            delta_time_seconds=delta_time_seconds,
        )
        try:
            return self._append(bundle, done=done)
        except BaseException:
            self._desynchronized = True
            raise

    @property
    def latest_analysis(self) -> FrameAnalysis | None:
        if not self._frame_analyses:
            return None
        return deepcopy(self._frame_analyses[-1])

    @property
    def current_timeline(self) -> AnalysisTimeline:
        return deepcopy(self._timeline)

    @property
    def frame_analyses(self) -> tuple[FrameAnalysis, ...]:
        return deepcopy(tuple(self._frame_analyses))

    def analysis_jsonl(self) -> str:
        """Return the current episode's canonical analysis JSONL snapshot."""

        return analysis_frames_to_jsonl(tuple(self._frame_analyses))

    def _append(
        self,
        bundle: ObservationBundle,
        *,
        done: bool | None,
    ) -> LiveAnalysisUpdate:
        green = json_clone(bundle.green)
        perspective = green.get("extra", {}).get("native", {}).get("perspective")
        if perspective != "GREEN":
            raise ValueError("live analysis accepts GREEN frames only")
        frame_index = len(self._frame_analyses)
        payload = canonical_green_frame_payload(green)
        frame_analysis = self.analyzer.analyze_green_frame(
            GreenFrameAnalysisInput(
                frame_index=frame_index,
                frame=green,
                frame_hash=hashlib.sha256(payload).hexdigest(),
                scenario_hash=self.environment.session.scenario.scenario_hash,
            )
        )
        timeline = build_analysis_timeline(
            (*self._frame_analyses, frame_analysis)
        )
        self._frame_analyses.append(frame_analysis)
        self._timeline = timeline

        detached_bundle = ObservationBundle(
            red=json_clone(bundle.red),
            blue=json_clone(bundle.blue),
            green=json_clone(bundle.green),
        )
        return LiveAnalysisUpdate(
            frame_index=frame_index,
            source_result=detached_bundle,
            done=done,
            green_frame=json_clone(green),
            frame_analysis=deepcopy(frame_analysis),
            timeline=deepcopy(timeline),
        )
