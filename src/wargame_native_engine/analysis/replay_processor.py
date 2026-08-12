"""Offline orchestration from replay importers to derived analysis JSONL."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from zipfile import ZipFile

from wargame_native_engine.analysis.analyzer import (
    EightFactorAnalyzer,
    GreenFrameAnalysisInput,
)
from wargame_native_engine.analysis.jsonl import analysis_frames_to_jsonl
from wargame_native_engine.compat.source_frame.models import SourceFrame
from wargame_native_engine.io.importers.historical_replay import (
    HistoricalOperatorState,
    HistoricalReplayImporter,
)
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.serialization import json_clone


FRAME_ANALYSIS_JSONL_NAME = "frame_analysis.native.jsonl"
_NUMBERED_FRAME_NAME = re.compile(r"^(0|[1-9][0-9]*)\.json$")
_SOURCE_FORMATS = frozenset({"auto", "native", "historical"})
_NATIVE_MANIFEST_NAME = "replay_manifest.native.jsonl"
_NATIVE_REPLAY_FORMAT = "native-source-frame-replay/1.0"
_HISTORICAL_MANIFEST_SCHEMAS = frozenset({
    "historical-replay-source/1.0",
    "game-session-replay/1.0",
})
_HISTORICAL_INDEX_SCHEMA = "historical-replay-fixture-index/1.0"
_NORMALIZED_HISTORICAL_SCHEMA = "historical-replay/1.0"
_DIRECT_FACTOR_FIELDS = (
    "value",
    "blood",
    "max_blood",
    "carry_weapon_ids",
    "remain_bullet_nums",
    "weapon_cool_time",
    "weapon_unfold_state",
    "move_state",
    "stop",
    "can_to_move",
    "on_board",
    "see_enemy_bop_ids",
    "keep",
    "tire",
    "lose_control",
    "close_combat",
)


@dataclass(frozen=True, slots=True)
class ReplayAnalysisResult:
    """Summary of one replay converted to a derived analysis JSONL file."""

    input_path: Path
    output_path: Path
    frame_count: int
    source_format: str


def analyze_replay_to_jsonl(
    input_path: str | Path,
    output_path: str | Path,
    *,
    source_format: str = "auto",
    analyzer: EightFactorAnalyzer | None = None,
    overwrite: bool = False,
) -> ReplayAnalysisResult:
    """Analyze one Native or historical replay into an independent JSONL file.

    Replay facts are read through the existing importers and are never changed.
    Analysis and serialization complete in memory before a temporary output is
    created and atomically published.
    """

    source = Path(input_path)
    output = Path(output_path)
    if source_format not in _SOURCE_FORMATS:
        raise ValueError(
            "source_format must be one of: auto, native, historical"
        )
    if output.exists() and not overwrite:
        raise FileExistsError(f"analysis output already exists: {output}")
    _validate_output_is_separate(source, output)

    detected_format, inputs = _load_analysis_inputs(source, source_format)
    if not inputs:
        raise ValueError("replay contains no analyzable frames")

    configured_analyzer = analyzer if analyzer is not None else EightFactorAnalyzer()
    timeline = configured_analyzer.analyze_green_frames(inputs)
    payload = analysis_frames_to_jsonl(timeline.frame_analyses).encode("utf-8")
    if not payload:
        raise ValueError("replay analysis produced empty JSONL")

    _atomic_write(output, payload, overwrite=overwrite)
    return ReplayAnalysisResult(
        input_path=source,
        output_path=output,
        frame_count=len(timeline.frame_analyses),
        source_format=detected_format,
    )


def _load_analysis_inputs(
    source: Path,
    requested_format: str,
) -> tuple[str, tuple[GreenFrameAnalysisInput, ...]]:
    detected_format = requested_format
    if requested_format == "auto":
        detected_format = _detect_source_format(source)
    if detected_format == "native":
        return "native", _native_analysis_inputs(source)
    return "historical", _historical_analysis_inputs(source)


def _detect_source_format(source: Path) -> str:
    if source.is_dir():
        paths = {
            item.name: item
            for item in source.iterdir()
            if item.is_file()
        }
        marker_names = set(paths) & {
            _NATIVE_MANIFEST_NAME,
            "manifest.json",
            "index.json",
        }
        members = {name: paths[name].read_bytes() for name in marker_names}
        for name in paths:
            if _NUMBERED_FRAME_NAME.fullmatch(name):
                members[name] = b""
        return _detect_from_members(members, archive=False)
    if source.is_file() and source.suffix.lower() == ".zip":
        with ZipFile(source) as archive:
            names = {
                name for name in archive.namelist()
                if "/" not in name and not name.endswith("/")
            }
            marker_names = names & {
                _NATIVE_MANIFEST_NAME,
                "manifest.json",
                "index.json",
            }
            members = {
                name: archive.read(name)
                for name in marker_names
            }
            for name in names:
                if _NUMBERED_FRAME_NAME.fullmatch(name):
                    members[name] = b""
        return _detect_from_members(members, archive=True)
    if source.is_file():
        value = _json_object(source.read_bytes(), source.name)
        if value.get("schema_version") == _NORMALIZED_HISTORICAL_SCHEMA:
            return "historical"
    raise ValueError(f"unable to detect replay format: {source}")


def _detect_from_members(
    members: Mapping[str, bytes],
    *,
    archive: bool,
) -> str:
    names = set(members)
    has_numbered_frames = any(
        _NUMBERED_FRAME_NAME.fullmatch(name) for name in names
    )
    native_manifest = False
    if _NATIVE_MANIFEST_NAME in members:
        manifest = _jsonl_first_object(
            members[_NATIVE_MANIFEST_NAME],
            _NATIVE_MANIFEST_NAME,
        )
        native_manifest = (
            manifest.get("replay_format") == _NATIVE_REPLAY_FORMAT
        )

    historical_manifest = False
    if "manifest.json" in members:
        manifest = _json_object(members["manifest.json"], "manifest.json")
        historical_manifest = (
            manifest.get("schema_version") in _HISTORICAL_MANIFEST_SCHEMAS
        )
    historical_index = False
    if "index.json" in members:
        index = _json_object(members["index.json"], "index.json")
        historical_index = (
            index.get("schema_version") == _HISTORICAL_INDEX_SCHEMA
        )

    historical_marker = historical_manifest or historical_index
    native_marker = native_manifest or (has_numbered_frames and not archive)
    if historical_marker and (native_manifest or has_numbered_frames):
        raise ValueError(
            "ambiguous replay format: input contains both Native/numbered "
            "frame markers and Historical manifest/index markers"
        )
    if native_marker:
        return "native"
    if historical_marker or (archive and has_numbered_frames):
        return "historical"
    raise ValueError("unable to detect replay format: no supported format markers")


def _json_object(payload: bytes, member_name: str) -> Mapping[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid replay format marker: {member_name}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"replay format marker must be an object: {member_name}")
    return value


def _jsonl_first_object(payload: bytes, member_name: str) -> Mapping[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid replay format marker: {member_name}") from exc
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"invalid replay format marker: {member_name}")
    return _json_object(lines[0].encode("utf-8"), member_name)


def _native_analysis_inputs(
    source: Path,
) -> tuple[GreenFrameAnalysisInput, ...]:
    archive = SourceReplayArchiveImporter().import_path(source)
    scenario_hash = archive.manifest.get("scenario_hash")
    if scenario_hash is not None and not isinstance(scenario_hash, str):
        raise ValueError("native replay scenario_hash must be a string or None")

    indexed_frames: list[tuple[int, SourceFrame]] = []
    for name, frame in zip(archive.frame_names, archive.frames, strict=True):
        match = _NUMBERED_FRAME_NAME.fullmatch(name)
        if match is None:
            raise ValueError(f"invalid Native numbered frame name: {name}")
        indexed_frames.append((int(match.group(1)), frame))
    indexed_frames.sort(key=lambda item: item[0])

    return tuple(
        GreenFrameAnalysisInput(
            frame_index=frame_index,
            frame=_source_frame_to_green(frame),
            frame_hash=frame.source_hash,
            scenario_hash=scenario_hash,
        )
        for frame_index, frame in indexed_frames
    )


def _source_frame_to_green(frame: SourceFrame) -> Mapping[str, Any]:
    return {
        "operators": [json_clone(item.raw) for item in frame.operators],
        "passengers": [json_clone(item.raw) for item in frame.passengers],
    }


def _historical_analysis_inputs(
    source: Path,
) -> tuple[GreenFrameAnalysisInput, ...]:
    replay = HistoricalReplayImporter().import_path(source).replay
    return tuple(
        GreenFrameAnalysisInput(
            frame_index=frame_index,
            frame={
                "operators": [
                    _historical_operator_to_green(operator)
                    for operator in frame.operators
                ],
                "passengers": [],
            },
            frame_hash=None,
            scenario_hash=None,
        )
        for frame_index, frame in enumerate(replay.frames)
    )


def _historical_operator_to_green(
    operator: HistoricalOperatorState,
) -> Mapping[str, Any]:
    raw = operator.source_raw
    unit: dict[str, Any] = {"obj_id": operator.operator_id}
    for field_name in _DIRECT_FACTOR_FIELDS:
        if field_name in raw:
            unit[field_name] = json_clone(raw[field_name])

    side = operator.side.lower()
    if side == "red":
        unit["color"] = 0
    elif side == "blue":
        unit["color"] = 1

    if "blood" not in unit and operator.health is not None:
        unit["blood"] = operator.health
    if "max_blood" not in unit and operator.max_health is not None:
        unit["max_blood"] = operator.max_health
    if "carry_weapon_ids" not in unit and (
        "weapon_ids" in raw or operator.weapon_ids
    ):
        unit["carry_weapon_ids"] = list(operator.weapon_ids)
    return unit


def _validate_output_is_separate(source: Path, output: Path) -> None:
    source_resolved = source.resolve(strict=False)
    output_resolved = output.resolve(strict=False)
    if source.is_file() and source_resolved == output_resolved:
        raise ValueError("analysis output must not replace the input replay")
    if source.is_dir() and output_resolved.is_relative_to(source_resolved):
        raise ValueError("analysis output must be outside the input replay directory")


def _atomic_write(output: Path, payload: bytes, *, overwrite: bool) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not overwrite:
        raise FileExistsError(f"analysis output already exists: {output}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
