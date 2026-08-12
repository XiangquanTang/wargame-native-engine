"""Derived analysis contracts for detached Native frame data."""

from wargame_native_engine.analysis.analyzer import (
    EightFactorAnalyzer,
    GreenFrameAnalysisInput,
)
from wargame_native_engine.analysis.events import detect_advantage_label_changes
from wargame_native_engine.analysis.jsonl import (
    analysis_frames_to_jsonl,
    analysis_sidecar_from_jsonl,
)
from wargame_native_engine.analysis.models import (
    AnalysisEvent,
    AnalysisSidecarLoadResult,
    AnalysisTimeline,
)
from wargame_native_engine.analysis.serialization import (
    ANALYSIS_SCHEMA,
    AnalysisSidecarFormatError,
    frame_analysis_from_record,
    frame_analysis_to_record,
)
from wargame_native_engine.analysis.timeline import build_analysis_timeline

__all__ = [
    "ANALYSIS_SCHEMA",
    "AnalysisEvent",
    "AnalysisSidecarFormatError",
    "AnalysisSidecarLoadResult",
    "AnalysisTimeline",
    "EightFactorAnalyzer",
    "GreenFrameAnalysisInput",
    "analysis_frames_to_jsonl",
    "analysis_sidecar_from_jsonl",
    "build_analysis_timeline",
    "detect_advantage_label_changes",
    "frame_analysis_from_record",
    "frame_analysis_to_record",
]
