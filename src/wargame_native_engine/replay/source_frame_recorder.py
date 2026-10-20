from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping, Sequence
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from wargame_native_engine.compat.source_frame.contract import (
    LEGACY_SOURCE_CONTRACT, SOURCE_CONTRACT_KEY, source_contract_from_manifest,
    validate_frame_contract,
)
from wargame_native_engine.analysis.analyzer import (
    EightFactorAnalyzer,
    GreenFrameAnalysisInput,
)
from wargame_native_engine.analysis.jsonl import analysis_frames_to_jsonl
from wargame_native_engine.compat.source_frame.serialization import (
    canonical_green_frame_payload,
    canonical_source_json,
    canonical_validated_green_frame_payload,
)
from wargame_native_engine.io.serialization import canonical_hash, file_sha256, json_clone
from wargame_native_engine.rules.indirect_fire import (
    load_indirect_fire_replay_identity,
)
from wargame_native_engine.version import ADJUDICATION_SEMANTICS_VERSION


ANALYSIS_SIDECAR_NAME = "frame_analysis.native.jsonl"


class SourceRecorderAppendRollbackError(RuntimeError):
    """A JSONL append failed and its committed prefix could not be secured."""


class SourceFrameRecorder:
    """Observer-only writer for numbered GREEN Source-compatible frames."""

    replay_format = "native-source-frame-replay/1.0"
    frame_contract = "source-frame-compat/1.0"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._started = False
        self._finalized = False
        self._failed = False
        self._frame_hashes: list[str] = []
        self._metadata: dict[str, Any] = {}
        self._initial_state_hash: str | None = None

    def start(self, metadata: Mapping[str, Any], *,
              started_at: str | None = None) -> None:
        if self._started:
            raise RuntimeError("recorder_already_started")
        if self.root.exists() and any(self.root.iterdir()):
            raise FileExistsError(f"refusing to overwrite recorder output: {self.root}")
        normalized_metadata = json_clone(metadata)
        source_contract_from_manifest(normalized_metadata)
        declared_semantics = normalized_metadata.get(
            "adjudication_semantics_version")
        if (declared_semantics is not None
                and declared_semantics != ADJUDICATION_SEMANTICS_VERSION):
            raise ValueError("recorder adjudication semantics mismatch")
        self.root.mkdir(parents=True, exist_ok=True)
        self._metadata = normalized_metadata
        self._metadata["adjudication_semantics_version"] = (
            ADJUDICATION_SEMANTICS_VERSION)
        self._metadata["started_at"] = started_at or datetime.now(
            timezone.utc).isoformat()
        self._atomic_write(self.root / "steps.jsonl", b"")
        self._started = True

    def record_initial(self, green_frame: Mapping[str, Any], *,
                       initial_state_hash: str) -> None:
        self._require_active()
        if self._frame_hashes:
            raise RuntimeError("initial_frame_already_recorded")
        if green_frame.get("actions") or green_frame.get("judge_info"):
            raise ValueError("GREEN 0.json must have empty actions and judge_info")
        # Replay-affecting authority identities are copied only from the
        # already-produced GREEN frame.  The recorder remains observer-only,
        # while direct recorder users cannot accidentally emit a new archive
        # whose frames use type-derived boarding semantics but whose manifest
        # would replay under legacy explicit-profile-only semantics.
        extra = green_frame.get("extra")
        native = extra.get("native") if isinstance(extra, Mapping) else None
        if isinstance(native, Mapping):
            for name in (
                "boarding_type_assignment_authority_version",
                "boarding_type_assignment_authority_hash",
                "boarding_type_assignment_policy",
            ):
                if name in native:
                    self._metadata[name] = json_clone(native[name])
            capture_policy = native.get("objective_capture_policy")
            if isinstance(capture_policy, Mapping):
                self._metadata["objective_capture_policy"] = json_clone(
                    capture_policy
                )
        declared = native.get(SOURCE_CONTRACT_KEY) if isinstance(native, Mapping) else None
        contract = source_contract_from_manifest({
            SOURCE_CONTRACT_KEY: declared or LEGACY_SOURCE_CONTRACT,
        })
        if any(name in self._metadata for name in (SOURCE_CONTRACT_KEY, "frame_contract")):
            if source_contract_from_manifest(self._metadata) != contract:
                raise ValueError("recorder Source projection contract mismatch")
        validate_frame_contract(green_frame, contract)
        self.frame_contract = contract
        if contract != LEGACY_SOURCE_CONTRACT:
            self._metadata[SOURCE_CONTRACT_KEY] = contract
        self._initial_state_hash = str(initial_state_hash)
        self._record_frame(green_frame)

    def record_step(self, green_frame: Mapping[str, Any],
                    step_record: Mapping[str, Any]) -> None:
        self._require_active()
        if not self._frame_hashes:
            raise RuntimeError("initial_frame_not_recorded")
        record = json_clone(step_record)
        expected_index = len(self._frame_hashes)
        record["step_index"] = expected_index
        record["frame_index"] = expected_index
        record.setdefault("decision_window_index", expected_index - 1)
        record.setdefault("frame_interval_offset", 0)
        required = {"delta_time_ms", "ordered_actions", "expected_state_hash"}
        missing = sorted(required - set(record))
        if missing:
            raise ValueError(f"step record missing fields: {missing}")
        ordered_actions = record["ordered_actions"]
        if (
            isinstance(ordered_actions, Sequence)
            and not isinstance(ordered_actions, (str, bytes))
            and any(
                _is_indirect_fire_action(value)
                for value in ordered_actions
            )
        ):
            self._metadata.update(load_indirect_fire_replay_identity())
        frame_path = self._record_frame(green_frame)
        try:
            self._append_jsonl(self.root / "steps.jsonl", record)
        except BaseException as exc:
            # The step and its GREEN frame form one recorder commit.  A failed
            # append must not leave a future finalize with a dangling frame.
            frame_path.unlink(missing_ok=True)
            self._frame_hashes.pop()
            if isinstance(exc, SourceRecorderAppendRollbackError):
                self._failed = True
            raise

    def finalize(self, outcome: Mapping[str, Any], *,
                 final_state_hash: str) -> Mapping[str, Any]:
        self._require_active()
        if not self._frame_hashes:
            raise RuntimeError("recorder_has_no_frames")
        if self._finalized:
            raise RuntimeError("recorder_already_finalized")
        if self._failed:
            raise RuntimeError("recorder_failed_reset_required")
        result = json_clone(outcome)
        step_count = len(self._frame_hashes) - 1
        if len(_read_jsonl(self.root / "steps.jsonl")) != step_count:
            raise RuntimeError("recorder frame/step count mismatch")
        manifest = {
            "replay_format": self.replay_format,
            "frame_contract": self.frame_contract,
            "perspective": "GREEN",
            **json_clone(self._metadata),
            "frame_count": len(self._frame_hashes),
            "step_count": step_count,
            "termination": bool(result.get("terminal", False)),
            "truncated": bool(result.get("truncated", False)),
            "termination_reason": result.get("reason"),
            "winner": result.get("winner"),
            "initial_state_hash": self._initial_state_hash,
            "final_state_hash": str(final_state_hash),
            "steps_sha256": file_sha256(self.root / "steps.jsonl"),
            "frames_merkle_root": canonical_hash(self._frame_hashes),
            "frame_hashes": list(self._frame_hashes),
            "source_frame_compatibility_version": self.frame_contract,
        }
        manifest["deterministic_content_hash"] = canonical_hash({
            key: value for key, value in manifest.items() if key != "started_at"
        })
        self._atomic_write(
            self.root / "replay_manifest.native.jsonl",
            (_canonical_json(manifest) + "\n").encode("utf-8"))
        self._finalized = True
        return json_clone(manifest)

    def package_zip(
        self,
        output: str | Path | None = None,
        *,
        analysis_analyzer: EightFactorAnalyzer | None = None,
    ) -> Path:
        if not self._finalized:
            raise RuntimeError("recorder_not_finalized")
        target = Path(output) if output is not None else self.root / "replay.zip"
        authoritative_member_paths = [
            path for path in self.root.iterdir()
            if path.is_file()
            and (_is_numbered_frame(path.name) or path.name in {
                "steps.jsonl",
                "replay_manifest.native.jsonl",
            })
        ]
        _validate_archive_output_does_not_alias_facts(
            target,
            authoritative_member_paths,
        )
        member_paths = sorted(
            authoritative_member_paths,
            key=lambda path: _member_sort_key(path.name),
        )

        sidecar_payload: bytes | None = None
        if analysis_analyzer is not None:
            sidecar_payload = _build_analysis_sidecar(
                member_paths,
                analysis_analyzer,
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=target.parent)
        os.close(descriptor)
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED,
                         compresslevel=9) as archive:
                sidecar_written = False
                for path in member_paths:
                    if (
                        sidecar_payload is not None
                        and not sidecar_written
                        and not _is_numbered_frame(path.name)
                    ):
                        _write_zip_payload(
                            archive, ANALYSIS_SIDECAR_NAME, sidecar_payload
                        )
                        sidecar_written = True
                    info = ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    with path.open("rb") as source, archive.open(
                        info, "w"
                    ) as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target

    def _record_frame(self, frame: Mapping[str, Any]) -> Path:
        validate_frame_contract(frame, self.frame_contract)
        index = len(self._frame_hashes)
        payload = canonical_validated_green_frame_payload(frame)
        path = self.root / f"{index}.json"
        self._atomic_write(path, payload)
        self._frame_hashes.append(hashlib.sha256(payload).hexdigest())
        return path

    def _require_active(self) -> None:
        if not self._started:
            raise RuntimeError("recorder_not_started")
        if self._finalized:
            raise RuntimeError("recorder_already_finalized")
        if self._failed:
            raise RuntimeError("recorder_failed_reset_required")

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.",
                                                  dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @classmethod
    def _append_jsonl(cls, path: Path, value: Mapping[str, Any]) -> None:
        payload = (_canonical_json(value) + "\n").encode("utf-8")
        with path.open("r+b" if path.exists() else "w+b") as stream:
            stream.seek(0, os.SEEK_END)
            committed_size = stream.tell()
            try:
                _write_all(stream, payload)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException as append_error:
                try:
                    stream.seek(committed_size)
                    stream.truncate()
                    stream.flush()
                    os.fsync(stream.fileno())
                except BaseException as rollback_error:
                    raise SourceRecorderAppendRollbackError(
                        "Source recorder append rollback was not durable"
                    ) from rollback_error
                raise append_error


def _write_all(stream, payload: bytes) -> None:
    """Complete an append even when the underlying writer short-writes."""

    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = stream.write(view[written:])
        if count is None or count <= 0:
            raise OSError("Source recorder append made no progress")
        written += count


def _canonical_json(value: Any) -> str:
    return canonical_source_json(value)


def _is_indirect_fire_action(value: Mapping[str, Any]) -> bool:
    if not isinstance(value, Mapping) or value.get("action_type") != "FIRE":
        return False
    parameters = value.get("parameters")
    return (
        isinstance(parameters, Mapping)
        and parameters.get("fire_mode") == "INDIRECT"
    )


def _read_jsonl(path: Path) -> list[Mapping[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _is_numbered_frame(name: str) -> bool:
    return name.endswith(".json") and name[:-5].isdigit()


def _member_sort_key(name: str) -> tuple[int, int | str]:
    if _is_numbered_frame(name):
        return (0, int(name[:-5]))
    return (1, name)


def _validate_archive_output_does_not_alias_facts(
    output: Path,
    authoritative_member_paths: Sequence[Path],
) -> None:
    output_resolved = output.resolve(strict=False)
    for member_path in authoritative_member_paths:
        aliases_member = output_resolved == member_path.resolve(strict=False)
        if not aliases_member and output.exists() and member_path.exists():
            try:
                aliases_member = os.path.samefile(output, member_path)
            except FileNotFoundError:
                # Either path may disappear between the existence and samefile
                # checks; the normal packaging read will then fail safely.
                aliases_member = False
        if aliases_member:
            raise ValueError(
                f"archive output {output} conflicts with authoritative "
                f"replay member {member_path.name}"
            )


def _build_analysis_sidecar(
    member_paths: Sequence[Path],
    analyzer: EightFactorAnalyzer,
) -> bytes:
    """Build and validate the derived sidecar before any ZIP is created."""

    numbered = [
        (int(path.name[:-5]), path)
        for path in member_paths
        if _is_numbered_frame(path.name)
    ]
    if not numbered:
        raise ValueError("analysis sidecar requires numbered frames")

    actual_names = [path.name for _, path in numbered]
    expected_names = [f"{index}.json" for index in range(len(numbered))]
    if actual_names != expected_names:
        raise ValueError(
            "analysis sidecar numbered frames must be continuous from 0"
        )

    manifest_paths = [
        path for path in member_paths
        if path.name == "replay_manifest.native.jsonl"
    ]
    if len(manifest_paths) != 1:
        raise ValueError("analysis sidecar requires one finalized manifest")
    manifest_records = _read_jsonl_payload(manifest_paths[0].read_bytes())
    if len(manifest_records) != 1:
        raise ValueError(
            "analysis sidecar requires one finalized manifest record"
        )
    manifest = manifest_records[0]

    frame_count = manifest.get("frame_count")
    if type(frame_count) is not int or frame_count != len(numbered):
        raise ValueError("analysis sidecar manifest frame_count mismatch")

    expected_hashes = manifest.get("frame_hashes")
    if not isinstance(expected_hashes, list) or len(expected_hashes) != frame_count:
        raise ValueError("analysis sidecar numbered frame hash mismatch")

    frame_analyses = []
    scenario_hash = manifest.get("scenario_hash")
    for frame_index, path in numbered:
        payload = path.read_bytes()
        frame_hash = hashlib.sha256(payload).hexdigest()
        if expected_hashes[frame_index] != frame_hash:
            raise ValueError("analysis sidecar numbered frame hash mismatch")
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError(
                f"analysis sidecar frame {path.name} root must be an object"
            )
        frame_analyses.append(analyzer.analyze_green_frame(
            GreenFrameAnalysisInput(
            frame_index=frame_index,
            frame=value,
            frame_hash=frame_hash,
            scenario_hash=scenario_hash,
            )
        ))

    from wargame_native_engine.analysis.timeline import build_analysis_timeline

    timeline = build_analysis_timeline(tuple(frame_analyses))
    if timeline.frame_indices != tuple(range(frame_count)):
        raise ValueError("analysis sidecar timeline frame index mismatch")
    text = analysis_frames_to_jsonl(timeline.frame_analyses)
    payload = text.encode("utf-8")
    if not payload or not payload.endswith(b"\n"):
        raise ValueError(
            "analysis sidecar JSONL must be non-empty and newline-terminated"
        )
    if len(text.splitlines()) != frame_count:
        raise ValueError("analysis sidecar JSONL frame_count mismatch")
    return payload


def _write_zip_payload(archive: ZipFile, name: str, payload: bytes) -> None:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, payload, compresslevel=9)


def _read_jsonl_payload(payload: bytes) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for line in payload.decode("utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise ValueError("JSONL record must be an object")
        result.append(value)
    return result
