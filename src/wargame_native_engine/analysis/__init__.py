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
from wargame_native_engine.analysis.live import (
    LiveAnalysisDesynchronizedError,
    LiveAnalyzedSession,
    LiveAnalysisUpdate,
)
from wargame_native_engine.analysis.models import (
    AnalysisEvent,
    AnalysisSidecarLoadResult,
    AnalysisTimeline,
)
from wargame_native_engine.analysis.replay_processor import (
    FRAME_ANALYSIS_JSONL_NAME,
    ReplayAnalysisResult,
    analyze_replay_to_jsonl,
)
from wargame_native_engine.analysis.serialization import (
    ANALYSIS_SCHEMA,
    AnalysisSidecarFormatError,
    frame_analysis_from_record,
    frame_analysis_to_record,
)
from wargame_native_engine.analysis.semantic_identity import (
    ANALYZER_MODEL_HASH,
    ANALYZER_MODEL_ID,
    ANALYZER_MODEL_VERSION,
    ANALYZER_SEMANTIC_SCHEMA_VERSION,
    DEFAULT_ANALYZER_AUTHORITY,
    AnalyzerAuthorityMetadata,
    analyzer_semantic_hash,
    analyzer_semantic_spec,
    require_analyzer_authority,
)
from wargame_native_engine.analysis.timeline import build_analysis_timeline

__all__ = [
    "ANALYSIS_SCHEMA",
    "ANALYZER_MODEL_HASH",
    "ANALYZER_MODEL_ID",
    "ANALYZER_MODEL_VERSION",
    "ANALYZER_SEMANTIC_SCHEMA_VERSION",
    "AnalyzerAuthorityMetadata",
    "AnalysisEvent",
    "AnalysisSidecarFormatError",
    "AnalysisSidecarLoadResult",
    "AnalysisTimeline",
    "EightFactorAnalyzer",
    "DEFAULT_ANALYZER_AUTHORITY",
    "FRAME_ANALYSIS_JSONL_NAME",
    "GreenFrameAnalysisInput",
    "LiveAnalysisDesynchronizedError",
    "LiveAnalyzedSession",
    "LiveAnalysisUpdate",
    "ReplayAnalysisResult",
    "analysis_frames_to_jsonl",
    "analyzer_semantic_hash",
    "analyzer_semantic_spec",
    "require_analyzer_authority",
    "analyze_replay_to_jsonl",
    "analysis_sidecar_from_jsonl",
    "build_analysis_timeline",
    "detect_advantage_label_changes",
    "frame_analysis_from_record",
    "frame_analysis_to_record",
]
