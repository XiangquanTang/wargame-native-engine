from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Any, Mapping
from zipfile import BadZipFile, ZipFile, ZipInfo

from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
from wargame_native_engine.compat.source_frame.models import SourceFrame
from wargame_native_engine.io.serialization import canonical_hash, file_sha256, json_clone


_NUMBERED_FRAME = re.compile(r"^(0|[1-9][0-9]*)\.json$")
MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 8 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SourceReplayArchive:
    source_hash: str
    frames: tuple[SourceFrame, ...]
    frame_names: tuple[str, ...]
    steps: tuple[Mapping[str, Any], ...]
    manifest: Mapping[str, Any]
    warnings: tuple[Mapping[str, Any], ...] = ()


class SourceReplayArchiveImporter:
    """Read historical or Native numbered Source frames without SDK code."""

    importer_version = "source-replay-archive-importer/1.0"

    def __init__(self, frame_importer: SourceFrameImporter | None = None) -> None:
        self.frame_importer = frame_importer or SourceFrameImporter()

    def import_path(self, source: str | Path) -> SourceReplayArchive:
        path = Path(source)
        if path.is_dir():
            source_hash = _directory_hash(path)
            files = [item for item in path.iterdir() if item.is_file()]
            _validate_member_sizes(
                ((item.name, item.stat().st_size) for item in files)
            )
            values = {item.name: item.read_bytes() for item in files}
        elif path.is_file() and path.suffix.lower() == ".zip":
            source_hash = file_sha256(path)
            try:
                with ZipFile(path) as archive:
                    values = _read_zip_members(archive)
            except BadZipFile:
                raise
        else:
            raise ValueError("source replay must be a directory or .zip")
        numbered = [(int(match.group(1)), name, payload)
                    for name, payload in values.items()
                    if (match := _NUMBERED_FRAME.fullmatch(name))]
        if not numbered:
            raise ValueError("source replay has no numbered JSON frames")
        frames: list[tuple[int, str, SourceFrame, str]] = []
        for numeric, name, payload in numbered:
            value = json.loads(payload.decode("utf-8"))
            if not isinstance(value, Mapping):
                raise ValueError(f"frame {name} root must be an object")
            frame = self.frame_importer.from_dict(
                value, source_hash=hashlib.sha256(payload).hexdigest())
            frames.append((numeric, name, frame,
                           hashlib.sha256(payload).hexdigest()))
        frames.sort(key=lambda item: (
            _optional_stage_sort_key(item[2].time.stage),
            int(item[2].time.cur_step),
            item[0],
        ))
        steps = _jsonl(values.get("steps.jsonl", b""))
        manifest_present = "replay_manifest.native.jsonl" in values
        manifest_values = _jsonl(values.get("replay_manifest.native.jsonl", b""))
        if manifest_present and len(manifest_values) != 1:
            raise ValueError(
                "Native replay manifest must contain exactly one JSONL record"
            )
        manifest = manifest_values[0] if manifest_values else {}
        if manifest:
            self._validate_native_manifest(
                manifest, frames, steps, values.get("steps.jsonl", b""))
        return SourceReplayArchive(
            source_hash=source_hash,
            frames=tuple(item[2] for item in frames),
            frame_names=tuple(item[1] for item in frames),
            steps=tuple(json_clone(steps)), manifest=json_clone(manifest),
            warnings=(),
        )

    @staticmethod
    def _validate_native_manifest(
            manifest: Mapping[str, Any],
            frames: list[tuple[int, str, SourceFrame, str]],
            steps: list[Mapping[str, Any]], steps_payload: bytes) -> None:
        if manifest.get("replay_format") != "native-source-frame-replay/1.0":
            raise ValueError("unsupported Native source replay format")
        if manifest.get("perspective") != "GREEN":
            raise ValueError("Native production replay must contain GREEN frames")
        names = [item[1] for item in frames]
        expected_names = [f"{index}.json" for index in range(len(frames))]
        if names != expected_names:
            raise ValueError("Native replay numbered frames are not continuous")
        hashes = [item[3] for item in frames]
        if int(manifest.get("frame_count", -1)) != len(frames):
            raise ValueError("Native replay frame_count mismatch")
        if int(manifest.get("step_count", -1)) != len(steps):
            raise ValueError("Native replay step_count mismatch")
        if len(frames) != len(steps) + 1:
            raise ValueError("Native replay must have exactly one frame per step plus zero")
        if manifest.get("frame_hashes") != hashes:
            raise ValueError("Native replay numbered frame hash mismatch")
        if manifest.get("frames_merkle_root") != canonical_hash(hashes):
            raise ValueError("Native replay frame collection hash mismatch")
        if manifest.get("steps_sha256") != hashlib.sha256(steps_payload).hexdigest():
            raise ValueError("Native replay steps hash mismatch")
        deterministic = {key: json_clone(value) for key, value in manifest.items()
                         if key not in {"started_at", "deterministic_content_hash"}}
        if manifest.get("deterministic_content_hash") != canonical_hash(deterministic):
            raise ValueError("Native replay deterministic manifest hash mismatch")


def _jsonl(payload: bytes) -> list[Mapping[str, Any]]:
    if not payload:
        return []
    result = []
    for line in payload.decode("utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise ValueError("JSONL record must be an object")
        result.append(value)
    return result


def _read_zip_members(archive: ZipFile) -> dict[str, bytes]:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise ValueError(
            f"source replay archive has too many members: {len(infos)}"
        )
    seen: set[str] = set()
    files: list[ZipInfo] = []
    file_paths: set[str] = set()
    directory_paths: set[str] = set()
    for info in infos:
        name = info.filename
        if name in seen:
            raise ValueError(f"duplicate ZIP member: {name}")
        seen.add(name)
        _validate_zip_member_name(name)
        normalized = name.rstrip("/")
        if info.is_dir() or name.endswith("/"):
            if normalized in file_paths:
                raise ValueError(f"ZIP file/directory conflict: {normalized}")
            directory_paths.add(normalized)
            continue
        if normalized in directory_paths:
            raise ValueError(f"ZIP file/directory conflict: {normalized}")
        file_paths.add(normalized)
        files.append(info)
    _validate_member_sizes((info.filename, info.file_size) for info in files)
    values: dict[str, bytes] = {}
    for info in files:
        with archive.open(info) as stream:
            payload = stream.read(MAX_ARCHIVE_MEMBER_BYTES + 1)
        if len(payload) > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError(f"ZIP member exceeds size limit: {info.filename}")
        values[info.filename] = payload
    return values


def _validate_zip_member_name(name: str) -> None:
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError(f"unsafe ZIP member path: {name!r}")
    path = PurePosixPath(name)
    parts = tuple(part for part in path.parts if part != "/")
    if path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe ZIP member path: {name!r}")
    # This importer contract is deliberately top-level. Nested historical
    # serialized archives are handled by the dedicated legacy adapter.
    if len(parts) != 1:
        raise ValueError(f"source replay ZIP members must be top-level: {name!r}")


def _validate_member_sizes(values) -> None:
    count = 0
    total = 0
    for name, size in values:
        count += 1
        if count > MAX_ARCHIVE_MEMBERS:
            raise ValueError(f"source replay archive has too many members: {count}")
        if type(size) is not int or size < 0:
            raise ValueError(f"invalid archive member size: {name}")
        if size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError(f"archive member exceeds size limit: {name}")
        total += size
        if total > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError("source replay archive exceeds total size limit")


def _directory_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(value for value in path.iterdir() if value.is_file()):
        digest.update(item.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _optional_stage_sort_key(stage: int | None) -> tuple[int, int]:
    """Sort known stages normally while retaining deterministic unknown order."""

    return (0, 0) if stage is None else (1, int(stage))
