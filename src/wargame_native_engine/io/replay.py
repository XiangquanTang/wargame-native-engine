from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.core.action import EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.result import StepResult
from wargame_native_engine.io.scenario import NativeScenario
from wargame_native_engine.io.serialization import (
    canonical_hash, file_sha256, json_clone,
)
from wargame_native_engine.version import ENGINE_VERSION, RULESET_VERSION


REPLAY_SCHEMA_VERSION = "native-replay/1.0"


class ReplayWriter:
    def __init__(self, root: str | Path, *, run_id: str,
                 scenario: NativeScenario, seed: int,
                 agents: Mapping[str, str], max_ticks: int,
                 project_commit: str | None) -> None:
        self.root = Path(root)
        if self.root.exists() and any(self.root.iterdir()):
            raise FileExistsError(f"refusing to overwrite non-empty replay: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "snapshots").mkdir()
        for name in ("actions.jsonl", "events.jsonl"):
            (self.root / name).write_text("", encoding="utf-8")
        self.manifest: dict[str, Any] = {
            "schema_version": REPLAY_SCHEMA_VERSION,
            "engine_version": ENGINE_VERSION,
            "ruleset_version": RULESET_VERSION,
            "run_id": run_id,
            "scenario_id": scenario.scenario_id,
            "scenario_hash": scenario.scenario_hash,
            "map_id": scenario.map.map_id,
            "seed": int(seed),
            "max_ticks": int(max_ticks),
            "agents": dict(agents),
            "project_commit": project_commit,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "collection_hashes": {},
        }
        self._write_json("manifest.json", self.manifest)

    def record_initial_state(self, snapshot: Mapping[str, Any]) -> None:
        self._write_json("initial_state.json", snapshot)
        self.record_snapshot(snapshot)

    def record_step(self, result: StepResult,
                    snapshot: Mapping[str, Any]) -> None:
        decisions = {item.action_id: item for item in result.decisions}
        for action in result.actions:
            decision = decisions[action.action_id]
            self._append("actions.jsonl", {
                "schema_version": "native-action/1.0",
                "tick_before": result.tick_before,
                "tick_after": result.tick_after,
                "action_id": action.action_id,
                "side": action.side,
                "action": action.to_dict(),
                "accepted": decision.accepted,
                "decision_code": decision.code,
            })
        for event in result.events:
            self._append("events.jsonl", event.to_dict())
        self.record_snapshot(snapshot)

    def record_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        tick = int(snapshot["world"]["tick"])
        self._write_json(f"snapshots/tick-{tick:08d}.json", snapshot)

    def finalize(self, summary: Mapping[str, Any], *, status: str) -> None:
        self._write_json("episode_summary.json", summary)
        collections = {
            "initial_state.json": file_sha256(self.root / "initial_state.json"),
            "actions.jsonl": file_sha256(self.root / "actions.jsonl"),
            "events.jsonl": file_sha256(self.root / "events.jsonl"),
            "episode_summary.json": file_sha256(self.root / "episode_summary.json"),
        }
        snapshots = sorted((self.root / "snapshots").glob("*.json"))
        collections.update({str(path.relative_to(self.root)): file_sha256(path)
                            for path in snapshots})
        self.manifest.update({"status": status,
                              "finished_at": datetime.now(timezone.utc).isoformat(),
                              "collection_hashes": collections,
                              "result_hash": canonical_hash(summary)})
        self._write_json("manifest.json", self.manifest)
        files = sorted(path for path in self.root.rglob("*")
                       if path.is_file() and path.name != "checksums.sha256")
        (self.root / "checksums.sha256").write_text(
            "\n".join(f"{file_sha256(path)}  {path.relative_to(self.root)}"
                      for path in files) + "\n", encoding="utf-8")

    def _append(self, name: str, value: Mapping[str, Any]) -> None:
        with (self.root / name).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(json_clone(value), ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")) + "\n")

    def _write_json(self, name: str, value: Mapping[str, Any]) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(json_clone(value), ensure_ascii=False,
                                   sort_keys=True, indent=2) + "\n",
                        encoding="utf-8")


class ReplayReader:
    def read(self, root: str | Path) -> Mapping[str, Any]:
        path = Path(root)
        manifest = self._json(path / "manifest.json")
        for name, expected in manifest.get("collection_hashes", {}).items():
            if file_sha256(path / name) != expected:
                raise ValueError(f"replay hash mismatch: {name}")
        summary = self._json(path / "episode_summary.json")
        if manifest.get("result_hash") != canonical_hash(summary):
            raise ValueError("replay result hash mismatch")
        return {"manifest": manifest,
                "initial_state": self._json(path / "initial_state.json"),
                "actions": self._jsonl(path / "actions.jsonl"),
                "events": self._jsonl(path / "events.jsonl"),
                "summary": summary}

    @staticmethod
    def _json(path: Path) -> Mapping[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError(f"JSON root is not an object: {path}")
        return value

    @staticmethod
    def _jsonl(path: Path) -> list[Mapping[str, Any]]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]


@dataclass(frozen=True, slots=True)
class ReplayVerification:
    matches: bool
    expected_world_hash: str
    actual_world_hash: str
    ticks: int


def replay_actions(scenario: NativeScenario,
                   root: str | Path) -> ReplayVerification:
    bundle = ReplayReader().read(root)
    if bundle["manifest"]["scenario_hash"] != scenario.scenario_hash:
        raise ValueError("replay scenario hash mismatch")
    engine = NativeEngine(scenario,
                          max_ticks=int(bundle["manifest"]["max_ticks"]))
    engine.reset(int(bundle["manifest"]["seed"]))
    grouped: dict[int, dict[str, EngineAction]] = {}
    for record in bundle["actions"]:
        grouped.setdefault(int(record["tick_before"]), {})[str(record["side"])] = (
            EngineAction.from_dict(record["action"]))
    for tick in sorted(grouped):
        if engine.is_terminal():
            break
        engine.step(grouped[tick])
    actual = engine.result()
    expected = str(bundle["summary"]["result"]["world_hash"])
    engine.close()
    return ReplayVerification(expected == actual.world_hash, expected,
                              actual.world_hash, actual.ticks)
