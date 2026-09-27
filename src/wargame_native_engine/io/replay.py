from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence
import warnings

from wargame_native_engine.core.action import EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.result import StepResult
from wargame_native_engine.io.scenario import NativeScenario
from wargame_native_engine.io.serialization import (
    canonical_hash, file_sha256, json_clone,
)
from wargame_native_engine.rules.config import Ruleset, load_ruleset
from wargame_native_engine.rules.indirect_fire import (
    load_indirect_fire_replay_identity,
)
from wargame_native_engine.rules.operator_lifecycle import (
    load_operator_lifecycle_authority,
)
from wargame_native_engine.rules.loitering_munition import (
    load_loitering_munition_attack_authority,
)
from wargame_native_engine.rules.boarding import (
    load_boarding_type_assignment_authority,
)
from wargame_native_engine.version import (
    ADJUDICATION_SEMANTICS_VERSION,
    ENGINE_VERSION,
)

if TYPE_CHECKING:
    from wargame_native_engine.data.map_runtime import MapRuntimeData


REPLAY_SCHEMA_VERSION = "native-replay/2.0"


class UnsupportedReplaySemanticsError(ValueError):
    """The archive requires adjudication semantics this engine cannot run."""


def _ordered_snapshot_paths(root: Path) -> list[Path]:
    """Return snapshots in archive order for current and legacy layouts.

    Snapshot storage identity is deliberately separate from ``WorldState.tick``.
    New archives use a monotonic replay-local step sequence.  Legacy archives
    used the integer-second tick projection and remain readable when their
    filenames are unambiguous.  A mixed layout is rejected instead of being
    silently reinterpreted.
    """

    paths = sorted(root.glob("*.json"))
    if not paths:
        return []
    step_paths = [path for path in paths if path.name.startswith("step-")]
    tick_paths = [path for path in paths if path.name.startswith("tick-")]
    if step_paths:
        if len(step_paths) != len(paths):
            raise ValueError("mixed replay snapshot filename layouts")
        return sorted(
            step_paths,
            key=lambda path: _snapshot_filename_sequence(path, "step-"),
        )
    if tick_paths:
        if len(tick_paths) != len(paths):
            raise ValueError("mixed replay snapshot filename layouts")
        return sorted(
            tick_paths,
            key=lambda path: _snapshot_filename_sequence(path, "tick-"),
        )
    # Preserve support for archives produced before snapshot naming became a
    # declared storage contract.  Lexical ordering was their existing reader
    # behavior and remains deterministic.
    return paths


def _snapshot_filename_sequence(path: Path, prefix: str) -> int:
    value = path.stem.removeprefix(prefix)
    if not value.isdigit():
        raise ValueError(f"invalid replay snapshot filename: {path.name}")
    return int(value)


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
        self._snapshot_sequence = 0
        for name in ("actions.jsonl", "events.jsonl", "steps.jsonl"):
            (self.root / name).write_text("", encoding="utf-8")
        self.manifest: dict[str, Any] = {
            "schema_version": REPLAY_SCHEMA_VERSION,
            "engine_version": ENGINE_VERSION,
            "adjudication_semantics_version": (
                ADJUDICATION_SEMANTICS_VERSION),
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
        boarding_assignment = snapshot.get(
            "boarding_type_assignment_authority"
        )
        if isinstance(boarding_assignment, Mapping):
            self.manifest.update({
                "boarding_type_assignment_authority_version": (
                    boarding_assignment.get("authority_version")
                ),
                "boarding_type_assignment_authority_hash": (
                    boarding_assignment.get("authority_hash")
                ),
                "boarding_type_assignment_policy": (
                    boarding_assignment.get("assignment_policy")
                ),
            })
        if _snapshot_uses_operator_lifecycle(snapshot):
            self.manifest.update(_operator_lifecycle_replay_identity())
        if _snapshot_uses_loitering_attack_authority(snapshot):
            self.manifest.update(_loitering_attack_replay_identity())
        self._write_json("manifest.json", self.manifest)
        self._write_json("initial_state.json", snapshot)
        self.record_snapshot(snapshot)

    def record_step(self, result: StepResult,
                    snapshot: Mapping[str, Any]) -> None:
        if any(_is_indirect_fire_action(item.to_dict()) for item in result.actions):
            self.manifest.update(load_indirect_fire_replay_identity())
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
        sequence = self._snapshot_sequence
        self._snapshot_sequence += 1
        self._write_json(f"snapshots/step-{sequence:08d}.json", snapshot)

    def finalize(self, summary: Mapping[str, Any], *, status: str) -> None:
        self._write_json("episode_summary.json", summary)
        collections = {
            "initial_state.json": file_sha256(self.root / "initial_state.json"),
            "actions.jsonl": file_sha256(self.root / "actions.jsonl"),
            "events.jsonl": file_sha256(self.root / "events.jsonl"),
            "steps.jsonl": file_sha256(self.root / "steps.jsonl"),
            "episode_summary.json": file_sha256(self.root / "episode_summary.json"),
        }
        snapshots = _ordered_snapshot_paths(self.root / "snapshots")
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
                "snapshots": [self._json(item) for item in (
                    _ordered_snapshot_paths(path / "snapshots")
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
    _check_adjudication_semantics(manifest, strict=strict, kind="replay")
    _check_operator_lifecycle_semantics(
        manifest,
        required=_snapshot_uses_operator_lifecycle(bundle["initial_state"]),
        strict=strict,
        kind="replay",
    )
    _check_loitering_attack_semantics(
        manifest,
        required=_snapshot_uses_loitering_attack_authority(
            bundle["initial_state"]
        ),
        strict=strict,
        kind="replay",
    )
    _check_indirect_fire_semantics(
        manifest,
        _classic_replay_actions(bundle),
        strict=strict,
        kind="replay",
    )
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
    boarding_type_assignments = (
        "boarding_type_assignment_authority" in bundle["initial_state"]
    )
    if boarding_type_assignments:
        _check_boarding_type_assignment_semantics(
            manifest, strict=strict, kind="replay"
        )
    engine = NativeEngine(scenario,
                          max_ticks=int(manifest["max_ticks"]),
                          ruleset=active_ruleset,
                          enable_boarding_type_assignments=(
                              boarding_type_assignments
                          ))
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
            delta_ms = int(record["delta_time_ms"])
            if record.get("ordered_action_input", bool(record.get("action_sequences"))):
                result = engine.step_ms(ordered, delta_time_ms=delta_ms)
            else:
                result = engine.step_ms(
                    {item.side: item for item in ordered},
                    delta_time_ms=delta_ms,
                )
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
                          strict: bool = True,
                          map_runtime_data: "MapRuntimeData | None" = None,
                          ) -> ReplayVerification:
    """Strictly replay Native steps embedded beside numbered GREEN frames."""
    from wargame_native_engine.io.importers.source_replay_archive import (
        SourceReplayArchiveImporter,
    )
    archive = SourceReplayArchiveImporter().import_path(source)
    manifest = archive.manifest
    _check_adjudication_semantics(
        manifest, strict=strict, kind="source replay")
    _check_operator_lifecycle_semantics(
        manifest,
        required=any(
            operator.lifecycle_profile_id is not None
            for operator in scenario.operators
        ),
        strict=strict,
        kind="source replay",
    )
    _check_loitering_attack_semantics(
        manifest,
        required=_scenario_uses_loitering_attack_authority(scenario),
        strict=strict,
        kind="source replay",
    )
    _check_indirect_fire_semantics(
        manifest,
        tuple(
            action
            for step in archive.steps
            for action in step.get("ordered_actions", [])
        ),
        strict=strict,
        kind="source replay",
    )
    boarding_type_assignments = (
        manifest.get("boarding_type_assignment_authority_version") is not None
    )
    if boarding_type_assignments:
        _check_boarding_type_assignment_semantics(
            manifest, strict=strict, kind="source replay"
        )
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
    runtime_identity = (
        None
        if map_runtime_data is None
        else map_runtime_data.identity_record()
    )
    if manifest.get("map_runtime_data") != runtime_identity:
        expected["map_runtime_data"] = runtime_identity
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
            map_runtime_data=map_runtime_data,
            enable_boarding_type_assignments=boarding_type_assignments,
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
        bundle, _ = environment.step_views_ms(
            ordered,
            delta_time_ms=_exact_int(
                record.get("delta_time_ms"), "delta_time_ms", minimum=1
            ),
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
        # A non-terminal truncated archive is an explicitly published fact
        # prefix. Truncation is a publication property, not a WorldState rule
        # outcome, so the replayed engine correctly remains non-truncated.
        # Terminal truncation (for example Native max_ticks) is still bound to
        # the engine result below.
        if expected_terminal and actual.truncated != expected_truncated:
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


def _check_adjudication_semantics(
    manifest: Mapping[str, Any], *, strict: bool, kind: str,
) -> None:
    actual = manifest.get("adjudication_semantics_version")
    if actual == ADJUDICATION_SEMANTICS_VERSION:
        return
    message = (
        f"unsupported {kind} adjudication semantics: "
        f"expected {ADJUDICATION_SEMANTICS_VERSION!r}, got {actual!r}"
    )
    if strict:
        raise UnsupportedReplaySemanticsError(message)
    warnings.warn(message + "; continuing in non-strict mode", RuntimeWarning,
                  stacklevel=3)


def _operator_lifecycle_replay_identity() -> dict[str, str]:
    authority = load_operator_lifecycle_authority()
    return {
        "operator_lifecycle_authority_version": authority.authority_version,
        "operator_lifecycle_authority_hash": authority.content_hash,
    }


def _loitering_attack_replay_identity() -> dict[str, str]:
    authority = load_loitering_munition_attack_authority()
    return {
        "loitering_munition_attack_authority_version": (
            authority.authority_version
        ),
        "loitering_munition_attack_authority_hash": authority.content_hash,
    }


def _boarding_type_assignment_replay_identity() -> dict[str, str]:
    authority = load_boarding_type_assignment_authority()
    return {
        "boarding_type_assignment_authority_version": (
            authority.authority_version
        ),
        "boarding_type_assignment_authority_hash": authority.content_hash,
        "boarding_type_assignment_policy": authority.assignment_policy,
    }


def _check_boarding_type_assignment_semantics(
    manifest: Mapping[str, Any], *, strict: bool, kind: str,
) -> None:
    expected = _boarding_type_assignment_replay_identity()
    mismatches = tuple(
        key for key, value in expected.items() if manifest.get(key) != value
    )
    if not mismatches:
        return
    message = (
        f"unsupported {kind} boarding type assignment authority: "
        + ", ".join(mismatches)
    )
    if strict:
        raise UnsupportedReplaySemanticsError(message)
    warnings.warn(message + "; continuing in non-strict mode", RuntimeWarning,
                  stacklevel=3)


def _snapshot_uses_operator_lifecycle(snapshot: Mapping[str, Any]) -> bool:
    world = snapshot.get("world")
    if not isinstance(world, Mapping):
        return False
    operators = world.get("operators")
    if not isinstance(operators, Mapping):
        return False
    return any(
        isinstance(value, Mapping)
        and value.get("lifecycle_profile_id") is not None
        for value in operators.values()
    )


def _snapshot_uses_loitering_attack_authority(
    snapshot: Mapping[str, Any],
) -> bool:
    world = snapshot.get("world")
    if not isinstance(world, Mapping):
        return False
    operators = world.get("operators")
    if not isinstance(operators, Mapping):
        return False
    authority = load_loitering_munition_attack_authority()
    for value in operators.values():
        if not isinstance(value, Mapping):
            continue
        profile_id = value.get("lifecycle_profile_id")
        weapon_ids = value.get("weapon_ids", ())
        if not isinstance(weapon_ids, list):
            continue
        if any(
            type(weapon_id) is int
            and authority.policy_for(
                lifecycle_profile_id=(
                    None if profile_id is None else str(profile_id)
                ),
                weapon_id=weapon_id,
            ) is not None
            for weapon_id in weapon_ids
        ):
            return True
    return False


def _scenario_uses_loitering_attack_authority(scenario: NativeScenario) -> bool:
    authority = load_loitering_munition_attack_authority()
    return any(
        authority.policy_for(
            lifecycle_profile_id=operator.lifecycle_profile_id,
            weapon_id=weapon_id,
        ) is not None
        for operator in scenario.operators
        for weapon_id in operator.weapon_ids
    )


def _check_operator_lifecycle_semantics(
    manifest: Mapping[str, Any],
    *,
    required: bool,
    strict: bool,
    kind: str,
) -> None:
    if not required:
        return
    expected = _operator_lifecycle_replay_identity()
    mismatches = tuple(
        key for key, value in expected.items() if manifest.get(key) != value
    )
    if not mismatches:
        return
    message = (
        f"unsupported {kind} operator lifecycle authority: "
        + ", ".join(mismatches)
    )
    if strict:
        raise UnsupportedReplaySemanticsError(message)
    warnings.warn(message + "; continuing in non-strict mode", RuntimeWarning,
                  stacklevel=3)


def _check_loitering_attack_semantics(
    manifest: Mapping[str, Any],
    *,
    required: bool,
    strict: bool,
    kind: str,
) -> None:
    if not required:
        return
    expected = _loitering_attack_replay_identity()
    mismatches = tuple(
        key for key, value in expected.items() if manifest.get(key) != value
    )
    if not mismatches:
        return
    message = (
        f"unsupported {kind} loitering-munition attack authority: "
        + ", ".join(mismatches)
    )
    if strict:
        raise UnsupportedReplaySemanticsError(message)
    warnings.warn(message + "; continuing in non-strict mode", RuntimeWarning,
                  stacklevel=3)


def _check_indirect_fire_semantics(
    manifest: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    *,
    strict: bool,
    kind: str,
) -> None:
    if not any(_is_indirect_fire_action(value) for value in actions):
        return
    expected = load_indirect_fire_replay_identity()
    mismatches = tuple(
        key for key, value in expected.items()
        if manifest.get(key) != value
    )
    if not mismatches:
        return
    message = (
        f"unsupported {kind} indirect-fire authority: "
        + ", ".join(mismatches)
    )
    if strict:
        raise UnsupportedReplaySemanticsError(message)
    warnings.warn(message + "; continuing in non-strict mode", RuntimeWarning,
                  stacklevel=3)


def _classic_replay_actions(
    bundle: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    steps = bundle.get("steps", ())
    if steps:
        return tuple(
            action
            for step in steps
            for action in step.get("ordered_actions", [])
        )
    return tuple(
        record.get("action", {})
        for record in bundle.get("actions", ())
    )


def _is_indirect_fire_action(value: Mapping[str, Any]) -> bool:
    if not isinstance(value, Mapping) or value.get("action_type") != "FIRE":
        return False
    parameters = value.get("parameters")
    return (
        isinstance(parameters, Mapping)
        and parameters.get("fire_mode") == "INDIRECT"
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
