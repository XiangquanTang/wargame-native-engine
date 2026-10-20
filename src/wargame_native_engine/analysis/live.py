"""Synchronous observer-only analysis for Source-compatible live sessions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from wargame_native_engine._framed_history import (
    append_private_record,
    clear_private_history,
    read_private_record,
)
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
from wargame_native_engine.core.time import (
    require_positive_milliseconds,
    resolve_delta_time_ms,
)


class LiveAnalysisDesynchronizedError(RuntimeError):
    """The environment has a GREEN frame missing from live analysis."""

    def __init__(self) -> None:
        super().__init__("live_analysis_desynchronized_reset_required")


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
    timeline: AnalysisTimeline | None


class LiveAnalyzedSession:
    """Wrap one Source environment without adding another advancement path."""

    def __init__(
        self,
        environment: SourceCompatibleEnvironment,
        *,
        analyzer: EightFactorAnalyzer | None = None,
        retain_update_timeline: bool = True,
        retain_frame_analyses: bool = True,
    ) -> None:
        if not isinstance(environment, SourceCompatibleEnvironment):
            raise TypeError("environment must be a SourceCompatibleEnvironment")
        self.environment = environment
        self.analyzer = analyzer if analyzer is not None else EightFactorAnalyzer()
        self._retain_update_timeline = bool(retain_update_timeline)
        self._retain_frame_analyses = bool(retain_frame_analyses)
        self._frame_analyses: list[FrameAnalysis] = []
        self._analysis_history = (
            None
            if self._retain_frame_analyses
            else tempfile.TemporaryFile(
                mode="w+b", prefix="wargame-live-analysis."
            )
        )
        self._analysis_offsets: list[tuple[int, int]] = []
        self._frame_count = 0
        self._analysis_identity = None
        self._timeline = build_analysis_timeline(())
        self._setup = False
        self._desynchronized = False

    def setup(self, seed: int = 42) -> LiveAnalysisUpdate:
        """Reset the underlying environment and analyze its initial GREEN frame."""

        bundle = self.environment.setup_views(seed)
        self._setup = True
        self._desynchronized = False
        try:
            self._frame_analyses.clear()
            self._analysis_offsets.clear()
            self._frame_count = 0
            self._analysis_identity = None
            if self._analysis_history is not None:
                clear_private_history(self._analysis_history)
            self._timeline = build_analysis_timeline(())
            committed = self._analyze_and_commit(bundle)
        except BaseException:
            self._desynchronized = True
            raise
        return self._build_update(bundle, done=None, committed=committed)

    def reset(self, seed: int = 42) -> LiveAnalysisUpdate:
        """Start a new analyzed episode; no prior analysis is retained."""

        return self.setup(seed)

    def step(
        self,
        actions: Sequence[EngineAction],
        *,
        delta_time_seconds: int | float | None = None,
        delta_time_ms: int | None = None,
    ) -> LiveAnalysisUpdate:
        """Advance exactly once, then analyze the returned detached GREEN frame.

        If analysis raises after ``step_views`` returns, the underlying step is
        already committed.  The original exception propagates and this wrapper
        rejects further steps until :meth:`setup` is called again.
        """

        milliseconds = resolve_delta_time_ms(
            delta_time_seconds=delta_time_seconds,
            delta_time_ms=delta_time_ms,
        )
        return self._step_ms(actions, delta_time_ms=milliseconds)

    def step_ms(
        self,
        actions: Sequence[EngineAction],
        *,
        delta_time_ms: int,
    ) -> LiveAnalysisUpdate:
        """Advance/analyze once using exact integer simulation milliseconds."""

        return self.step(
            actions,
            delta_time_ms=require_positive_milliseconds(delta_time_ms),
        )

    def _step_ms(
        self,
        actions: Sequence[EngineAction],
        *,
        delta_time_ms: int,
    ) -> LiveAnalysisUpdate:
        if not self._setup:
            raise RuntimeError("live_analysis_not_setup")
        self._require_synchronized()
        try:
            bundle, done = self.environment.step_views(
                actions, delta_time_ms=delta_time_ms
            )
        except BaseException:
            if self.environment.desynchronized:
                self._desynchronized = True
            raise
        try:
            committed = self._analyze_and_commit(bundle)
        except BaseException:
            self._desynchronized = True
            raise
        return self._build_update(bundle, done=done, committed=committed)

    @property
    def latest_analysis(self) -> FrameAnalysis | None:
        self._require_synchronized()
        if not self._frame_analyses:
            return None
        return deepcopy(self._frame_analyses[-1])

    @property
    def current_timeline(self) -> AnalysisTimeline:
        self._require_synchronized()
        if not self._retain_update_timeline:
            return build_analysis_timeline(self.frame_analyses)
        return deepcopy(self._timeline)

    @property
    def frame_analyses(self) -> tuple[FrameAnalysis, ...]:
        self._require_synchronized()
        if self._retain_frame_analyses:
            return deepcopy(tuple(self._frame_analyses))
        return tuple(
            self._load_frame_analysis(index)
            for index in range(self._frame_count)
        )

    def frame_analysis_at(self, frame_index: int) -> FrameAnalysis:
        """Return one detached analysis without copying the complete history."""

        self._require_synchronized()
        if type(frame_index) is not int or not 0 <= frame_index < self._frame_count:
            raise IndexError("analysis frame index out of range")
        value = (
            self._frame_analyses[frame_index]
            if self._retain_frame_analyses
            else self._load_frame_analysis(frame_index)
        )
        if value.provenance.frame_index != frame_index:
            raise RuntimeError("analysis frame sequence is not contiguous")
        return deepcopy(value)

    def analysis_jsonl(self) -> str:
        """Return the current episode's canonical analysis JSONL snapshot."""

        self._require_synchronized()
        return analysis_frames_to_jsonl(self.frame_analyses)

    def write_analysis_jsonl(self, path: str | Path) -> Path:
        """Stream canonical committed analyses to a new sidecar file."""

        self._require_synchronized()
        target = Path(path)
        with target.open("xb") as stream:
            for index in range(self._frame_count):
                frame = self.frame_analysis_at(index)
                stream.write(analysis_frames_to_jsonl((frame,)).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        return target

    def close(self) -> None:
        if self._analysis_history is not None:
            self._analysis_history.close()
            self._analysis_history = None
        self._analysis_offsets.clear()
        self._frame_analyses.clear()
        self._frame_count = 0

    @property
    def desynchronized(self) -> bool:
        """Constant-cost synchronization state; never materializes history."""

        return self._desynchronized or self.environment.desynchronized

    def _require_synchronized(self) -> None:
        if self.desynchronized:
            raise LiveAnalysisDesynchronizedError()

    def _analyze_and_commit(
        self,
        bundle: ObservationBundle,
    ) -> tuple[
        int, Mapping[str, Any], FrameAnalysis, AnalysisTimeline | None,
    ]:
        green = json_clone(bundle.green)
        perspective = green.get("extra", {}).get("native", {}).get("perspective")
        if perspective != "GREEN":
            raise ValueError("live analysis accepts GREEN frames only")
        frame_index = self._frame_count
        payload = canonical_green_frame_payload(green)
        frame_analysis = self.analyzer.analyze_green_frame(
            GreenFrameAnalysisInput(
                frame_index=frame_index,
                frame=green,
                frame_hash=hashlib.sha256(payload).hexdigest(),
                scenario_hash=self.environment.session.scenario.scenario_hash,
            )
        )
        identity = (
            frame_analysis.provenance.scenario_hash,
            frame_analysis.provenance.model_id,
            frame_analysis.provenance.model_version,
            frame_analysis.provenance.model_hash,
        )
        if self._analysis_identity is None:
            self._analysis_identity = identity
        elif identity != self._analysis_identity:
            raise ValueError("live analysis provenance changed within an episode")
        build_analysis_timeline((frame_analysis,))
        timeline = (
            build_analysis_timeline((*self.frame_analyses, frame_analysis))
            if self._retain_update_timeline
            else None
        )
        if self._retain_frame_analyses:
            self._frame_analyses.append(frame_analysis)
        else:
            self._store_frame_analysis(frame_analysis)
        self._frame_count += 1
        if timeline is not None:
            self._timeline = timeline
        return frame_index, green, frame_analysis, timeline

    def _store_frame_analysis(self, value: FrameAnalysis) -> None:
        if self._analysis_history is None:
            raise RuntimeError("live analysis history is unavailable")
        offset = append_private_record(self._analysis_history, value)
        self._analysis_offsets.append(offset)

    def _load_frame_analysis(self, frame_index: int) -> FrameAnalysis:
        if self._analysis_history is None:
            raise RuntimeError("live analysis history is unavailable")
        offset, size = self._analysis_offsets[frame_index]
        value = read_private_record(self._analysis_history, offset, size)
        if not isinstance(value, FrameAnalysis):
            raise RuntimeError("live analysis history has an invalid record")
        return value

    def _build_update(
        self,
        bundle: ObservationBundle,
        *,
        done: bool | None,
        committed: tuple[
            int,
            Mapping[str, Any],
            FrameAnalysis,
            AnalysisTimeline | None,
        ],
    ) -> LiveAnalysisUpdate:
        frame_index, green, frame_analysis, timeline = committed

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
            timeline=None if timeline is None else deepcopy(timeline),
        )
