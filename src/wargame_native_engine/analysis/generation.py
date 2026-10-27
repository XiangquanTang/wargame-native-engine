"""Explicit model selection over the shared, read-only replay analysis pipeline.

Container format and analysis model are independent: a Native replay container
can still be evaluated with the unchanged Source contract for comparison.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from zipfile import ZIP_DEFLATED, ZipFile

from wargame_native_engine.analysis.analyzer import EightFactorAnalyzer, ReplayAnalyzer
from wargame_native_engine.analysis.replay_processor import (
    FRAME_ANALYSIS_JSONL_NAME,
    ReplayAnalysisResult,
    analyze_replay_to_jsonl,
)
from wargame_native_engine.io.importers.source_replay_archive import SourceReplayArchiveReader


@dataclass(frozen=True, slots=True)
class GeneratedReplayAnalysis:
    """Independent output bundle; no input facts or old sidecars are replaced."""

    analysis_model: str
    output_directory: Path
    analysis: ReplayAnalysisResult
    replay_path: Path | None


def generate_source_analysis(
    input_path: str | Path,
    output_directory: str | Path,
    *,
    source_format: str = "auto",
) -> GeneratedReplayAnalysis:
    """Run the original Source contract, including its strict UNKNOWN behavior."""

    return _generate(
        input_path, output_directory, source_format=source_format,
        analysis_model="source", analyzer=EightFactorAnalyzer(),
    )


def generate_native_analysis(
    input_path: str | Path,
    output_directory: str | Path,
    *,
    source_format: str = "auto",
) -> GeneratedReplayAnalysis:
    """Run the versioned Native conversion contract, never infer a model.

    A Native manifest is mandatory. The Native analyzer additionally validates
    per-unit authoritative evidence; a manifest alone does not supply facts.
    Unmodeled state remains UNKNOWN, including loss of control.
    """

    if source_format not in {"auto", "native"}:
        raise ValueError("Native analysis requires a Native replay container")
    source = Path(input_path)
    reader = SourceReplayArchiveReader(source)
    if reader.manifest.get("replay_format") != "native-source-frame-replay/1.0":
        raise ValueError("Native analysis requires an authoritative Native replay manifest")
    from wargame_native_engine.analysis.native_analyzer import NativeEightFactorAnalyzer

    return _generate(
        source, output_directory, source_format="native",
        analysis_model="native", analyzer=NativeEightFactorAnalyzer(),
    )


def _generate(
    input_path: str | Path,
    output_directory: str | Path,
    *,
    source_format: str,
    analysis_model: str,
    analyzer: ReplayAnalyzer,
) -> GeneratedReplayAnalysis:
    source = Path(input_path)
    output = Path(output_directory)
    source_root = source if source.is_dir() else source.parent
    if output.resolve().is_relative_to(source_root.resolve()):
        raise ValueError("output directory must be outside the input replay directory")
    # Exclusive creation also rejects empty directories: a generation is never
    # allowed to overwrite an earlier result or a user's partially copied run.
    output.mkdir(parents=True, exist_ok=False)
    try:
        result = analyze_replay_to_jsonl(
            source, output / FRAME_ANALYSIS_JSONL_NAME,
            source_format=source_format, analyzer=analyzer,
        )
        replay_path = None
        if result.source_format == "native":
            replay_path = output / "replay.zip"
            if source.is_file():
                shutil.copyfile(source, replay_path)
            else:
                # Keep every member's bytes, particularly numbered-frame hashes.
                # The surrounding ZIP is new, never a reserialization of facts.
                with ZipFile(replay_path, "w", compression=ZIP_DEFLATED) as archive:
                    for member in sorted(source.iterdir()):
                        if member.is_file():
                            archive.write(member, member.name)
        return GeneratedReplayAnalysis(analysis_model, output, result, replay_path)
    except Exception:
        # Only remove the new directory reserved by this call, never input data.
        shutil.rmtree(output)
        raise
