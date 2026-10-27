"""Validated, immutable data projection for the local replay viewer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from wargame_native_engine.analysis.jsonl import analysis_sidecar_from_jsonl
from wargame_native_engine.analysis.models import (
    AnalysisEvent,
    AnalysisSidecarLoadResult,
    FrameAnalysis,
)
from wargame_native_engine.analysis.serialization import frame_analysis_to_record
from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
from wargame_native_engine.compat.source_frame.mapping import SourceMappings
from wargame_native_engine.compat.source_frame.models import (
    SourceFrame,
    SourceUnitState,
)
from wargame_native_engine.core.action import EngineAction
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchive,
    SourceReplayArchiveReader,
)
from wargame_native_engine.io.serialization import json_clone
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.viewer.map_config import (
    ViewerMapConfig,
    coordinate_fallback_record,
    load_native_scenario_map_record,
    load_viewer_map_config,
)
from wargame_native_engine.viewer.scenario_config import (
    ViewerScenarioConfig,
    load_viewer_scenario_config,
)
from wargame_native_engine.viewer.recorded_visibility import (
    ReplayPerspectiveUnavailable,
    recorded_side_frame,
    validate_recorded_visibility,
    published_contact_identities,
)


REPLAY_ARCHIVE_NAME = "replay.zip"
ANALYSIS_SIDECAR_NAME = "frame_analysis.native.jsonl"
PLAYBACK_COMPATIBILITY = "source-numbered-frame-playback/1.0"
NATIVE_REPLAY_FORMAT = "native-source-frame-replay/1.0"
_SOURCE_MAPPINGS = SourceMappings()
_STRICT_REPLAY_MANIFEST_STRING_FIELDS = frozenset({
    "scenario_id",
    "scenario_hash",
    "ruleset_id",
    "ruleset_version",
    "ruleset_hash",
    "initial_state_hash",
    "final_state_hash",
})


class ReplayViewerLoadError(ValueError):
    """Raised when replay facts and analysis cannot be aligned safely."""


@dataclass(frozen=True, slots=True)
class ReplayView:
    """One fully validated replay and its reconstructed analysis timeline."""

    replay_dir: Path
    archive: SourceReplayArchive
    analysis: AnalysisSidecarLoadResult
    playback_compatibility: str
    strict_replay_evidence_status: str
    map_data: Mapping[str, object]
    model_id: str
    model_version: str
    model_hash: str | None
    perspective_errors: Mapping[str, str] = field(default_factory=dict)
    contact_identities: Mapping[int, Mapping[str, Any]] = field(default_factory=dict, repr=False)
    _events_by_frame: Mapping[int, tuple[AnalysisEvent, ...]] = field(
        init=False, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        grouped: dict[int, list[AnalysisEvent]] = {}
        for event in self.analysis.events:
            grouped.setdefault(event.frame_index, []).append(event)
        object.__setattr__(self, "_events_by_frame", MappingProxyType({
            index: tuple(events) for index, events in grouped.items()
        }))

    @property
    def frame_count(self) -> int:
        return len(self.archive.frames)

    def replay_record(self) -> dict[str, object]:
        manifest = self.archive.manifest
        return {
            "frame_count": self.frame_count,
            "first_frame_index": 0,
            "last_frame_index": self.frame_count - 1,
            "perspective": "GREEN",
            "playback_compatibility": self.playback_compatibility,
            "strict_replay_evidence_status": self.strict_replay_evidence_status,
            "strict_replay_evidence_note": "",
            "model_provenance": {
                "model_id": self.model_id,
                "model_version": self.model_version,
                "model_hash": self.model_hash,
            },
            "scenario_hash": (
                self.analysis.timeline.frame_analyses[0].provenance.scenario_hash
            ),
            "termination": manifest.get("termination"),
            "truncated": manifest.get("truncated"),
            "winner": manifest.get("winner"),
            "outcome": manifest.get("outcome"),
        }

    def map_record(self) -> dict[str, object]:
        return json_clone(self.map_data)

    def perspectives_record(self) -> dict[str, object]:
        return {
            "GREEN": {"status": "available", "reason": None},
            **{
                side: {
                    "status": "unavailable" if side in self.perspective_errors else "available",
                    "reason": self.perspective_errors.get(side),
                    "source": "derived_from_recorded_visibility",
                }
                for side in ("RED", "BLUE")
            },
        }

    def frame_record(self, frame_index: int, perspective: str = "GREEN") -> dict[str, object]:
        index = self._checked_index(frame_index)
        if perspective not in {"GREEN", "RED", "BLUE"}:
            raise ValueError("perspective must be GREEN, RED or BLUE")
        if perspective in self.perspective_errors:
            raise ReplayPerspectiveUnavailable(self.perspective_errors[perspective])
        frame = self.archive.frames[index]
        if perspective != "GREEN":
            return recorded_side_frame(index, self.archive.frame_names[index], frame,
                                       self.archive.manifest, perspective, self.contact_identities)
        return _frame_record(index, self.archive.frame_names[index], frame)

    def analysis_record(self, frame_index: int) -> dict[str, object]:
        index = self._checked_index(frame_index)
        frame = self.analysis.timeline.frame_analyses[index]
        events = self._events_by_frame.get(index, ())
        return frame_analysis_to_record(frame, events=events)

    def frame_bundle_record(self, frame_index: int, perspective: str = "GREEN") -> dict[str, object]:
        frame = self.frame_record(frame_index, perspective)
        analysis = self.analysis_record(frame_index)
        if perspective == "GREEN":
            return {"frame": frame, "analysis": analysis}
        side = perspective.lower()
        own_ids = {unit["operator_id"] for unit in frame["viewer_units"] if unit["side"] == side}
        return {
            "frame": frame, "analysis": None,
            "own_unit_analyses": [unit for unit in analysis["unit_analyses"]
                                  if unit["operator_id"] in own_ids and unit["side"] == side],
            "own_analysis_source": "GLOBAL_MODEL_DERIVED_OWN_FACTORS",
        }

    def timeline_record(self) -> dict[str, object]:
        timeline = self.analysis.timeline
        red_power: list[float | None] = []
        blue_power: list[float | None] = []
        labels: list[str] = []
        for frame in timeline.frame_analyses:
            sides = {side.side: side for side in frame.side_analyses}
            red_power.append(
                sides["red"].effective_power if "red" in sides else None
            )
            blue_power.append(
                sides["blue"].effective_power if "blue" in sides else None
            )
            labels.append(frame.advantage.label)
        return {
            "frame_indices": list(timeline.frame_indices),
            "red_effective_power": red_power,
            "blue_effective_power": blue_power,
            "raw_advantages": list(timeline.raw_advantages),
            "normalized_advantages": list(timeline.normalized_advantages),
            "delta_advantages": list(timeline.delta_advantages),
            "advantage_labels": labels,
        }

    def _checked_index(self, frame_index: int) -> int:
        if type(frame_index) is not int:
            raise TypeError("frame index must be an int")
        if not 0 <= frame_index < self.frame_count:
            raise IndexError(
                f"frame index {frame_index} is outside 0..{self.frame_count - 1}"
            )
        return frame_index


def load_replay_view(
    replay_dir: str | Path,
    *,
    map_config: str | Path | None = None,
    native_scenario_map: str | Path | None = None,
    map_coordinate_system: str | None = None,
    scenario_config: str | Path | None = None,
) -> ReplayView:
    """Load and strictly align a replay ZIP with its external analysis JSONL."""

    if native_scenario_map is not None and any(
        value is not None
        for value in (map_config, map_coordinate_system, scenario_config)
    ):
        raise ReplayViewerLoadError(
            "native_scenario_map cannot be combined with map_config, "
            "map_coordinate_system, or scenario_config"
        )
    root = Path(replay_dir)
    replay_path = root / REPLAY_ARCHIVE_NAME
    analysis_path = root / ANALYSIS_SIDECAR_NAME
    if not replay_path.is_file():
        raise ReplayViewerLoadError(f"missing replay archive: {replay_path}")
    if not analysis_path.is_file():
        raise ReplayViewerLoadError(f"missing analysis sidecar: {analysis_path}")

    try:
        reader = SourceReplayArchiveReader(
            replay_path,
            frame_importer=_ViewerSourceFrameImporter()
        )
        archive = SourceReplayArchive(
            source_hash=reader.source_hash,
            frames=reader.lazy_frames(),
            frame_names=reader.frame_names,
            steps=reader.steps,
            manifest=reader.manifest,
            warnings=(),
        )
    except Exception as exc:
        raise ReplayViewerLoadError(f"failed to load replay archive: {exc}") from exc
    try:
        analysis = analysis_sidecar_from_jsonl(analysis_path.read_bytes())
    except Exception as exc:
        raise ReplayViewerLoadError(f"failed to load analysis sidecar: {exc}") from exc

    frame_count = len(archive.frames)
    if frame_count == 0:
        raise ReplayViewerLoadError("replay must contain at least one frame")
    expected_names = tuple(f"{index}.json" for index in range(frame_count))
    if archive.frame_names != expected_names:
        raise ReplayViewerLoadError(
            "replay frame names must be continuous from 0.json; "
            f"got {archive.frame_names!r}"
        )

    analysis_frames = analysis.timeline.frame_analyses
    if len(analysis_frames) != frame_count:
        raise ReplayViewerLoadError(
            "replay/analysis frame count mismatch: "
            f"{frame_count} replay frames, {len(analysis_frames)} analysis frames"
        )

    model_identity: tuple[str, str, str | None] | None = None
    replay_scenario_ids: set[str] = set()
    replay_map_ids: set[str] = set()
    # Four extrema retain enough evidence for bounds validation without keeping
    # every unit/frame resident or decompressing the archive a second time.
    extrema: dict[str, tuple[int, int, object, int, int]] = {}
    perspective_errors: dict[str, str] = {}
    for index, (frame, frame_analysis) in enumerate(
        zip(archive.frames, analysis_frames)
    ):
        if frame.scenario_id is not None:
            replay_scenario_ids.add(str(frame.scenario_id))
        if frame.terrain_id is not None:
            replay_map_ids.add(str(frame.terrain_id))
        for unit in (*frame.operators, *frame.passengers):
            row, column = unit.row_col
            for name, value, minimize in (
                ("min_row", row, True), ("max_row", row, False),
                ("min_column", column, True), ("max_column", column, False),
            ):
                current = extrema.get(name)
                if current is None or (value < current[0] if minimize else value > current[0]):
                    extrema[name] = (value, index, unit.obj_id, row, column)
        provenance = frame_analysis.provenance
        if provenance.frame_index != index:
            raise ReplayViewerLoadError(
                f"analysis frame index mismatch at frame {index}: "
                f"got {provenance.frame_index}"
            )
        if provenance.frame_hash != frame.source_hash:
            raise ReplayViewerLoadError(
                f"analysis frame hash mismatch at frame {index}: "
                f"expected {frame.source_hash}, got {provenance.frame_hash}"
            )
        if provenance.perspective != "GREEN":
            raise ReplayViewerLoadError(
                f"analysis perspective at frame {index} must be GREEN"
            )
        _validate_explicit_frame_perspective(frame, index)
        for perspective in ("RED", "BLUE"):
            if perspective not in perspective_errors:
                try:
                    validate_recorded_visibility(frame, archive.manifest, perspective)
                except ReplayPerspectiveUnavailable as exc:
                    perspective_errors[perspective] = f"Frame {index}：{exc}"
        identity = (
            provenance.model_id,
            provenance.model_version,
            provenance.model_hash,
        )
        if model_identity is None:
            model_identity = identity
        elif identity != model_identity:
            raise ReplayViewerLoadError(
                f"analysis model provenance mismatch at frame {index}"
            )

    assert model_identity is not None
    manifest = archive.manifest
    manifest_perspective = manifest.get("perspective")
    if manifest_perspective is not None and manifest_perspective != "GREEN":
        raise ReplayViewerLoadError("replay manifest perspective must be GREEN")
    strict_replay_evidence_status = _strict_replay_evidence_status(archive)
    try:
        loaded_map: ViewerMapConfig | None = (
            None
            if map_config is None
            else load_viewer_map_config(
                map_config,
                coordinate_system_override=map_coordinate_system,
            )
        )
    except (OSError, ValueError) as exc:
        raise ReplayViewerLoadError(f"failed to load map config: {exc}") from exc
    map_data: dict[str, object] = (
        coordinate_fallback_record(())
        if loaded_map is None
        else loaded_map.to_record()
    )
    if loaded_map is None and extrema:
        bounds = {name: item[0] for name, item in extrema.items()}
        map_data["bounds"] = bounds
        map_data["rows"] = bounds["max_row"] - bounds["min_row"] + 1
        map_data["columns"] = bounds["max_column"] - bounds["min_column"] + 1
    if len(replay_scenario_ids) > 1:
        raise ReplayViewerLoadError(
            "replay frames contain multiple scenario identities: "
            f"{sorted(replay_scenario_ids)!r}"
        )
    replay_scenario_id = next(iter(replay_scenario_ids), None)
    replay_map_id = (
        next(iter(replay_map_ids)) if len(replay_map_ids) == 1 else None
    )
    contact_identities: Mapping[int, Mapping[str, Any]] = {}
    if native_scenario_map is not None:
        if replay_scenario_id is None or replay_map_id is None:
            raise ReplayViewerLoadError(
                "native scenario map requires unique replay scenario_id "
                "and terrain_id evidence"
            )
        try:
            native_scenario = load_scenario(native_scenario_map)
            map_data = load_native_scenario_map_record(
                native_scenario_map,
                expected_scenario_id=replay_scenario_id,
                expected_map_id=replay_map_id,
                expected_scenario_hash=manifest.get("scenario_hash"),
                expected_map_hash=manifest.get("map_hash"),
                loaded_scenario=native_scenario,
            )
            contact_identities = published_contact_identities(root, archive, native_scenario)
        except (OSError, ValueError) as exc:
            raise ReplayViewerLoadError(
                f"failed to load native scenario map: {exc}"
            ) from exc
    if scenario_config is not None:
        if replay_scenario_id is None:
            raise ReplayViewerLoadError(
                "scenario config requires replay scenario_id evidence"
            )
        try:
            loaded_scenario: ViewerScenarioConfig = load_viewer_scenario_config(
                scenario_config,
                expected_scenario_id=replay_scenario_id,
                expected_map_id=replay_map_id,
            )
        except (OSError, ValueError) as exc:
            raise ReplayViewerLoadError(
                f"failed to load scenario config: {exc}"
            ) from exc
        map_data["scenario"] = loaded_scenario.to_record()
    elif native_scenario_map is None:
        map_data["scenario"] = None
    if loaded_map is not None and loaded_map.map_id is not None:
        if replay_map_ids and replay_map_ids != {loaded_map.map_id}:
            raise ReplayViewerLoadError(
                "map config map_id does not match replay terrain_id: "
                f"config={loaded_map.map_id!r}, replay={sorted(replay_map_ids)!r}"
            )
    if loaded_map is not None or native_scenario_map is not None:
        bounds = map_data["bounds"]
        scenario_record = map_data.get("scenario")
        if isinstance(scenario_record, Mapping):
            for feature in scenario_record.get("features", []):
                if not isinstance(feature, Mapping):
                    continue
                row = feature.get("row")
                column = feature.get("column")
                if not (
                    type(row) is int
                    and type(column) is int
                    and bounds["min_row"] <= row <= bounds["max_row"]
                    and bounds["min_column"] <= column <= bounds["max_column"]
                ):
                    raise ReplayViewerLoadError(
                        "scenario feature is outside map config bounds: "
                        f"feature={feature.get('feature_id')!r}, "
                        f"row={row}, column={column}"
                    )
        for _, frame_index, unit_id, row, column in extrema.values():
            if not (
                bounds["min_row"] <= row <= bounds["max_row"]
                and bounds["min_column"] <= column <= bounds["max_column"]
            ):
                raise ReplayViewerLoadError(
                    "replay unit is outside map config bounds at frame "
                    f"{frame_index}: unit={unit_id}, row={row}, column={column}"
                )
    playback_compatibility = str(
        manifest.get("source_frame_compatibility_version")
        or PLAYBACK_COMPATIBILITY
    )
    return ReplayView(
        replay_dir=root,
        archive=archive,
        analysis=analysis,
        playback_compatibility=playback_compatibility,
        strict_replay_evidence_status=strict_replay_evidence_status,
        map_data=map_data,
        model_id=model_identity[0],
        model_version=model_identity[1],
        model_hash=model_identity[2],
        perspective_errors=MappingProxyType(perspective_errors),
        contact_identities=MappingProxyType(dict(contact_identities)),
    )


def _strict_replay_evidence_status(archive: SourceReplayArchive) -> str:
    """Classify evidence presence without claiming strict verification."""

    manifest = archive.manifest
    has_native_manifest = manifest.get("replay_format") == NATIVE_REPLAY_FORMAT
    has_any_evidence = bool(manifest) or bool(archive.steps)
    if not has_native_manifest:
        return "Incomplete" if has_any_evidence else "Unavailable"
    manifest_complete = all(
        _is_nonempty_string(manifest.get(field_name))
        for field_name in _STRICT_REPLAY_MANIFEST_STRING_FIELDS
    ) and _is_nonnegative_exact_int(manifest.get("episode_seed"))
    manifest_counts_complete = (
        _is_positive_exact_int(manifest.get("frame_count"))
        and _is_positive_exact_int(manifest.get("step_count"))
        and manifest["frame_count"] == len(archive.frames)
        and manifest["step_count"] == len(archive.steps)
        and manifest["frame_count"] == manifest["step_count"] + 1
    )
    steps_complete = bool(archive.steps) and all(
        _strict_replay_step_is_usable(step, expected_index=index)
        for index, step in enumerate(archive.steps, start=1)
    )
    return (
        "Available"
        if manifest_complete and manifest_counts_complete and steps_complete
        else "Incomplete"
    )


def _strict_replay_step_is_usable(
    step: Mapping[str, Any],
    *,
    expected_index: int,
) -> bool:
    if step.get("step_index") != expected_index or type(
        step.get("step_index")
    ) is not int:
        return False
    if not _is_positive_exact_int(step.get("delta_time_ms")):
        return False
    if not _is_nonempty_string(step.get("expected_state_hash")):
        return False
    ordered_actions = step.get("ordered_actions")
    if not isinstance(ordered_actions, list):
        return False
    try:
        for action in ordered_actions:
            if not isinstance(action, Mapping):
                return False
            EngineAction.from_dict(action)
    except (KeyError, TypeError, ValueError):
        return False
    action_sequences = step.get("action_sequences")
    if action_sequences is not None and (
        not isinstance(action_sequences, list)
        or any(not _is_nonnegative_exact_int(value) for value in action_sequences)
    ):
        return False
    return True


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_nonnegative_exact_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _is_positive_exact_int(value: object) -> bool:
    return type(value) is int and value > 0


def _validate_explicit_frame_perspective(frame: SourceFrame, index: int) -> None:
    extra = frame.raw.get("extra")
    if not isinstance(extra, Mapping):
        return
    native = extra.get("native")
    if not isinstance(native, Mapping) or "perspective" not in native:
        return
    if native["perspective"] != "GREEN":
        raise ReplayViewerLoadError(
            f"replay frame {index} perspective must be GREEN"
        )


class _ViewerSourceFrameImporter(SourceFrameImporter):
    """Adapt optional display evidence without changing the fact importer."""

    def from_dict(
        self,
        value: Mapping[str, Any],
        *,
        source_hash: str,
    ) -> SourceFrame:
        if "actions" in value and "judge_info" in value:
            return super().from_dict(value, source_hash=source_hash)
        display_value = dict(value)
        display_value.setdefault("actions", [])
        display_value.setdefault("judge_info", [])
        return super().from_dict(display_value, source_hash=source_hash)


def _frame_record(
    frame_index: int,
    frame_name: str,
    frame: SourceFrame,
) -> dict[str, object]:
    units = [
        _viewer_unit(unit, "operator", _SOURCE_MAPPINGS)
        for unit in frame.operators
    ] + [
        _viewer_unit(unit, "passenger", _SOURCE_MAPPINGS)
        for unit in frame.passengers
    ]
    return {
        "frame_index": frame_index,
        "frame_name": frame_name,
        "source_hash": frame.source_hash,
        "time": json_clone(frame.time.raw),
        "operators": [json_clone(unit.raw) for unit in frame.operators],
        "passengers": [json_clone(unit.raw) for unit in frame.passengers],
        "actions": json_clone(frame.actions),
        "judge_info": json_clone(frame.judge_info),
        "done": json_clone(frame.raw.get("done")),
        "winner": json_clone(frame.raw.get("winner")),
        "outcome": json_clone(frame.raw.get("outcome")),
        "scenario_id": frame.scenario_id,
        "terrain_id": json_clone(frame.terrain_id),
        "cities": [json_clone(city.raw) for city in frame.cities],
        "viewer_units": units,
    }


def _viewer_unit(
    unit: SourceUnitState,
    category: str,
    mappings: SourceMappings,
) -> dict[str, object]:
    row, column = unit.row_col
    state = mappings.unit_state(unit).value
    health_ratio = unit.blood / unit.max_blood
    extra = unit.raw.get("extra")
    native = extra.get("native", {}) if isinstance(extra, Mapping) else {}
    native = native if isinstance(native, Mapping) else {}
    move_path = unit.raw.get("move_path")
    movement_target_hex = (
        move_path[0]
        if isinstance(move_path, list)
        and move_path
        and type(move_path[0]) is int
        and move_path[0] >= 0
        else None
    )
    return {
        "operator_id": unit.obj_id,
        "category": category,
        "side": str(mappings.side(unit.color).native_effective_value),
        "name": unit.name,
        "type_code": unit.type_code,
        "sub_type_code": unit.sub_type_code,
        "icon_key": _unit_icon_key(unit.type_code, unit.sub_type_code),
        "current_hex": unit.current_hex,
        "row": row,
        "column": column,
        "movement_target_hex": movement_target_hex,
        "movement_progress": max(0.0, min(1.0, unit.current_progress)),
        "blood": unit.blood,
        "max_blood": unit.max_blood,
        "health_ratio": health_ratio,
        "alive": unit.alive,
        "destroyed": not unit.alive,
        "state": state,
        "weapon_locked": native.get("weapon_locked") if type(native.get("weapon_locked")) is bool else None,
        "locked_weapon_id": native.get("locked_weapon_id") if type(native.get("locked_weapon_id")) is int else None,
        "stopped": unit.stopped,
        "marching": unit.marching,
        "suppressed": unit.suppressed,
        "move_state": json_clone(unit.raw.get("move_state")),
        "direction": json_clone(unit.raw.get("direction")),
        "lose_control": (native["command_lost"]
                         if type(native.get("command_lost")) is bool
                         else bool(unit.raw.get("lose_control", False))),
        "can_to_move": json_clone(unit.raw.get("can_to_move")),
        "on_board": unit.on_board,
        "carrier_id": unit.car_id,
        "launcher_id": unit.launcher_id,
    }


def _unit_icon_key(type_code: int, sub_type_code: int) -> str:
    subtype = {
        0: "tank",
        1: "armored_vehicle",
        2: "infantry",
        3: "artillery",
        4: "unmanned_ground_vehicle",
        5: "drone",
        6: "helicopter",
        7: "loitering_munition",
        8: "helicopter",
        9: "armored_vehicle",
        10: "armored_vehicle",
    }.get(sub_type_code)
    if subtype is not None:
        return subtype
    return {
        1: "infantry",
        2: "armored_vehicle",
        3: "drone",
        4: "facility",
    }.get(type_code, "unit_fallback")
