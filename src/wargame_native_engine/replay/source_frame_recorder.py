from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from wargame_native_engine.io.serialization import canonical_hash, file_sha256, json_clone


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

    def package_zip(self, output: str | Path | None = None) -> Path:
        if not self._finalized:
            raise RuntimeError("recorder_not_finalized")
        target = Path(output) if output is not None else self.root / "replay.zip"
        members = [path for path in self.root.iterdir()
                   if path.is_file() and path != target
                   and (_is_numbered_frame(path.name)
                        or path.name in {"steps.jsonl",
                                         "replay_manifest.native.jsonl"})]
        members.sort(key=lambda path: _member_sort_key(path.name))
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=target.parent)
        os.close(descriptor)
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED,
                         compresslevel=9) as archive:
                for path in members:
                    info = ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, path.read_bytes(), compresslevel=9)
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
