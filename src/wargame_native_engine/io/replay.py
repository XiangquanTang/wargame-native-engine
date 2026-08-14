from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
import warnings

from wargame_native_engine.core.action import EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.result import StepResult
from wargame_native_engine.io.scenario import NativeScenario
from wargame_native_engine.io.serialization import (
    canonical_hash, file_sha256, json_clone,
)
from wargame_native_engine.rules.config import Ruleset, load_ruleset
from wargame_native_engine.version import ENGINE_VERSION


REPLAY_SCHEMA_VERSION = "native-replay/2.0"


class ReplayWriter:
    def __init__(self, root: str | Path, *, run_id: str,
                 scenario: NativeScenario, seed: int,
                 agents: Mapping[str, str], max_ticks: int,
                 ruleset: Ruleset,
                 project_commit: str | None,
                 agent_seeds: Mapping[str, int | None] | None = None) -> None:
        self.root = Path(root)
        if self.root.exists() and any(self.root.iterdir()):
            raise FileExistsError(f"refusing to overwrite non-empty replay: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "snapshots").mkdir()
        for name in ("actions.jsonl", "events.jsonl", "steps.jsonl"):
            (self.root / name).write_text("", encoding="utf-8")
        self.manifest: dict[str, Any] = {
            "schema_version": REPLAY_SCHEMA_VERSION,
            "engine_version": ENGINE_VERSION,
            "ruleset_id": ruleset.ruleset_id,
            "ruleset_version": ruleset.ruleset_version,
            "ruleset_hash": ruleset.content_hash,
            "run_id": run_id,
            "scenario_id": scenario.scenario_id,
            "scenario_hash": scenario.scenario_hash,
            "map_id": scenario.map.map_id,
            "seed": int(seed),
            "max_ticks": int(max_ticks),
            "agents": dict(agents),
            "agent_seeds": dict(agent_seeds or {}),
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
        decisions: dict[str, list[Any]] = {}
        for item in result.decisions:
            decisions.setdefault(item.action_id, []).append(item)
        for index, action in enumerate(result.actions):
            decision = decisions[action.action_id].pop(0)
            self._append("actions.jsonl", {
                "schema_version": "native-action/1.0",
                "tick_before": result.tick_before,
                "tick_after": result.tick_after,
                "action_id": action.action_id,
                "side": action.side,
                "action": action.to_dict(),
                "accepted": decision.accepted,
                "decision_code": decision.code,
                "action_sequence": (None if not result.action_sequences else
                                    result.action_sequences[index]),
                "delta_time_ms": result.delta_time_ms,
            })
        for event in result.events:
            self._append("events.jsonl", event.to_dict())
        self.record_snapshot(snapshot)
        self._append("steps.jsonl", {
            "schema_version": "native-step/1.0",
            "tick_before": result.tick_before,
            "tick_after": result.tick_after,
            "delta_time_ms": result.delta_time_ms,
            "ordered_actions": [item.to_dict() for item in result.actions],
            "action_sequences": list(result.action_sequences),
            "ordered_action_input": result.ordered_action_input,
            "world_hash": canonical_hash(snapshot["world"]),
        })

    def record_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        tick = int(snapshot["world"]["tick"])
        self._write_json(f"snapshots/tick-{tick:08d}.json", snapshot)

    def finalize(self, summary: Mapping[str, Any], *, status: str) -> None:
        self._write_json("episode_summary.json", summary)
        collections = {
            "initial_state.json": file_sha256(self.root / "initial_state.json"),
            "actions.jsonl": file_sha256(self.root / "actions.jsonl"),
            "events.jsonl": file_sha256(self.root / "events.jsonl"),
            "steps.jsonl": file_sha256(self.root / "steps.jsonl"),
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
                "snapshots": [self._json(item) for item in sorted(
                    (path / "snapshots").glob("*.json")
                )],
                "actions": self._jsonl(path / "actions.jsonl"),
                "events": self._jsonl(path / "events.jsonl"),
                "steps": (self._jsonl(path / "steps.jsonl")
                          if (path / "steps.jsonl").is_file() else []),
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


def replay_actions(scenario: NativeScenario, root: str | Path, *,
                   ruleset: Ruleset | str | Path | None = None,
                   strict: bool = True,
                   seed: int | None = None) -> ReplayVerification:
    bundle = ReplayReader().read(root)
    active_ruleset = (ruleset if isinstance(ruleset, Ruleset)
                      else load_ruleset(ruleset))
    manifest = bundle["manifest"]
    mismatches: list[str] = []
    expected = {
        "scenario_hash": scenario.scenario_hash,
        "ruleset_id": active_ruleset.ruleset_id,
        "ruleset_version": active_ruleset.ruleset_version,
        "ruleset_hash": active_ruleset.content_hash,
    }
    for name, value in expected.items():
        if manifest.get(name) != value:
            mismatches.append(name)
    if bundle["initial_state"].get("seed") != manifest.get("seed"):
        mismatches.append("seed")
    if seed is not None and int(manifest.get("seed")) != int(seed):
        mismatches.append("seed")
    if mismatches:
        message = "replay compatibility mismatch: " + ", ".join(sorted(set(mismatches)))
        if strict:
            raise ValueError(message)
        warnings.warn(message + "; continuing in non-strict mode", RuntimeWarning,
                      stacklevel=2)
    engine = NativeEngine(scenario,
                          max_ticks=int(manifest["max_ticks"]),
                          ruleset=active_ruleset)
    engine.reset(int(manifest["seed"]))
    if strict and canonical_hash(engine.snapshot()) != canonical_hash(
        bundle["initial_state"]
    ):
        engine.close()
        raise ValueError("replay initial state mismatch")
    if bundle.get("steps"):
        generated_actions: list[Mapping[str, Any]] = []
        generated_events: list[Mapping[str, Any]] = []
        snapshots = bundle.get("snapshots", [])
        if strict and len(snapshots) != len(bundle["steps"]) + 1:
            engine.close()
            raise ValueError("replay snapshot/step count mismatch")
        if strict and snapshots and canonical_hash(snapshots[0]) != canonical_hash(
            engine.snapshot()
        ):
            engine.close()
            raise ValueError("replay initial snapshot mismatch")
        for offset, record in enumerate(bundle["steps"], start=1):
            if engine.is_terminal():
                engine.close()
                raise ValueError(
                    f"replay contains step after terminal at step {offset}"
                )
            _validate_classic_step_record(record, offset)
            ordered = [EngineAction.from_dict(item)
                       for item in record["ordered_actions"]]
            delta_seconds = int(record["delta_time_ms"]) / 1000
            if record.get("ordered_action_input", bool(record.get("action_sequences"))):
                result = engine.step(ordered, delta_time_seconds=delta_seconds)
            else:
                result = engine.step({item.side: item for item in ordered},
                                     delta_time_seconds=delta_seconds)
            snapshot = engine.snapshot()
            if strict:
                actual_step = {
                    "schema_version": "native-step/1.0",
                    "tick_before": result.tick_before,
                    "tick_after": result.tick_after,
                    "delta_time_ms": result.delta_time_ms,
                    "ordered_actions": [item.to_dict() for item in result.actions],
                    "action_sequences": list(result.action_sequences),
                    "ordered_action_input": result.ordered_action_input,
                    "world_hash": canonical_hash(snapshot["world"]),
                }
                if json_clone(record) != actual_step:
                    engine.close()
                    raise ValueError(f"replay step mismatch at step {offset}")
                if canonical_hash(snapshots[offset]) != canonical_hash(snapshot):
                    engine.close()
                    raise ValueError(f"replay snapshot mismatch at step {offset}")
            generated_actions.extend(_classic_action_records(result))
            generated_events.extend(item.to_dict() for item in result.events)
        if strict and json_clone(bundle["actions"]) != json_clone(generated_actions):
            engine.close()
            raise ValueError("replay action trajectory mismatch")
        if strict and json_clone(bundle["events"]) != json_clone(generated_events):
            engine.close()
            raise ValueError("replay event trajectory mismatch")
    else:
        if strict and manifest.get("schema_version") == REPLAY_SCHEMA_VERSION:
            engine.close()
            raise ValueError("strict native replay requires steps.jsonl")
        grouped: dict[int, dict[str, EngineAction]] = {}
        for record in bundle["actions"]:
            grouped.setdefault(int(record["tick_before"]), {})[str(record["side"])] = (
                EngineAction.from_dict(record["action"]))
        for tick in sorted(grouped):
            if engine.is_terminal():
                break
            engine.step(grouped[tick])
    actual = engine.result()
    expected_hash = str(bundle["summary"]["result"]["world_hash"])
    if strict:
        expected_result = bundle["summary"].get("result", {})
        if actual.to_dict() != expected_result:
            engine.close()
            raise ValueError("replay final result mismatch")
    engine.close()
    return ReplayVerification(expected_hash == actual.world_hash, expected_hash,
                              actual.world_hash, actual.ticks)


def replay_source_archive(scenario: NativeScenario, source: str | Path, *,
                          ruleset: Ruleset | str | Path | None = None,
                          strict: bool = True) -> ReplayVerification:
    """Strictly replay Native steps embedded beside numbered GREEN frames."""
    from wargame_native_engine.io.importers.source_replay_archive import (
        SourceReplayArchiveImporter,
    )
    archive = SourceReplayArchiveImporter().import_path(source)
    manifest = archive.manifest
    active_ruleset = (ruleset if isinstance(ruleset, Ruleset)
                      else load_ruleset(ruleset))
    expected = {
        "scenario_id": scenario.scenario_id,
        "scenario_hash": scenario.scenario_hash,
        "ruleset_id": active_ruleset.ruleset_id,
        "ruleset_version": active_ruleset.ruleset_version,
        "ruleset_hash": active_ruleset.content_hash,
        "perspective": "GREEN",
    }
    mismatches = [name for name, value in expected.items()
                  if manifest.get(name) != value]
    if not archive.steps:
        mismatches.append("steps.jsonl")
    if mismatches:
        message = "source replay compatibility mismatch: " + ", ".join(
            sorted(set(mismatches)))
        if strict:
            raise ValueError(message)
        warnings.warn(message + "; continuing in non-strict mode", RuntimeWarning,
                      stacklevel=2)
    from wargame_native_engine.backends.native import NativeEngineBackend
    from wargame_native_engine.session.game_session import GameSession
    from wargame_native_engine.session.source_environment import (
        SourceCompatibleEnvironment,
    )

    environment = SourceCompatibleEnvironment(
        GameSession(NativeEngineBackend(
            scenario,
            max_ticks=int(manifest.get(
                "max_ticks", scenario.terminal_rules.max_ticks)),
            ruleset=active_ruleset,
        )),
        template=archive.frames[0],
    )
    bundle = environment.setup_views(_exact_int(
        manifest.get("episode_seed"), "episode_seed", minimum=0
    ))
    if strict:
        if environment.initial_state_hash != manifest.get("initial_state_hash"):
            environment.session.close()
            raise ValueError("source replay initial state hash mismatch")
        _verify_green_frame_hash(bundle.green, archive.frames[0].source_hash, 0)

    for offset, record in enumerate(archive.steps, start=1):
        if environment.session.is_terminal():
            environment.session.close()
            raise ValueError(
                f"source replay contains step after terminal at step {offset}"
            )
        _validate_source_step_record(record, offset)
        ordered = [EngineAction.from_dict(value)
                   for value in record.get("ordered_actions", [])]
        bundle, _ = environment.step_views(
            ordered,
            delta_time_seconds=_exact_int(
                record.get("delta_time_ms"), "delta_time_ms", minimum=1
            ) / 1000,
        )
        actual_record = environment.last_step_record
        if actual_record is None:
            environment.session.close()
            raise RuntimeError("strict replay did not produce a step record")
        if strict:
            comparable_keys = [
                "tick_before", "tick_after", "delta_time_ms",
                "ordered_actions", "action_sequences",
                "ordered_action_input", "expected_state_hash",
            ]
            if "sim_time_ms" in record:
                comparable_keys.append("sim_time_ms")
            for key in comparable_keys:
                if json_clone(record.get(key)) != json_clone(actual_record.get(key)):
                    environment.session.close()
                    raise ValueError(
                        f"source replay {key} mismatch at step {offset}"
                    )
            _verify_green_frame_hash(
                bundle.green, archive.frames[offset].source_hash, offset
            )

    actual = environment.session.result()
    expected_hash = str(manifest["final_state_hash"])
    if strict:
        expected_terminal = bool(manifest.get("termination", False))
        expected_truncated = bool(manifest.get("truncated", False))
        if actual.terminal != expected_terminal:
            environment.session.close()
            raise ValueError("source replay terminal status mismatch")
        if actual.truncated != expected_truncated:
            environment.session.close()
            raise ValueError("source replay truncated status mismatch")
        if actual.winner != manifest.get("winner"):
            environment.session.close()
            raise ValueError("source replay winner mismatch")
    environment.session.close()
    return ReplayVerification(expected_hash == actual.world_hash, expected_hash,
                              actual.world_hash, actual.ticks)


def _verify_green_frame_hash(
    frame: Mapping[str, Any],
    expected_hash: str,
    frame_index: int,
) -> None:
    from wargame_native_engine.compat.source_frame.serialization import (
        canonical_green_frame_payload,
    )

    actual_hash = hashlib.sha256(canonical_green_frame_payload(frame)).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError(
            f"source replay GREEN frame hash mismatch at frame {frame_index}"
        )


def _validate_source_step_record(record: Mapping[str, Any], offset: int) -> None:
    if not isinstance(record, Mapping):
        raise ValueError(f"source replay step {offset} must be an object")
    if _exact_int(record.get("step_index"), "step_index", minimum=1) != offset:
        raise ValueError(f"source replay step_index mismatch at step {offset}")
    if "frame_index" in record and _exact_int(
        record.get("frame_index"), "frame_index", minimum=1
    ) != offset:
        raise ValueError(f"source replay frame_index mismatch at step {offset}")
    before = _exact_int(record.get("tick_before"), "tick_before", minimum=0)
    after = _exact_int(record.get("tick_after"), "tick_after", minimum=0)
    if after < before:
        raise ValueError(f"source replay tick order mismatch at step {offset}")
    _exact_int(record.get("delta_time_ms"), "delta_time_ms", minimum=1)
    if "sim_time_ms" in record:
        _exact_int(record.get("sim_time_ms"), "sim_time_ms", minimum=1)
    if "decision_window_index" in record:
        _exact_int(
            record.get("decision_window_index"),
            "decision_window_index",
            minimum=0,
        )
    if "frame_interval_offset" in record:
        _exact_int(
            record.get("frame_interval_offset"),
            "frame_interval_offset",
            minimum=0,
        )
    actions = record.get("ordered_actions")
    sequences = record.get("action_sequences")
    if not isinstance(actions, list) or not isinstance(sequences, list):
        raise ValueError(f"source replay action lists invalid at step {offset}")
    if len(actions) != len(sequences):
        raise ValueError(f"source replay action sequence length mismatch at step {offset}")
    for value in sequences:
        _exact_int(value, "action_sequence", minimum=0)
    if type(record.get("ordered_action_input")) is not bool:
        raise ValueError(f"source replay ordered_action_input invalid at step {offset}")
    state_hash = record.get("expected_state_hash")
    if not isinstance(state_hash, str) or not state_hash:
        raise ValueError(f"source replay expected_state_hash invalid at step {offset}")


def _exact_int(value: Any, field: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(
            f"source replay {field} must be an exact int >= {minimum}"
        )
    return value


def _validate_classic_step_record(record: Mapping[str, Any], offset: int) -> None:
    if not isinstance(record, Mapping):
        raise ValueError(f"replay step {offset} must be an object")
    before = _exact_int(record.get("tick_before"), "tick_before", minimum=0)
    after = _exact_int(record.get("tick_after"), "tick_after", minimum=0)
    if after < before:
        raise ValueError(f"replay tick order mismatch at step {offset}")
    _exact_int(record.get("delta_time_ms"), "delta_time_ms", minimum=1)
    actions = record.get("ordered_actions")
    sequences = record.get("action_sequences")
    if not isinstance(actions, list) or not isinstance(sequences, list):
        raise ValueError(f"replay action lists invalid at step {offset}")
    if sequences and len(sequences) != len(actions):
        raise ValueError(f"replay action sequence length mismatch at step {offset}")
    if type(record.get("ordered_action_input")) is not bool:
        raise ValueError(f"replay ordered_action_input invalid at step {offset}")
    world_hash = record.get("world_hash")
    if not isinstance(world_hash, str) or not world_hash:
        raise ValueError(f"replay world_hash invalid at step {offset}")


def _classic_action_records(result: StepResult) -> list[Mapping[str, Any]]:
    decisions: dict[str, list[Any]] = {}
    for item in result.decisions:
        decisions.setdefault(item.action_id, []).append(item)
    records: list[Mapping[str, Any]] = []
    for index, action in enumerate(result.actions):
        decision = decisions[action.action_id].pop(0)
        records.append({
            "schema_version": "native-action/1.0",
            "tick_before": result.tick_before,
            "tick_after": result.tick_after,
            "action_id": action.action_id,
            "side": action.side,
            "action": action.to_dict(),
            "accepted": decision.accepted,
            "decision_code": decision.code,
            "action_sequence": (
                None if not result.action_sequences
                else result.action_sequences[index]
            ),
            "delta_time_ms": result.delta_time_ms,
        })
    return records
