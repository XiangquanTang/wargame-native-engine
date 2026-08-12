from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from wargame_native_engine.io.serialization import (
    canonical_hash, file_sha256, json_clone,
)


HISTORICAL_IMPORTER_VERSION = "historical-replay/1.0.0"


class EvidenceLevel(str, Enum):
    ACTION_LEVEL = "ACTION_LEVEL"
    STATE_TRAJECTORY_LEVEL = "STATE_TRAJECTORY_LEVEL"
    EVENT_LEVEL = "EVENT_LEVEL"
    SUMMARY_ONLY = "SUMMARY_ONLY"


class MappingStatus(str, Enum):
    DIRECT = "DIRECT"
    NORMALIZED = "NORMALIZED"
    DERIVED = "DERIVED"
    APPROXIMATED = "APPROXIMATED"
    UNMAPPED = "UNMAPPED"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class HistoricalOperatorState:
    operator_id: int
    side: str
    operator_type: str
    x: float | None
    y: float | None
    health: float | None
    max_health: float | None
    alive: bool | None
    state: str | None
    weapon_ids: tuple[int, ...] = ()
    field_mapping: Mapping[str, str] = field(default_factory=dict)
    source_raw: Mapping[str, Any] = field(default_factory=dict)
    normalized_historical: Mapping[str, Any] = field(default_factory=dict)
    native_comparable: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return json_clone({
            "operator_id": self.operator_id, "side": self.side,
            "operator_type": self.operator_type, "x": self.x, "y": self.y,
            "health": self.health, "max_health": self.max_health,
            "alive": self.alive, "state": self.state,
            "weapon_ids": list(self.weapon_ids),
            "field_mapping": self.field_mapping, "source_raw": self.source_raw,
            "normalized_historical": self.normalized_historical,
            "native_comparable": self.native_comparable,
        })


@dataclass(frozen=True, slots=True)
class HistoricalFrame:
    historical_time: float
    operators: tuple[HistoricalOperatorState, ...]
    advantage: float | None = None
    source_raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"historical_time": self.historical_time,
                "operators": [item.to_dict() for item in self.operators],
                "advantage": self.advantage,
                "source_raw": json_clone(self.source_raw)}


@dataclass(frozen=True, slots=True)
class HistoricalAction:
    historical_time: float
    side: str
    actor_id: int
    action_type: str
    target_id: int | None = None
    target_position: tuple[float, float] | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    mapping_status: str = MappingStatus.DIRECT.value
    source_kind: str = "COMMAND"
    execution_status: str = "CONFIRMED"
    executable: bool = True
    source_raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return json_clone({
            "historical_time": self.historical_time, "side": self.side,
            "actor_id": self.actor_id, "action_type": self.action_type,
            "target_id": self.target_id,
            "target_position": (None if self.target_position is None
                                else list(self.target_position)),
            "parameters": self.parameters, "mapping_status": self.mapping_status,
            "source_kind": self.source_kind,
            "execution_status": self.execution_status,
            "executable": self.executable, "source_raw": self.source_raw,
        })


@dataclass(frozen=True, slots=True)
class HistoricalEvent:
    historical_time: float
    event_type: str
    side: str | None = None
    actor_id: int | None = None
    target_id: int | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    mapping_status: str = MappingStatus.DIRECT.value
    source_raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return json_clone({
            "historical_time": self.historical_time,
            "event_type": self.event_type, "side": self.side,
            "actor_id": self.actor_id, "target_id": self.target_id,
            "data": self.data, "mapping_status": self.mapping_status,
            "source_raw": self.source_raw,
        })


@dataclass(frozen=True, slots=True)
class HistoricalEpisodeSummary:
    terminal: bool | None
    truncated: bool | None
    winner: str | None
    terminal_reason: str | None
    terminal_time: float | None
    final_survivors: Mapping[str, int]
    cumulative_damage: Mapping[str, float]
    source_raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return json_clone({
            "terminal": self.terminal, "truncated": self.truncated,
            "winner": self.winner, "terminal_reason": self.terminal_reason,
            "terminal_time": self.terminal_time,
            "final_survivors": self.final_survivors,
            "cumulative_damage": self.cumulative_damage,
            "source_raw": self.source_raw,
        })


@dataclass(frozen=True, slots=True)
class HistoricalReplay:
    replay_id: str
    source_format: str
    source_hash: str
    evidence_level: EvidenceLevel
    scenario_id: str
    map_id: str
    time_unit: str
    coordinate_unit: str
    frames: tuple[HistoricalFrame, ...]
    actions: tuple[HistoricalAction, ...]
    events: tuple[HistoricalEvent, ...]
    advantage_curve: tuple[tuple[float, float], ...]
    summary: HistoricalEpisodeSummary
    metadata: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return json_clone({
            "schema_version": "historical-replay/1.0",
            "replay_id": self.replay_id, "source_format": self.source_format,
            "source_hash": self.source_hash,
            "evidence_level": self.evidence_level.value,
            "scenario_id": self.scenario_id, "map_id": self.map_id,
            "time_unit": self.time_unit, "coordinate_unit": self.coordinate_unit,
            "frames": [item.to_dict() for item in self.frames],
            "actions": [item.to_dict() for item in self.actions],
            "events": [item.to_dict() for item in self.events],
            "advantage_curve": [[time, value]
                                for time, value in self.advantage_curve],
            "summary": self.summary.to_dict(), "metadata": self.metadata,
            "warnings": list(self.warnings),
        })

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HistoricalReplay":
        return cls(
            replay_id=str(value["replay_id"]),
            source_format=str(value["source_format"]),
            source_hash=str(value["source_hash"]),
            evidence_level=EvidenceLevel(str(value["evidence_level"])),
            scenario_id=str(value["scenario_id"]), map_id=str(value["map_id"]),
            time_unit=str(value["time_unit"]),
            coordinate_unit=str(value["coordinate_unit"]),
            frames=tuple(_frame_from_normalized(item)
                         for item in value.get("frames", [])),
            actions=tuple(_action_from_normalized(item)
                          for item in value.get("actions", [])),
            events=tuple(_event_from_normalized(item)
                         for item in value.get("events", [])),
            advantage_curve=tuple((float(item[0]), float(item[1]))
                                  for item in value.get("advantage_curve", [])),
            summary=_summary_from_normalized(value.get("summary", {})),
            metadata=json_clone(value.get("metadata", {})),
            warnings=tuple(json_clone(value.get("warnings", []))),
        )


@dataclass(frozen=True, slots=True)
class HistoricalImportResult:
    replay: HistoricalReplay
    import_manifest: Mapping[str, Any]
    unmapped_fields: Mapping[str, Any]

    def write(self, output_dir: str | Path) -> Path:
        root = Path(output_dir)
        if root.exists() and any(root.iterdir()):
            raise FileExistsError(f"refusing to overwrite import: {root}")
        root.mkdir(parents=True, exist_ok=True)
        _write_json(root / "import_manifest.json", self.import_manifest)
        _write_json(root / "historical_replay.json", self.replay.to_dict())
        _write_jsonl(root / "frames.jsonl",
                     (item.to_dict() for item in self.replay.frames))
        _write_jsonl(root / "actions.jsonl",
                     (item.to_dict() for item in self.replay.actions))
        _write_jsonl(root / "events.jsonl",
                     (item.to_dict() for item in self.replay.events))
        _write_json(root / "summary.json", self.replay.summary.to_dict())
        _write_json(root / "unmapped_fields.json", self.unmapped_fields)
        _write_json(root / "warnings.json",
                    {"warnings": list(self.replay.warnings)})
        return root


class HistoricalReplayImporter:
    """Read small parsed replay directories without loading legacy runtime code."""

    def import_path(self, source: str | Path, *,
                    output_dir: str | Path | None = None) -> HistoricalImportResult:
        path = Path(source)
        before = _source_hash(path)
        if path.is_file() and path.suffix.lower() == ".zip":
            result = self._import_source_archive(path)
            if _source_hash(path) != before:
                raise RuntimeError("historical source replay changed during import")
            if output_dir is not None:
                result.write(output_dir)
            return result
        if path.is_dir() and not (path / "manifest.json").exists():
            index_path = path / "index.json"
            if not index_path.exists():
                raise ValueError("historical replay directory has no manifest")
            index = _read_json(index_path)
            if index.get("schema_version") != "historical-replay-fixture-index/1.0":
                raise ValueError("unsupported historical replay fixture index")
            replay_id = str(index["default_replay_id"])
            selected = path / replay_id
            if selected.parent != path or not selected.is_dir():
                raise ValueError("invalid default replay in fixture index")
            result = self.import_path(selected)
            if _source_hash(path) != before:
                raise RuntimeError("historical replay collection changed during import")
            if output_dir is not None:
                result.write(output_dir)
            return result
        if path.is_file():
            value = _read_json(path)
            if value.get("schema_version") == "historical-replay/1.0":
                replay = HistoricalReplay.from_dict(value)
                result = HistoricalImportResult(
                    replay, self._manifest(replay, path),
                    {"fields": [], "note": "normalized historical replay input"})
            else:
                raise ValueError("unsupported historical replay file")
        else:
            manifest = _read_json(path / "manifest.json")
            schema = manifest.get("schema_version")
            if schema == "historical-replay-source/1.0":
                result = self._import_reduced(path, manifest, before)
            elif schema == "game-session-replay/1.0":
                from wargame_native_engine.io.importers.canonical_replay import (
                    CanonicalReplayImporter,
                )
                result = CanonicalReplayImporter().import_directory(path, before)
            else:
                raise ValueError(f"unsupported replay source schema: {schema}")
        if _source_hash(path) != before:
            raise RuntimeError("historical replay source changed during import")
        if output_dir is not None:
            result.write(output_dir)
        return result

    def _import_source_archive(self, source: Path) -> HistoricalImportResult:
        from wargame_native_engine.io.importers.source_replay_archive import (
            SourceReplayArchiveImporter,
        )
        archive = SourceReplayArchiveImporter().import_path(source)
        frames: list[HistoricalFrame] = []
        for frame in archive.frames:
            operators = []
            for unit in sorted(frame.all_units, key=lambda item: item.obj_id):
                row, column = unit.row_col
                side = "red" if unit.color == 0 else "blue" if unit.color == 1 else "unknown"
                health = 100.0 * unit.blood / unit.max_blood
                mapping = {
                    "operator_id": MappingStatus.DIRECT.value,
                    "side": MappingStatus.NORMALIZED.value,
                    "operator_type": MappingStatus.NORMALIZED.value,
                    "position": MappingStatus.NORMALIZED.value,
                    "health": MappingStatus.DERIVED.value,
                    "alive": MappingStatus.DERIVED.value,
                    "state": MappingStatus.NORMALIZED.value,
                }
                normalized = {
                    "position": {"x": float(column), "y": float(row)},
                    "health": health, "alive": unit.alive,
                    "state": "MOVING" if not unit.stopped else "STOPPED",
                }
                operators.append(HistoricalOperatorState(
                    operator_id=unit.obj_id, side=side,
                    operator_type=f"source-type-{unit.type_code}",
                    x=float(column), y=float(row), health=health,
                    max_health=100.0, alive=unit.alive,
                    state=str(normalized["state"]),
                    weapon_ids=unit.carry_weapon_ids,
                    field_mapping=mapping, source_raw=json_clone(unit.raw),
                    normalized_historical=normalized,
                    native_comparable={**normalized,
                                       "mapping_status": mapping},
                ))
            frames.append(HistoricalFrame(
                historical_time=float(frame.time.cur_step),
                operators=tuple(operators), source_raw=frame.to_dict()))
        actions: list[HistoricalAction] = []
        for step in archive.steps:
            for action in step.get("ordered_actions", []):
                if not isinstance(action, Mapping) or action.get("actor_id") is None:
                    continue
                position = action.get("target_position")
                actions.append(HistoricalAction(
                    historical_time=float(step.get("step_index", 0)),
                    side=str(action["side"]), actor_id=int(action["actor_id"]),
                    action_type=str(action["action_type"]),
                    target_id=(None if action.get("target_id") is None
                               else int(action["target_id"])),
                    target_position=(None if position is None else
                                     (float(position[0]), float(position[1]))),
                    parameters=json_clone(action.get("parameters", {})),
                    mapping_status=MappingStatus.DIRECT.value,
                    source_kind="NATIVE_STEP", execution_status="CONFIRMED",
                    executable=True, source_raw=json_clone(action),
                ))
        manifest = archive.manifest
        final = archive.frames[-1]
        survivor_counts: dict[str, int] = {}
        for unit in final.all_units:
            if not unit.alive:
                continue
            side = "red" if unit.color == 0 else "blue" if unit.color == 1 else "unknown"
            survivor_counts[side] = survivor_counts.get(side, 0) + 1
        summary_raw = {
            "terminal": manifest.get("termination"),
            "truncated": manifest.get("truncated"),
            "winner": manifest.get("winner"),
            "terminal_reason": manifest.get("termination_reason"),
            "terminal_time": final.time.cur_step,
            "final_survivors": survivor_counts,
            "cumulative_damage": {},
        }
        summary = self._summary(summary_raw)
        evidence = detect_evidence_level(frames, actions, (), summary_raw)
        replay = HistoricalReplay(
            replay_id=str(manifest.get("episode_id", source.stem)),
            source_format=str(manifest.get(
                "replay_format", "source-numbered-frames/unknown")),
            source_hash=archive.source_hash, evidence_level=evidence,
            scenario_id=archive.frames[0].scenario_id,
            map_id=str(archive.frames[0].terrain_id),
            time_unit="source_cur_step", coordinate_unit="source_hex",
            frames=tuple(frames), actions=tuple(actions), events=(),
            advantage_curve=(), summary=summary,
            metadata={"source_archive_manifest": json_clone(manifest),
                      "frame_names": list(archive.frame_names),
                      "native_sidecars_ignored_as_frames": [
                          "steps.jsonl", "replay_manifest.native.jsonl",
                          "frame_analysis.native.jsonl"]},
            warnings=archive.warnings,
        )
        return HistoricalImportResult(
            replay, self._manifest(replay, source),
            {"fields": [], "note": (
                "numbered Source-compatible frames imported; JSONL sidecars "
                "were not treated as frames")})

    def _import_reduced(self, root: Path, manifest: Mapping[str, Any],
                        source_hash: str) -> HistoricalImportResult:
        time_unit = manifest.get("time_unit")
        coordinate_unit = manifest.get("coordinate_unit")
        if not isinstance(time_unit, str) or not time_unit:
            raise ValueError("historical time_unit must be explicit")
        if not isinstance(coordinate_unit, str) or not coordinate_unit:
            raise ValueError("historical coordinate_unit must be explicit")
        raw_frames = _read_jsonl_optional(root / "frames.jsonl")
        raw_actions = _read_jsonl_optional(root / "actions.jsonl")
        raw_events = _read_jsonl_optional(root / "events.jsonl")
        raw_advantage = _read_jsonl_optional(root / "advantage.jsonl")
        raw_summary = _read_json(root / "summary.json")
        frames = tuple(sorted((self._frame(item) for item in raw_frames),
                              key=lambda item: item.historical_time))
        actions = tuple(sorted((self._action(item) for item in raw_actions),
                               key=lambda item: (item.historical_time, item.side,
                                                 item.actor_id, item.action_type)))
        events = tuple(sorted((self._event(item) for item in raw_events),
                              key=lambda item: (item.historical_time,
                                                item.event_type,
                                                item.side or "",
                                                item.actor_id or -1)))
        advantage = tuple(sorted(
            (float(item["historical_time"]), float(item["advantage"]))
            for item in raw_advantage if item.get("advantage") is not None))
        evidence = detect_evidence_level(frames, actions, events, raw_summary)
        warnings: list[Mapping[str, Any]] = []
        declared = manifest.get("declared_evidence_level")
        if declared is not None and str(declared) != evidence.value:
            warnings.append({"code": "declared_evidence_level_mismatch",
                             "declared": declared, "detected": evidence.value})
        unconfirmed = sum(not item.executable for item in actions)
        if unconfirmed:
            warnings.append({"code": "unconfirmed_actions_not_executable",
                             "count": unconfirmed})
        summary = self._summary(raw_summary)
        replay = HistoricalReplay(
            replay_id=str(manifest["replay_id"]),
            source_format=str(manifest["source_format"]),
            source_hash=source_hash, evidence_level=evidence,
            scenario_id=str(manifest["scenario_id"]),
            map_id=str(manifest["map_id"]), time_unit=time_unit,
            coordinate_unit=coordinate_unit, frames=frames, actions=actions,
            events=events, advantage_curve=advantage, summary=summary,
            metadata={**json_clone(manifest.get("metadata", {})),
                      "source_nature": manifest.get("source_nature"),
                      "map": json_clone(manifest.get("map", {})),
                      "time_alignment": json_clone(
                          manifest.get("time_alignment", {}))},
            warnings=tuple(warnings),
        )
        known_frame = {"historical_time", "operators", "advantage"}
        known_operator = {"operator_id", "side", "operator_type", "x", "y",
                          "health", "max_health", "alive", "state", "weapon_ids",
                          "field_mapping"}
        known_action = {"historical_time", "side", "actor_id", "action_type",
                        "target_id", "target_position", "parameters",
                        "mapping_status", "source_kind", "execution_status"}
        known_event = {"historical_time", "event_type", "side", "actor_id",
                       "target_id", "data", "mapping_status"}
        unmapped = {
            "frame_fields": _unknown_counts(raw_frames, known_frame),
            "operator_fields": _unknown_counts(
                [operator for frame in raw_frames
                 for operator in frame.get("operators", [])], known_operator),
            "action_fields": _unknown_counts(raw_actions, known_action),
            "event_fields": _unknown_counts(raw_events, known_event),
            "missing_fields": _missing_fields(raw_frames, raw_actions),
            "handling": "preserved_in_source_raw_without_native_rule_effect",
        }
        return HistoricalImportResult(
            replay, self._manifest(replay, root), unmapped)

    def _frame(self, value: Mapping[str, Any]) -> HistoricalFrame:
        operators = tuple(sorted((self._operator(item)
                                  for item in value.get("operators", [])),
                                 key=lambda item: item.operator_id))
        return HistoricalFrame(
            historical_time=float(value["historical_time"]),
            operators=operators,
            advantage=(None if value.get("advantage") is None
                       else float(value["advantage"])),
            source_raw=json_clone(value),
        )

    @staticmethod
    def _operator(value: Mapping[str, Any]) -> HistoricalOperatorState:
        mapping = dict(value.get("field_mapping", {}))
        defaults = {
            "operator_id": MappingStatus.DIRECT.value,
            "side": MappingStatus.DIRECT.value,
            "operator_type": MappingStatus.NORMALIZED.value,
            "position": (MappingStatus.DIRECT.value
                         if value.get("x") is not None and value.get("y") is not None
                         else MappingStatus.MISSING.value),
            "health": (MappingStatus.DIRECT.value if value.get("health") is not None
                       else MappingStatus.MISSING.value),
            "alive": (MappingStatus.DIRECT.value if value.get("alive") is not None
                      else MappingStatus.MISSING.value),
            "state": (MappingStatus.DIRECT.value if value.get("state") is not None
                      else MappingStatus.MISSING.value),
        }
        defaults.update({str(key): str(item) for key, item in mapping.items()})
        normalized = {
            "position": (None if value.get("x") is None or value.get("y") is None
                         else {"x": float(value["x"]), "y": float(value["y"])}),
            "health": (None if value.get("health") is None
                       else float(value["health"])),
            "alive": value.get("alive"), "state": value.get("state"),
        }
        comparable = {**normalized,
                      "mapping_status": json_clone(defaults)}
        return HistoricalOperatorState(
            operator_id=int(value["operator_id"]), side=str(value["side"]),
            operator_type=str(value.get("operator_type", "ground_unit")),
            x=None if value.get("x") is None else float(value["x"]),
            y=None if value.get("y") is None else float(value["y"]),
            health=None if value.get("health") is None else float(value["health"]),
            max_health=(None if value.get("max_health") is None
                        else float(value["max_health"])),
            alive=None if value.get("alive") is None else bool(value["alive"]),
            state=None if value.get("state") is None else str(value["state"]),
            weapon_ids=tuple(int(item) for item in value.get("weapon_ids", [])),
            field_mapping=defaults, source_raw=json_clone(value),
            normalized_historical=normalized, native_comparable=comparable,
        )

    @staticmethod
    def _action(value: Mapping[str, Any]) -> HistoricalAction:
        action_type = str(value.get("action_type", "UNSUPPORTED")).upper()
        position = value.get("target_position")
        source_kind = str(value.get("source_kind", "COMMAND")).upper()
        execution = str(value.get("execution_status", "CONFIRMED")).upper()
        complete = (value.get("historical_time") is not None
                    and value.get("side") is not None
                    and value.get("actor_id") is not None
                    and action_type in {"MOVE", "ATTACK", "WAIT", "CHANGE_STATE"}
                    and (action_type != "MOVE" or position is not None)
                    and (action_type != "ATTACK" or value.get("target_id") is not None))
        executable = bool(complete and not (
            source_kind == "ACTION_ECHO" and execution != "CONFIRMED"))
        status = str(value.get(
            "mapping_status", MappingStatus.DIRECT.value if complete
            else MappingStatus.UNMAPPED.value))
        return HistoricalAction(
            historical_time=float(value.get("historical_time", 0.0)),
            side=str(value.get("side", "unknown")),
            actor_id=int(value.get("actor_id", -1)), action_type=action_type,
            target_id=(None if value.get("target_id") is None
                       else int(value["target_id"])),
            target_position=(None if position is None
                             else (float(position[0]), float(position[1]))),
            parameters=json_clone(value.get("parameters", {})),
            mapping_status=status, source_kind=source_kind,
            execution_status=execution, executable=executable,
            source_raw=json_clone(value),
        )

    @staticmethod
    def _event(value: Mapping[str, Any]) -> HistoricalEvent:
        return HistoricalEvent(
            historical_time=float(value["historical_time"]),
            event_type=str(value["event_type"]),
            side=None if value.get("side") is None else str(value["side"]),
            actor_id=(None if value.get("actor_id") is None
                      else int(value["actor_id"])),
            target_id=(None if value.get("target_id") is None
                       else int(value["target_id"])),
            data=json_clone(value.get("data", {})),
            mapping_status=str(value.get("mapping_status",
                                         MappingStatus.DIRECT.value)),
            source_raw=json_clone(value),
        )

    @staticmethod
    def _summary(value: Mapping[str, Any]) -> HistoricalEpisodeSummary:
        return HistoricalEpisodeSummary(
            terminal=None if value.get("terminal") is None else bool(value["terminal"]),
            truncated=(None if value.get("truncated") is None
                       else bool(value["truncated"])),
            winner=None if value.get("winner") is None else str(value["winner"]),
            terminal_reason=(None if value.get("terminal_reason") is None
                             else str(value["terminal_reason"])),
            terminal_time=(None if value.get("terminal_time") is None
                           else float(value["terminal_time"])),
            final_survivors={str(key): int(item) for key, item in
                             dict(value.get("final_survivors", {})).items()},
            cumulative_damage={str(key): float(item) for key, item in
                               dict(value.get("cumulative_damage", {})).items()},
            source_raw=json_clone(value),
        )

    @staticmethod
    def _manifest(replay: HistoricalReplay,
                  source: Path) -> Mapping[str, Any]:
        return {
            "schema_version": "historical-import/1.0",
            "replay_id": replay.replay_id,
            "source_identifier": str(source),
            "source_hash": replay.source_hash,
            "source_format": replay.source_format,
            "importer_version": HISTORICAL_IMPORTER_VERSION,
            "evidence_level": replay.evidence_level.value,
            "scenario_id": replay.scenario_id, "map_id": replay.map_id,
            "time_unit": replay.time_unit,
            "coordinate_unit": replay.coordinate_unit,
            "frame_count": len(replay.frames),
            "action_count": len(replay.actions),
            "executable_action_count": sum(item.executable
                                           for item in replay.actions),
            "event_count": len(replay.events),
            "warning_count": len(replay.warnings),
        }


def load_historical_replay(path: str | Path) -> HistoricalReplay:
    value = _read_json(Path(path))
    if value.get("schema_version") != "historical-replay/1.0":
        raise ValueError("unsupported normalized historical replay schema")
    return HistoricalReplay.from_dict(value)


def detect_evidence_level(frames: Iterable[HistoricalFrame],
                          actions: Iterable[HistoricalAction],
                          events: Iterable[HistoricalEvent],
                          summary: Mapping[str, Any]) -> EvidenceLevel:
    action_values = tuple(actions)
    frame_values = tuple(frames)
    event_values = tuple(events)
    if action_values and all(item.executable for item in action_values):
        return EvidenceLevel.ACTION_LEVEL
    if len(frame_values) >= 2 and all(item.operators for item in frame_values):
        return EvidenceLevel.STATE_TRAJECTORY_LEVEL
    if event_values:
        return EvidenceLevel.EVENT_LEVEL
    if summary:
        return EvidenceLevel.SUMMARY_ONLY
    raise ValueError("historical replay contains no usable evidence")


def _source_hash(path: Path) -> str:
    if path.is_file():
        return file_sha256(path)
    files = sorted(item for item in path.rglob("*") if item.is_file())
    return canonical_hash({str(item.relative_to(path)): file_sha256(item)
                           for item in files})


def _unknown_counts(values: Iterable[Mapping[str, Any]],
                    known: set[str]) -> list[Mapping[str, Any]]:
    counts: dict[str, int] = {}
    for value in values:
        for key in set(value) - known:
            counts[str(key)] = counts.get(str(key), 0) + 1
    return [{"field": key, "occurrences": counts[key],
             "mapping_status": MappingStatus.UNMAPPED.value,
             "handling": "preserved_in_source_raw"}
            for key in sorted(counts)]


def _missing_fields(frames: Iterable[Mapping[str, Any]],
                    actions: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    operators = [operator for frame in frames
                 for operator in frame.get("operators", [])]
    for field_name in ("x", "y", "health", "alive", "state"):
        count = sum(item.get(field_name) is None for item in operators)
        if count:
            result.append({"record": "operator", "field": field_name,
                           "occurrences": count,
                           "mapping_status": MappingStatus.MISSING.value})
    for action in actions:
        action_type = str(action.get("action_type", "")).upper()
        if action_type == "MOVE" and action.get("target_position") is None:
            result.append({"record": "action", "field": "target_position",
                           "historical_time": action.get("historical_time"),
                           "actor_id": action.get("actor_id"),
                           "mapping_status": MappingStatus.MISSING.value})
        if action_type == "ATTACK" and action.get("target_id") is None:
            result.append({"record": "action", "field": "target_id",
                           "historical_time": action.get("historical_time"),
                           "actor_id": action.get("actor_id"),
                           "mapping_status": MappingStatus.MISSING.value})
    return result


def _read_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _read_jsonl_optional(path: Path) -> list[Mapping[str, Any]]:
    if not path.exists():
        return []
    values = [json.loads(line) for line in path.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    if not all(isinstance(item, Mapping) for item in values):
        raise ValueError(f"JSONL records must be objects: {path}")
    return values


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(json_clone(value), ensure_ascii=False,
                               sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    path.write_text("".join(json.dumps(json_clone(value), ensure_ascii=False,
                                       sort_keys=True, separators=(",", ":")) + "\n"
                            for value in values), encoding="utf-8")


def _frame_from_normalized(value: Mapping[str, Any]) -> HistoricalFrame:
    return HistoricalFrame(
        historical_time=float(value["historical_time"]),
        operators=tuple(_operator_from_normalized(item)
                        for item in value.get("operators", [])),
        advantage=(None if value.get("advantage") is None
                   else float(value["advantage"])),
        source_raw=json_clone(value.get("source_raw", {})),
    )


def _operator_from_normalized(
        value: Mapping[str, Any]) -> HistoricalOperatorState:
    return HistoricalOperatorState(
        operator_id=int(value["operator_id"]), side=str(value["side"]),
        operator_type=str(value["operator_type"]),
        x=None if value.get("x") is None else float(value["x"]),
        y=None if value.get("y") is None else float(value["y"]),
        health=None if value.get("health") is None else float(value["health"]),
        max_health=(None if value.get("max_health") is None
                    else float(value["max_health"])),
        alive=None if value.get("alive") is None else bool(value["alive"]),
        state=None if value.get("state") is None else str(value["state"]),
        weapon_ids=tuple(int(item) for item in value.get("weapon_ids", [])),
        field_mapping=json_clone(value.get("field_mapping", {})),
        source_raw=json_clone(value.get("source_raw", {})),
        normalized_historical=json_clone(
            value.get("normalized_historical", {})),
        native_comparable=json_clone(value.get("native_comparable", {})),
    )


def _action_from_normalized(value: Mapping[str, Any]) -> HistoricalAction:
    position = value.get("target_position")
    return HistoricalAction(
        historical_time=float(value["historical_time"]),
        side=str(value["side"]), actor_id=int(value["actor_id"]),
        action_type=str(value["action_type"]),
        target_id=(None if value.get("target_id") is None
                   else int(value["target_id"])),
        target_position=(None if position is None
                         else (float(position[0]), float(position[1]))),
        parameters=json_clone(value.get("parameters", {})),
        mapping_status=str(value["mapping_status"]),
        source_kind=str(value["source_kind"]),
        execution_status=str(value["execution_status"]),
        executable=bool(value["executable"]),
        source_raw=json_clone(value.get("source_raw", {})),
    )


def _event_from_normalized(value: Mapping[str, Any]) -> HistoricalEvent:
    return HistoricalEvent(
        historical_time=float(value["historical_time"]),
        event_type=str(value["event_type"]),
        side=None if value.get("side") is None else str(value["side"]),
        actor_id=(None if value.get("actor_id") is None
                  else int(value["actor_id"])),
        target_id=(None if value.get("target_id") is None
                   else int(value["target_id"])),
        data=json_clone(value.get("data", {})),
        mapping_status=str(value["mapping_status"]),
        source_raw=json_clone(value.get("source_raw", {})),
    )


def _summary_from_normalized(value: Mapping[str, Any]) -> HistoricalEpisodeSummary:
    return HistoricalEpisodeSummary(
        terminal=None if value.get("terminal") is None else bool(value["terminal"]),
        truncated=(None if value.get("truncated") is None
                   else bool(value["truncated"])),
        winner=None if value.get("winner") is None else str(value["winner"]),
        terminal_reason=(None if value.get("terminal_reason") is None
                         else str(value["terminal_reason"])),
        terminal_time=(None if value.get("terminal_time") is None
                       else float(value["terminal_time"])),
        final_survivors={str(key): int(item) for key, item in
                         dict(value.get("final_survivors", {})).items()},
        cumulative_damage={str(key): float(item) for key, item in
                           dict(value.get("cumulative_damage", {})).items()},
        source_raw=json_clone(value.get("source_raw", {})),
    )
