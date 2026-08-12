"""Convert legacy serialized archives into derived frame-playback bundles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from wargame_native_engine.analysis.analyzer import (
    EightFactorAnalyzer,
    GreenFrameAnalysisInput,
)
from wargame_native_engine.analysis.jsonl import analysis_frames_to_jsonl
from wargame_native_engine.analysis.legacy_replay_adapter import (
    LegacySerializedReplaySelection,
    load_legacy_serialized_replay,
)
from wargame_native_engine.analysis.replay_processor import (
    FRAME_ANALYSIS_JSONL_NAME,
)
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.serialization import (
    canonical_json,
    file_sha256,
)


CONVERTED_REPLAY_NAME = "replay.zip"
LEGACY_REPLAY_CONVERSION_VERSION = "legacy-frame-playback-conversion/1.0"
FRAME_PLAYBACK_COMPATIBILITY = "source-numbered-frame-playback/1.0"


@dataclass(frozen=True, slots=True)
class LegacyReplayConversionResult:
    """Published frame-playback artifacts and their source provenance."""

    input_path: Path
    output_dir: Path
    source_archive_sha256: str
    raw_record_count: int
    selected_frame_count: int
    collapsed_record_count: int
    selected_member_names: tuple[str, ...]
    replay_zip_path: Path
    analysis_jsonl_path: Path
    replay_zip_sha256: str
    conversion_version: str = LEGACY_REPLAY_CONVERSION_VERSION
    playback_compatibility: str = FRAME_PLAYBACK_COMPATIBILITY
    strict_replay_supported: bool = False


def convert_legacy_replay(
    input_path: str | Path,
    output_dir: str | Path,
    *,
    analyzer: EightFactorAnalyzer | None = None,
    overwrite: bool = False,
) -> LegacyReplayConversionResult:
    """Publish one legacy archive as numbered playback frames plus analysis.

    The generated ``replay.zip`` intentionally contains only continuous
    numbered Source-compatible frames.  It has no Native steps or manifest and
    therefore makes no strict-replay claim.  The analysis JSONL remains a
    separate derived file beside the archive.
    """

    source = Path(input_path)
    target = Path(output_dir)
    if not source.is_file():
        raise ValueError(f"legacy replay input must be a file: {source}")
    _validate_output_is_separate(source, target)
    if _path_exists(target) and not overwrite:
        raise FileExistsError(f"conversion output already exists: {target}")

    source_hash = file_sha256(source)
    selection = load_legacy_serialized_replay(source)
    frame_payloads = _numbered_frame_payloads(selection)
    configured_analyzer = analyzer if analyzer is not None else EightFactorAnalyzer()
    analysis_payload = _analysis_payload(frame_payloads, configured_analyzer)

    if file_sha256(source) != source_hash:
        raise RuntimeError("legacy replay input changed during conversion")

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    ))
    replay_zip = staging / CONVERTED_REPLAY_NAME
    analysis_jsonl = staging / FRAME_ANALYSIS_JSONL_NAME
    try:
        _write_replay_zip(replay_zip, frame_payloads)
        _write_bytes(analysis_jsonl, analysis_payload)
        _validate_playback_archive(replay_zip, frame_payloads)
        replay_hash = file_sha256(replay_zip)
        _publish_directory(staging, target, overwrite=overwrite)
    finally:
        _remove_path(staging)

    return LegacyReplayConversionResult(
        input_path=source,
        output_dir=target,
        source_archive_sha256=source_hash,
        raw_record_count=selection.raw_record_count,
        selected_frame_count=selection.selected_frame_count,
        collapsed_record_count=selection.collapsed_record_count,
        selected_member_names=selection.selected_member_names,
        replay_zip_path=target / CONVERTED_REPLAY_NAME,
        analysis_jsonl_path=target / FRAME_ANALYSIS_JSONL_NAME,
        replay_zip_sha256=replay_hash,
    )


def _numbered_frame_payloads(
    selection: LegacySerializedReplaySelection,
) -> tuple[bytes, ...]:
    payloads: list[bytes] = []
    for item in selection.inputs:
        payload = (canonical_json(item.frame) + "\n").encode("utf-8")
        # Analyze the exact detached JSON value that the playback archive will
        # contain, not a caller-owned mapping or the legacy member bytes.
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("converted playback frame root must be an object")
        payloads.append(payload)
    return tuple(payloads)


def _analysis_payload(
    frame_payloads: Sequence[bytes],
    analyzer: EightFactorAnalyzer,
) -> bytes:
    inputs: list[GreenFrameAnalysisInput] = []
    for frame_index, payload in enumerate(frame_payloads):
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("converted playback frame root must be an object")
        inputs.append(GreenFrameAnalysisInput(
            frame_index=frame_index,
            frame=value,
            frame_hash=hashlib.sha256(payload).hexdigest(),
            scenario_hash=None,
        ))

    timeline = analyzer.analyze_green_frames(tuple(inputs))
    expected_indices = tuple(range(len(frame_payloads)))
    if timeline.frame_indices != expected_indices:
        raise ValueError("converted analysis frame indices are not continuous")
    text = analysis_frames_to_jsonl(timeline.frame_analyses)
    payload = text.encode("utf-8")
    if not payload or not payload.endswith(b"\n"):
        raise ValueError("converted analysis JSONL must be newline-terminated")
    if len(text.splitlines()) != len(frame_payloads):
        raise ValueError("converted analysis JSONL frame count mismatch")
    return payload


def _write_replay_zip(path: Path, frame_payloads: Sequence[bytes]) -> None:
    with ZipFile(
        path,
        "w",
        compression=ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for frame_index, payload in enumerate(frame_payloads):
            info = ZipInfo(
                f"{frame_index}.json",
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.create_system = 3
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload, compresslevel=9)
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _validate_playback_archive(
    path: Path,
    frame_payloads: Sequence[bytes],
) -> None:
    imported = SourceReplayArchiveImporter().import_path(path)
    expected_names = tuple(
        f"{frame_index}.json" for frame_index in range(len(frame_payloads))
    )
    if imported.frame_names != expected_names:
        raise ValueError("converted replay frame playback order mismatch")
    if imported.steps or imported.manifest:
        raise ValueError("converted frame-playback replay must not claim Native steps")
    expected_hashes = tuple(
        hashlib.sha256(payload).hexdigest() for payload in frame_payloads
    )
    if tuple(frame.source_hash for frame in imported.frames) != expected_hashes:
        raise ValueError("converted replay numbered frame hash mismatch")


def _write_bytes(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _validate_output_is_separate(source: Path, output: Path) -> None:
    source_resolved = source.resolve(strict=False)
    output_resolved = output.resolve(strict=False)
    if source_resolved == output_resolved or source_resolved.is_relative_to(
        output_resolved
    ):
        raise ValueError("conversion output must not replace or contain the input replay")
    if _path_exists(output):
        try:
            if os.path.samefile(source, output):
                raise ValueError("conversion output must not alias the input replay")
        except FileNotFoundError:
            pass


def _publish_directory(target_staging: Path, target: Path, *, overwrite: bool) -> None:
    if not _path_exists(target):
        os.replace(target_staging, target)
        return
    if not overwrite:
        raise FileExistsError(f"conversion output already exists: {target}")

    descriptor, backup_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".backup",
        dir=target.parent,
    )
    os.close(descriptor)
    backup = Path(backup_name)
    backup.unlink()
    os.replace(target, backup)
    try:
        os.replace(target_staging, target)
    except BaseException:
        os.replace(backup, target)
        raise
    _remove_path(backup)


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _remove_path(path: Path) -> None:
    if not _path_exists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
