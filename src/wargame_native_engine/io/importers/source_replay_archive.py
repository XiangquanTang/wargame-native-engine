from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Any, Iterator, Mapping
from zipfile import BadZipFile, ZipFile, ZipInfo

from wargame_native_engine.compat.source_frame.contract import (
    source_contract_from_manifest, validate_frame_contract,
)
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
    frames: Sequence[SourceFrame]
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
        source_contract_from_manifest(manifest)
        if manifest.get("replay_format") != "native-source-frame-replay/1.0":
            raise ValueError("unsupported Native source replay format")
        if manifest.get("perspective") != "GREEN":
            raise ValueError("Native production replay must contain GREEN frames")
        names = [item[1] for item in frames]
        expected_names = [f"{index}.json" for index in range(len(frames))]
        if names != expected_names:
            raise ValueError("Native replay numbered frames are not continuous")
        for _, _, frame, _ in frames:
            validate_frame_contract(frame.raw, source_contract_from_manifest(manifest))
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


class SourceReplayArchiveReader:
    """Metadata and one-at-a-time frame access for resource-bounded consumers.

    ``SourceReplayArchiveImporter.import_path`` intentionally remains the
    compatible full-materialization API.  This reader is the production path
    for replay verification, analysis and viewers that do not need all parsed
    frames to be resident together.
    """

    def __init__(
        self,
        source: str | Path,
        *,
        frame_importer: SourceFrameImporter | None = None,
    ) -> None:
        self.path = Path(source)
        self.frame_importer = frame_importer or SourceFrameImporter()
        if self.path.is_dir():
            files = tuple(item for item in self.path.iterdir() if item.is_file())
            _validate_member_sizes(
                (item.name, item.stat().st_size) for item in files
            )
            self._member_names = tuple(item.name for item in files)
            self._zip = False
            self.source_hash = _directory_hash(self.path)
        elif self.path.is_file() and self.path.suffix.lower() == ".zip":
            self._zip = True
            self.source_hash = file_sha256(self.path)
            try:
                with ZipFile(self.path) as archive:
                    files = _validated_zip_files(archive)
                    self._member_names = tuple(info.filename for info in files)
            except BadZipFile:
                raise
        else:
            raise ValueError("source replay must be a directory or .zip")

        numbered = [
            (int(match.group(1)), name)
            for name in self._member_names
            if (match := _NUMBERED_FRAME.fullmatch(name))
        ]
        numbered.sort(key=lambda item: item[0])
        if not numbered:
            raise ValueError("source replay has no numbered JSON frames")
        self._frame_numbers = tuple(number for number, _ in numbered)
        self.frame_names = tuple(name for _, name in numbered)
        # Empty is a valid zero-step trajectory; absent is not equivalent.
        self.steps_file_present = "steps.jsonl" in self._member_names
        self.steps_payload = self._read_member("steps.jsonl", required=False)
        self.steps = tuple(json_clone(_jsonl(self.steps_payload)))
        manifest_present = "replay_manifest.native.jsonl" in self._member_names
        manifest_values = _jsonl(self._read_member(
            "replay_manifest.native.jsonl", required=False
        ))
        if manifest_present and len(manifest_values) != 1:
            raise ValueError(
                "Native replay manifest must contain exactly one JSONL record"
            )
        self.manifest = json_clone(manifest_values[0] if manifest_values else {})
        if self.manifest:
            self._validate_native_metadata()

    @property
    def frame_count(self) -> int:
        return len(self.frame_names)

    def iter_frame_payloads(self) -> Iterator[tuple[int, str, bytes]]:
        if self._zip:
            with ZipFile(self.path) as archive:
                for numeric, name in zip(
                    self._frame_numbers, self.frame_names, strict=True
                ):
                    yield numeric, name, _read_zip_member(archive, name)
            return
        for numeric, name in zip(
            self._frame_numbers, self.frame_names, strict=True
        ):
            yield numeric, name, (self.path / name).read_bytes()

    def iter_frames(self) -> Iterator[tuple[int, str, SourceFrame]]:
        expected_hashes = self.manifest.get("frame_hashes")
        for numeric, name, payload in self.iter_frame_payloads():
            source_hash = hashlib.sha256(payload).hexdigest()
            if isinstance(expected_hashes, list) and (
                numeric >= len(expected_hashes)
                or expected_hashes[numeric] != source_hash
            ):
                raise ValueError("Native replay numbered frame hash mismatch")
            value = json.loads(payload.decode("utf-8"))
            if not isinstance(value, Mapping):
                raise ValueError(f"frame {name} root must be an object")
            if self.manifest:
                validate_frame_contract(value, source_contract_from_manifest(self.manifest))
            yield numeric, name, self.frame_importer.from_dict(
                value, source_hash=source_hash
            )

    def frame_at(self, frame_index: int) -> SourceFrame:
        if type(frame_index) is not int or not 0 <= frame_index < self.frame_count:
            raise IndexError("source replay frame index out of range")
        name = self.frame_names[frame_index]
        payload = self._read_member(name, required=True)
        source_hash = hashlib.sha256(payload).hexdigest()
        expected_hashes = self.manifest.get("frame_hashes")
        if (
            isinstance(expected_hashes, list)
            and expected_hashes[frame_index] != source_hash
        ):
            raise ValueError("Native replay numbered frame hash mismatch")
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError(f"frame {name} root must be an object")
        if self.manifest:
            validate_frame_contract(value, source_contract_from_manifest(self.manifest))
        return self.frame_importer.from_dict(value, source_hash=source_hash)

    def lazy_frames(self) -> "LazySourceFrameSequence":
        return LazySourceFrameSequence(self)

    def _read_member(self, name: str, *, required: bool) -> bytes:
        if name not in self._member_names:
            if required:
                raise ValueError(f"source replay member is missing: {name}")
            return b""
        if self._zip:
            with ZipFile(self.path) as archive:
                return _read_zip_member(archive, name)
        return (self.path / name).read_bytes()

    def _validate_native_metadata(self) -> None:
        manifest = self.manifest
        source_contract_from_manifest(manifest)
        if manifest.get("replay_format") != "native-source-frame-replay/1.0":
            raise ValueError("unsupported Native source replay format")
        if manifest.get("perspective") != "GREEN":
            raise ValueError("Native production replay must contain GREEN frames")
        if self.frame_names != tuple(
            f"{index}.json" for index in range(self.frame_count)
        ):
            raise ValueError("Native replay numbered frames are not continuous")
        if int(manifest.get("frame_count", -1)) != self.frame_count:
            raise ValueError("Native replay frame_count mismatch")
        if int(manifest.get("step_count", -1)) != len(self.steps):
            raise ValueError("Native replay step_count mismatch")
        if self.frame_count != len(self.steps) + 1:
            raise ValueError(
                "Native replay must have exactly one frame per step plus zero"
            )
        hashes = manifest.get("frame_hashes")
        if not isinstance(hashes, list) or len(hashes) != self.frame_count:
            raise ValueError("Native replay numbered frame hash mismatch")
        if manifest.get("frames_merkle_root") != canonical_hash(hashes):
            raise ValueError("Native replay frame collection hash mismatch")
        if manifest.get("steps_sha256") != hashlib.sha256(
            self.steps_payload
        ).hexdigest():
            raise ValueError("Native replay steps hash mismatch")
        deterministic = {
            key: json_clone(value) for key, value in manifest.items()
            if key not in {"started_at", "deterministic_content_hash"}
        }
        if manifest.get("deterministic_content_hash") != canonical_hash(deterministic):
            raise ValueError("Native replay deterministic manifest hash mismatch")


class LazySourceFrameSequence(Sequence[SourceFrame]):
    """Random-access frame sequence that never caches parsed frame payloads."""

    def __init__(self, reader: SourceReplayArchiveReader) -> None:
        self.reader = reader

    def __len__(self) -> int:
        return self.reader.frame_count

    def __iter__(self) -> Iterator[SourceFrame]:
        # Sequence's default iterator calls frame_at for every element, which
        # reopens a ZIP and reparses its central directory on every iteration.
        # Streaming validates the same hashes/contracts with one open handle.
        for _, _, frame in self.reader.iter_frames():
            yield frame

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(
                self.reader.frame_at(value)
                for value in range(*index.indices(len(self)))
            )
        if type(index) is not int:
            raise TypeError("frame index must be an int or slice")
        normalized = index if index >= 0 else len(self) + index
        return self.reader.frame_at(normalized)


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
    files = _validated_zip_files(archive)
    values: dict[str, bytes] = {}
    for info in files:
        values[info.filename] = _read_zip_member(archive, info.filename)
    return values


def _validated_zip_files(archive: ZipFile) -> list[ZipInfo]:
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
    return files


def _read_zip_member(archive: ZipFile, name: str) -> bytes:
    with archive.open(name) as stream:
        payload = stream.read(MAX_ARCHIVE_MEMBER_BYTES + 1)
    if len(payload) > MAX_ARCHIVE_MEMBER_BYTES:
        raise ValueError(f"ZIP member exceeds size limit: {name}")
    return payload


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
        with item.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _optional_stage_sort_key(stage: int | None) -> tuple[int, int]:
    """Sort known stages normally while retaining deterministic unknown order."""

    return (0, 0) if stage is None else (1, int(stage))
