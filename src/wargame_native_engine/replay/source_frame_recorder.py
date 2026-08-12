from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from wargame_native_engine.analysis.analyzer import (
    EightFactorAnalyzer,
    GreenFrameAnalysisInput,
)
from wargame_native_engine.analysis.jsonl import analysis_frames_to_jsonl
from wargame_native_engine.io.serialization import canonical_hash, file_sha256, json_clone


ANALYSIS_SIDECAR_NAME = "frame_analysis.native.jsonl"


class SourceFrameRecorder:
    """Observer-only writer for numbered GREEN Source-compatible frames."""

    replay_format = "native-source-frame-replay/1.0"
    frame_contract = "source-frame-compat/1.0"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._started = False
        self._finalized = False
        self._frame_hashes: list[str] = []
        self._metadata: dict[str, Any] = {}
        self._initial_state_hash: str | None = None

    def start(self, metadata: Mapping[str, Any], *,
              started_at: str | None = None) -> None:
        if self._started:
            raise RuntimeError("recorder_already_started")
        if self.root.exists() and any(self.root.iterdir()):
            raise FileExistsError(f"refusing to overwrite recorder output: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        self._metadata = json_clone(metadata)
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
        required = {"delta_time_ms", "ordered_actions", "expected_state_hash"}
        missing = sorted(required - set(record))
        if missing:
            raise ValueError(f"step record missing fields: {missing}")
        self._record_frame(green_frame)
        self._append_jsonl(self.root / "steps.jsonl", record)

    def finalize(self, outcome: Mapping[str, Any], *,
                 final_state_hash: str) -> Mapping[str, Any]:
        self._require_active()
        if not self._frame_hashes:
            raise RuntimeError("recorder_has_no_frames")
        if self._finalized:
            raise RuntimeError("recorder_already_finalized")
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
        member_paths = authoritative_member_paths
        member_paths.sort(key=lambda path: _member_sort_key(path.name))
        members = [(path.name, path.read_bytes()) for path in member_paths]

        if analysis_analyzer is not None:
            sidecar_payload = _build_analysis_sidecar(
                members,
                analysis_analyzer,
            )
            first_fact_sidecar = next(
                index for index, (name, _) in enumerate(members)
                if not _is_numbered_frame(name)
            )
            members.insert(
                first_fact_sidecar,
                (ANALYSIS_SIDECAR_NAME, sidecar_payload),
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=target.parent)
        os.close(descriptor)
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED,
                         compresslevel=9) as archive:
                for name, payload in members:
                    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, payload, compresslevel=9)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target

    def _record_frame(self, frame: Mapping[str, Any]) -> None:
        value = json_clone(frame)
        perspective = value.get("extra", {}).get("native", {}).get("perspective")
        if perspective != "GREEN":
            raise ValueError("recorder accepts GREEN frames only")
        index = len(self._frame_hashes)
        payload = (_canonical_json(value) + "\n").encode("utf-8")
        self._atomic_write(self.root / f"{index}.json", payload)
        self._frame_hashes.append(hashlib.sha256(payload).hexdigest())

    def _require_active(self) -> None:
        if not self._started:
            raise RuntimeError("recorder_not_started")
        if self._finalized:
            raise RuntimeError("recorder_already_finalized")

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
        existing = path.read_bytes() if path.exists() else b""
        cls._atomic_write(path, existing + (_canonical_json(value) + "\n").encode(
            "utf-8"))


def _canonical_json(value: Any) -> str:
    return json.dumps(json_clone(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


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
    members: list[tuple[str, bytes]],
    analyzer: EightFactorAnalyzer,
) -> bytes:
    """Build and validate the derived sidecar before any ZIP is created."""

    numbered = [
        (int(name[:-5]), name, payload)
        for name, payload in members
        if _is_numbered_frame(name)
    ]
    if not numbered:
        raise ValueError("analysis sidecar requires numbered frames")

    actual_names = [name for _, name, _ in numbered]
    expected_names = [f"{index}.json" for index in range(len(numbered))]
    if actual_names != expected_names:
        raise ValueError(
            "analysis sidecar numbered frames must be continuous from 0"
        )

    manifest_payloads = [
        payload for name, payload in members
        if name == "replay_manifest.native.jsonl"
    ]
    if len(manifest_payloads) != 1:
        raise ValueError("analysis sidecar requires one finalized manifest")
    manifest_records = _read_jsonl_payload(manifest_payloads[0])
    if len(manifest_records) != 1:
        raise ValueError(
            "analysis sidecar requires one finalized manifest record"
        )
    manifest = manifest_records[0]

    frame_count = manifest.get("frame_count")
    if type(frame_count) is not int or frame_count != len(numbered):
        raise ValueError("analysis sidecar manifest frame_count mismatch")

    frame_hashes = [
        hashlib.sha256(payload).hexdigest()
        for _, _, payload in numbered
    ]
    if manifest.get("frame_hashes") != frame_hashes:
        raise ValueError("analysis sidecar numbered frame hash mismatch")

    inputs: list[GreenFrameAnalysisInput] = []
    scenario_hash = manifest.get("scenario_hash")
    for frame_index, name, payload in numbered:
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError(
                f"analysis sidecar frame {name} root must be an object"
            )
        inputs.append(GreenFrameAnalysisInput(
            frame_index=frame_index,
            frame=value,
            frame_hash=frame_hashes[frame_index],
            scenario_hash=scenario_hash,
        ))

    timeline = analyzer.analyze_green_frames(tuple(inputs))
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
