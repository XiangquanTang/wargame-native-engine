"""Reproducible, model-independent game evaluation without training dependencies."""

from wargame_native_engine.evaluation.manifest import BenchmarkManifest, load_manifest
from wargame_native_engine.evaluation.policy import (
    EpisodeContext,
    EvaluationPolicy,
    FirstLegalPolicy,
    NoOpPolicy,
)
from wargame_native_engine.evaluation.runner import run_benchmark

__all__ = [
    "BenchmarkManifest",
    "EpisodeContext",
    "EvaluationPolicy",
    "FirstLegalPolicy",
    "NoOpPolicy",
    "load_manifest",
    "run_benchmark",
]
