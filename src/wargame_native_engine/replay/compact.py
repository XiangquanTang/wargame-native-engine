"""Lossless raw-JSON replay storage, independent of Native strict re-execution.

No importer/exporter is called here. Frames are JSON trees, not SourceFrame
models. ZIP members are never extracted. Each bounded gzip JSONL chunk starts
with a full checkpoint, so seek only decodes at most checkpoint_interval frames.
"""
from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Iterator, Mapping
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

COMPACT_FORMAT = "native-source-compact-replay/1.0"
MANIFEST_NAME = "compact_manifest.json"
MAX_RECORD_BYTES = 64 * 1024 * 1024
MAX_ASSET_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_FRAMES = 2_000_000
MAX_DEPTH = 128
_NAME = re.compile(r"[0-9]+\.json\Z")


def _validate(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ValueError("compact JSON nesting limit exceeded")
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("compact JSON requires finite numbers")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError("compact JSON object keys must be strings")
            _validate(child, depth + 1)
        return
    if isinstance(value, list):
        for child in value:
            _validate(child, depth + 1)
        return
    raise ValueError(f"not a JSON value: {type(value).__name__}")


def _encode(value: Any, limit: int = MAX_RECORD_BYTES) -> bytes:
    _validate(value)
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    if len(payload) > limit:
        raise ValueError("compact JSON record size limit exceeded")
    return payload


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _decode(payload: bytes, limit: int = MAX_RECORD_BYTES) -> Any:
    if len(payload) > limit:
        raise ValueError("compact JSON record size limit exceeded")
    value = json.loads(payload, object_pairs_hook=_pairs,
                       parse_constant=lambda value: (_ for _ in ()).throw(
                           ValueError("non-finite JSON number")))
    _validate(value)
    return value


def json_equal(left: Any, right: Any) -> bool:
    """Tree equality retains numeric types and the sign of floating zero."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            json_equal(a, b) for a, b in zip(left, right))
    if type(left) is float:
        return left.hex() == right.hex()
    return left == right


def make_patch(previous: Any, current: Any) -> list[dict[str, Any]]:
    """Exact paths are typed tokens (string dict keys, integer array indices).

    Arrays whose lengths change are replaced atomically. This intentionally
    avoids object-ID inference, string/int key coercion and reorder heuristics.
    """
    _validate(previous)
    _validate(current)
    operations = []
    def visit(old, new, path):
        if type(old) is type(new) is dict:
            for key in old:
                if key not in new:
                    operations.append({"op": "remove", "path": path + [key]})
            for key, value in new.items():
                if key not in old:
                    operations.append({"op": "add", "path": path + [key], "value": deepcopy(value)})
                else:
                    visit(old[key], value, path + [key])
        elif type(old) is type(new) is list and len(old) == len(new):
            for index, (a, b) in enumerate(zip(old, new)):
                visit(a, b, path + [index])
        elif not json_equal(old, new):
            operations.append({"op": "replace", "path": path, "value": deepcopy(new)})
    visit(previous, current, [])
    return operations


def apply_patch(previous: Any, operations: Any) -> Any:
    """Apply validated operations to a detached tree; malformed paths fail closed."""
    _validate(previous)
    if not isinstance(operations, list) or len(operations) > 1_000_000:
        raise ValueError("invalid compact patch operations")
    result = deepcopy(previous)
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("invalid compact patch operation")
        op = operation.get("op")
        expected = {"op", "path"} if op == "remove" else {"op", "path", "value"}
        if op not in {"add", "remove", "replace"} or set(operation) != expected:
            raise ValueError("invalid compact patch operation fields")
        path = operation["path"]
        if not isinstance(path, list) or len(path) > MAX_DEPTH or any(
                type(token) not in (str, int) for token in path):
            raise ValueError("invalid compact patch path")
        if op != "remove":
            _validate(operation["value"])
        if not path:
            if op != "replace":
                raise ValueError("only replace may target the root")
            result = deepcopy(operation["value"])
            continue
        parent = result
        for token in path[:-1]:
            if isinstance(parent, dict) and type(token) is str and token in parent:
                parent = parent[token]
            elif isinstance(parent, list) and type(token) is int and 0 <= token < len(parent):
                parent = parent[token]
            else:
                raise ValueError("compact patch path does not exist")
        key = path[-1]
        if isinstance(parent, dict) and type(key) is str:
            exists = key in parent
            if op == "add" and exists or op in {"remove", "replace"} and not exists:
                raise ValueError("compact patch object operation precondition failed")
            if op == "remove":
                del parent[key]
            else:
                parent[key] = deepcopy(operation["value"])
        elif isinstance(parent, list) and type(key) is int and 0 <= key < len(parent) and op == "replace":
            parent[key] = deepcopy(operation["value"])
        else:
            raise ValueError("invalid compact patch container or array operation")
    _validate(result)
    return result


def _atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class CompactReplayWriter:
    """Incremental recorder retaining one previous frame, not a replay tree list.

    The private spool contains compressed committed chunks, never a second set
    of numbered full frames. A finalized spool can be packaged again if an
    interrupted publication fails; package_zip never removes input files.
    """
    replay_format = COMPACT_FORMAT

    def __init__(self, root: str | Path, *, checkpoint_interval: int = 128):
        if type(checkpoint_interval) is not int or not 1 <= checkpoint_interval <= 4096:
            raise ValueError("checkpoint_interval must be in 1..4096")
        self.root = Path(root)
        self.checkpoint_interval = checkpoint_interval
        self._previous = None
        self._names: list[str] = []
        self._name_set: set[str] = set()
        self._chunks: list[dict[str, Any]] = []
        self._metadata: dict[str, Any] = {}
        self._started = False
        self._finalized = False
        self._failed = False
        self.manifest: dict[str, Any] | None = None

    def start(self, metadata: Mapping[str, Any] | None = None, *,
              assets: Mapping[str, Any] | None = None, started_at: str | None = None) -> None:
        if self._started:
            raise RuntimeError("compact recorder already started")
        if self.root.exists() and any(self.root.iterdir()):
            raise FileExistsError(f"refusing to overwrite recorder output: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        self._metadata = deepcopy(dict(metadata or {}))
        _validate(self._metadata)
        self._assets = deepcopy(dict(assets or {}))
        _atomic_write(self.root / "assets.json", _encode(self._assets, MAX_ASSET_BYTES))
        self._started = True

    def append(self, raw_frame: Mapping[str, Any], *, name: str | None = None,
               evidence: Mapping[str, Any] | None = None) -> None:
        if not self._started or self._finalized or self._failed:
            raise RuntimeError("compact recorder is not active")
        frame = deepcopy(dict(raw_frame))
        _encode(frame)
        if len(self._names) >= MAX_FRAMES:
            raise ValueError("compact frame limit exceeded")
        index = len(self._names)
        name = f"{index}.json" if name is None else name
        if type(name) is not str or not _NAME.fullmatch(name) or name in self._name_set:
            raise ValueError("source frame name must be a unique numbered JSON filename")
        if self._names and int(name[:-5]) <= int(self._names[-1][:-5]):
            raise ValueError("source frame numbers must increase; gaps are permitted")
        checkpoint = index % self.checkpoint_interval == 0
        record: dict[str, Any] = {"index": index, "name": name}
        if checkpoint:
            record["frame"] = frame
        else:
            record["patch"] = make_patch(self._previous, frame)
        if evidence is not None:
            record["evidence"] = deepcopy(dict(evidence))
        payload = _encode(record) + b"\n"
        chunk_index = index // self.checkpoint_interval
        path = self.root / f"chunk-{chunk_index:08d}.jsonl.gz"
        # Every append is an independent gzip member in a concatenated stream.
        # Compression for each patch is bounded; no gzip writer state can become
        # inconsistent after rollback, and checkpoints permit bounded seeks.
        compressed = gzip.compress(payload, compresslevel=6, mtime=0)
        with path.open("a+b") as stream:
            offset = stream.tell()
            try:
                stream.write(compressed)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                try:
                    stream.seek(offset)
                    stream.truncate()
                    stream.flush()
                    os.fsync(stream.fileno())
                except BaseException:
                    self._failed = True
                raise
        if checkpoint:
            self._chunks.append({"name": path.name, "start": index, "count": 0})
        self._chunks[-1]["count"] += 1
        self._names.append(name)
        self._name_set.add(name)
        self._previous = frame

    def finalize(self, outcome: Mapping[str, Any] | None = None, *,
                 final_state_hash: str | None = None) -> dict[str, Any]:
        if self._finalized:
            return deepcopy(self.manifest)
        if not self._started or not self._names or self._failed:
            raise RuntimeError("compact recorder cannot be finalized")
        self._final_state_hash = final_state_hash
        outcome = deepcopy(dict(outcome or {}))
        manifest = {
            **self._metadata,
            "replay_format": COMPACT_FORMAT,
            "frame_count": len(self._names),
            "step_count": len(self._names) - 1,
            "frame_names": list(self._names),
            "checkpoint_interval": self.checkpoint_interval,
            "chunks": deepcopy(self._chunks),
            "assets": "assets.json",
            "termination": bool(outcome.get("terminal", False)),
            "truncated": bool(outcome.get("truncated", False)),
            "winner": outcome.get("winner"),
            "outcome": outcome,
            "integrity": "external-package-sha256",
            "reconstruction": "raw-json-tree-lossless",
        }
        _atomic_write(self.root / MANIFEST_NAME, _encode(manifest, MAX_MANIFEST_BYTES))
        self.manifest = manifest
        self._finalized = True
        return deepcopy(manifest)

    def package_zip(self, output: str | Path | None = None) -> Path:
        if not self._finalized:
            raise RuntimeError("compact recorder not finalized")
        target = Path(output) if output is not None else self.root / "replay.zip"
        paths = [self.root / MANIFEST_NAME, self.root / "assets.json", *(
            self.root / chunk["name"] for chunk in self._chunks)]
        if any(target.resolve() == path.resolve() or
               (target.exists() and os.path.samefile(target, path)) for path in paths):
            raise ValueError("archive output aliases compact recording input")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        os.close(descriptor)
        try:
            with ZipFile(temporary, "w") as archive:
                for path in paths:
                    info = ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = ZIP_STORED if path.suffix == ".gz" else ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    with path.open("rb") as source, archive.open(info, "w") as dest:
                        shutil.copyfileobj(source, dest, 1024 * 1024)
            with open(temporary, "rb") as stream:
                os.fsync(stream.fileno())
            digest = _sha256(Path(temporary))
            os.replace(temporary, target)
            # Hash stays outside the package and cannot recursively hash itself.
            _atomic_write(Path(str(target) + ".sha256"),
                          (digest + "  " + target.name + "\n").encode("ascii"))
        finally:
            Path(temporary).unlink(missing_ok=True)
        return target

    def iter_records(self) -> Iterator[tuple[str, dict, dict | None]]:
        """Read this writer's own committed spool without publishing a ZIP."""
        for chunk in self._chunks:
            yield from self._spool_chunk(chunk)

    def _spool_chunk(self, chunk):
        frame = None
        with gzip.open(self.root / chunk["name"], "rb") as stream:
            for offset in range(chunk["count"]):
                payload = stream.readline(MAX_RECORD_BYTES + 2)
                if not payload.endswith(b"\n") or len(payload) > MAX_RECORD_BYTES + 1:
                    raise ValueError("corrupt compact recording spool")
                record = _decode(payload[:-1])
                frame = record["frame"] if offset == 0 else apply_patch(frame, record["patch"])
                yield record["name"], deepcopy(frame), deepcopy(record.get("evidence"))
            if stream.read(1):
                raise ValueError("unexpected compact recording spool data")

    def raw_frame(self, index: int) -> dict[str, Any]:
        if type(index) is not int or not 0 <= index < len(self._names):
            raise IndexError("compact recording frame ordinal out of range")
        records = self._spool_chunk(self._chunks[index // self.checkpoint_interval])
        try:
            for offset, (_, frame, _) in enumerate(records):
                if offset == index % self.checkpoint_interval:
                    return frame
        finally:
            records.close()
        raise ValueError("compact recording frame missing")

    def record_initial(self, green_frame: Mapping[str, Any], *, initial_state_hash: str = "") -> None:
        if self._names:
            raise RuntimeError("initial frame already recorded")
        self._initial_state_hash = initial_state_hash
        native = green_frame.get("extra", {}).get("native", {})
        if isinstance(native, Mapping):
            self._metadata["perspective"] = native.get("perspective", "GREEN")
            for key in ("source_frame_compatibility_version", "adjudication_semantics_version"):
                if key in native:
                    self._metadata[key] = native[key]
            if "source_frame_compatibility_version" in native:
                self._metadata["frame_contract"] = native["source_frame_compatibility_version"]
        self.append(green_frame)

    def record_step(self, green_frame: Mapping[str, Any], step_record: Mapping[str, Any]) -> None:
        if not self._names:
            raise RuntimeError("initial frame not recorded")
        self.append(green_frame)


class CompactSourceFrameRecorder(CompactReplayWriter):
    """Native live recording: minimal facts normally, strict evidence only opt-in."""
    def __init__(self, root: str | Path, *, checkpoint_interval: int = 128,
                 debug_evidence: bool = False):
        super().__init__(root, checkpoint_interval=checkpoint_interval)
        self.debug_evidence = debug_evidence
        self._native_static = None
        self._source_registry: list[dict] = []

    def _playback_frame(self, frame):
        if self.debug_evidence:
            return frame
        from wargame_native_engine.replay.native_projection import (
            native_playback_frame, NATIVE_PLAYBACK_PROJECTION)
        projected, static = native_playback_frame(frame, source_registry=self._source_registry)
        self._metadata["adjudication_sources"] = self._source_registry
        if self._native_static is None:
            self._native_static = static
            self._metadata.update(deepcopy(static))
            self._metadata["native_frame_projection"] = NATIVE_PLAYBACK_PROJECTION
        elif not json_equal(static, self._native_static):
            raise ValueError("Native static playback provenance changed during recording")
        return projected

    def record_initial(self, green_frame, *, initial_state_hash=""):
        super().record_initial(self._playback_frame(green_frame), initial_state_hash=initial_state_hash)

    def record_step(self, green_frame, step_record):
        if not self._names:
            raise RuntimeError("initial frame not recorded")
        self.append(self._playback_frame(green_frame),
                    evidence=step_record if self.debug_evidence else None)

    def finalize(self, outcome=None, *, final_state_hash=None):
        result = deepcopy(dict(outcome or {}))
        if not self.debug_evidence:
            result.pop("world_hash", None)
        return super().finalize(result, final_state_hash=final_state_hash)



def is_compact_replay(path: str | Path) -> bool:
    with ZipFile(path) as archive:
        return MANIFEST_NAME in archive.namelist()


class CompactReplayReader:
    def __init__(self, path: str | Path, *, verify_hash: bool = True):
        self.path = Path(path)
        sidecar = Path(str(self.path) + ".sha256")
        self.hash_verified = False
        if verify_hash and sidecar.exists():
            if sidecar.stat().st_size > 4096:
                raise ValueError("invalid compact package hash sidecar")
            fields = sidecar.read_text("ascii").strip().split()
            if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{64}", fields[0]) or fields[1] != self.path.name:
                raise ValueError("invalid compact package hash sidecar")
            if _sha256(self.path) != fields[0]:
                raise ValueError("compact package SHA256 mismatch")
            self.hash_verified = True
        with ZipFile(self.path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)) or len(names) > MAX_FRAMES + 2:
                raise ValueError("duplicate or excessive compact ZIP members")
            manifest = _decode(_read_member(archive, MANIFEST_NAME, MAX_MANIFEST_BYTES), MAX_MANIFEST_BYTES)
            if not isinstance(manifest, dict) or manifest.get("replay_format") != COMPACT_FORMAT:
                raise ValueError("unsupported compact replay format")
            count = manifest.get("frame_count")
            interval = manifest.get("checkpoint_interval")
            if type(count) is not int or not 1 <= count <= MAX_FRAMES or type(interval) is not int or not 1 <= interval <= 4096:
                raise ValueError("invalid compact frame count or checkpoint interval")
            frame_names = manifest.get("frame_names")
            if not isinstance(frame_names, list) or len(frame_names) != count or any(
                    type(name) is not str or not _NAME.fullmatch(name) for name in frame_names):
                raise ValueError("invalid compact frame names")
            if any(int(a[:-5]) >= int(b[:-5]) for a, b in zip(frame_names, frame_names[1:])):
                raise ValueError("compact source frame numbers must increase")
            chunks = manifest.get("chunks")
            if not isinstance(chunks, list) or len(chunks) != (count + interval - 1) // interval:
                raise ValueError("invalid compact chunk index")
            for index, chunk in enumerate(chunks):
                expected = {"name": f"chunk-{index:08d}.jsonl.gz", "start": index * interval,
                            "count": min(interval, count - index * interval)}
                if chunk != expected or any(type(chunk[key]) is not int for key in ("start", "count")):
                    raise ValueError("invalid compact checkpoint index")
            allowed = {MANIFEST_NAME, "assets.json", *(chunk["name"] for chunk in chunks)}
            if set(names) != allowed or manifest.get("assets") != "assets.json":
                raise ValueError("unexpected/missing compact archive members")
            assets = _decode(_read_member(archive, "assets.json", MAX_ASSET_BYTES), MAX_ASSET_BYTES)
            if not isinstance(assets, dict):
                raise ValueError("compact assets must be an object")
        self.manifest = manifest
        self.assets = assets
        self.frame_names = tuple(frame_names)
        self.frame_count = count
        self.checkpoints = tuple(deepcopy(chunks))
        self._interval = interval

    def _chunk_records(self, chunk_index: int) -> Iterator[tuple[str, dict, dict | None]]:
        chunk = self.checkpoints[chunk_index]
        frame = None
        with ZipFile(self.path) as archive:
            info = archive.getinfo(chunk["name"])
            if info.file_size > MAX_RECORD_BYTES * chunk["count"]:
                raise ValueError("oversized compact chunk")
            with archive.open(info) as compressed, gzip.GzipFile(fileobj=compressed, mode="rb") as stream:
                for offset in range(chunk["count"]):
                    payload = stream.readline(MAX_RECORD_BYTES + 2)
                    if not payload.endswith(b"\n") or len(payload) > MAX_RECORD_BYTES + 1:
                        raise ValueError("truncated or oversized compact record")
                    record = _decode(payload[:-1])
                    index = chunk["start"] + offset
                    if not isinstance(record, dict) or type(record.get("index")) is not int or record["index"] != index or record.get("name") != self.frame_names[index]:
                        raise ValueError("compact record index/name mismatch")
                    expected = {"index", "name", "frame" if offset == 0 else "patch"}
                    if "evidence" in record:
                        expected.add("evidence")
                        if not isinstance(record["evidence"], dict):
                            raise ValueError("compact evidence must be an object")
                    if set(record) != expected:
                        raise ValueError("invalid compact record fields/checkpoint")
                    frame = record["frame"] if offset == 0 else apply_patch(frame, record["patch"])
                    if not isinstance(frame, dict):
                        raise ValueError("compact Source frame root must be an object")
                    # Bound decoded frame too, not merely the patch record.
                    _encode(frame)
                    yield record["name"], deepcopy(frame), deepcopy(record.get("evidence"))
                if stream.read(1):
                    raise ValueError("unexpected records after compact chunk count")

    def _record(self, index: int) -> tuple[str, dict, dict | None]:
        if type(index) is not int or not 0 <= index < self.frame_count:
            raise IndexError("compact frame ordinal out of range")
        chunk_index = index // self._interval
        offset = index % self._interval
        records = self._chunk_records(chunk_index)
        try:
            for current, record in enumerate(records):
                if current == offset:
                    return record
        finally:
            records.close()
        raise ValueError("compact frame missing")

    def raw_frame(self, index: int) -> dict[str, Any]:
        return self._record(index)[1]

    def evidence(self, index: int) -> dict[str, Any] | None:
        return self._record(index)[2]

    def iter_records(self) -> Iterator[tuple[str, dict, dict | None]]:
        for index in range(len(self.checkpoints)):
            yield from self._chunk_records(index)

    def iter_raw_frames(self) -> Iterator[dict[str, Any]]:
        for _, frame, _ in self.iter_records():
            yield frame


def _read_member(archive: ZipFile, name: str, limit: int) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise ValueError(f"oversized compact ZIP member: {name}")
    with archive.open(info) as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError(f"oversized compact ZIP member: {name}")
    return value


def package_legacy_recording(recorder: CompactReplayWriter, output: str | Path) -> Path:
    """Explicit debug/research export; normal publication never runs this path."""
    from wargame_native_engine.replay.source_frame_recorder import SourceFrameRecorder
    if not recorder._finalized:
        raise RuntimeError("compact recorder not finalized")
    if not getattr(recorder, "debug_evidence", False):
        raise ValueError("strict debug evidence was not enabled at recording start")
    with tempfile.TemporaryDirectory(prefix="wargame-legacy-export-") as temporary:
        root = Path(temporary)
        compact = recorder.package_zip(root / "compact.zip")
        reader = CompactReplayReader(compact)
        legacy = SourceFrameRecorder(root / "facts")
        legacy.start(recorder._metadata)
        for index, (_, frame, evidence) in enumerate(reader.iter_records()):
            if index == 0:
                native = frame.get("extra", {}).get("native", {})
                initial_hash = getattr(recorder, "_initial_state_hash", None) or native.get("world_hash")
                if not initial_hash:
                    raise ValueError("strict debug export requires initial state identity")
                legacy.record_initial(frame, initial_state_hash=initial_hash)
            else:
                if evidence is None:
                    raise ValueError("strict debug export requires Native step evidence")
                legacy.record_step(frame, evidence)
        final_hash = getattr(recorder, "_final_state_hash", None)
        if not final_hash:
            raise ValueError("strict debug export requires final state identity")
        legacy.finalize(recorder.manifest["outcome"], final_state_hash=final_hash)
        return legacy.package_zip(output)
