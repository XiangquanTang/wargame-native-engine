"""Lazy, read-only rendering adapter for lossless compact Source recordings.

The codec remains the authority for raw JSON.  SourceFrameImporter is used only
at the rendering boundary: it never writes decoded frames back to the archive.
Filename indices, playback ordinals, and Source time/action steps are distinct.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any
from zipfile import BadZipFile

from wargame_native_engine.io.serialization import json_clone
from wargame_native_engine.replay.compact import CompactReplayReader
from wargame_native_engine.viewer.compact_analysis import CompactReplayAnalysis
from wargame_native_engine.viewer.map_config import (
    coordinate_fallback_record, load_native_scenario_map_record, load_viewer_map_config,
)
from wargame_native_engine.viewer.recorded_visibility import (
    ReplayPerspectiveUnavailable, recorded_side_frame, validate_recorded_visibility,
    scenario_contact_identities,
)
from wargame_native_engine.viewer.replay_view import (
    ReplayViewerLoadError, _ViewerSourceFrameImporter, _frame_record,
    _validate_explicit_frame_perspective,
)
from wargame_native_engine.viewer.scenario_config import load_viewer_scenario_config




@dataclass(slots=True)
class CompactReplayView:
    """One indexed compact recording; only an accessed frame is projected."""

    replay_dir: Path
    reader: CompactReplayReader
    frame_indices: tuple[int, ...]
    map_data: Mapping[str, Any]
    perspective_errors: Mapping[str, str]
    scenario_id: str | None = None
    terrain_id: str | None = None
    load_validation: str = "streamed-coordinate-bounds"
    contact_identities: Mapping[int, Mapping[str, Any]] = field(default_factory=dict, repr=False)
    _analysis: CompactReplayAnalysis = field(init=False, repr=False)
    supports_analysis_models = True
    _ordinals: dict[int, int] = field(init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def __post_init__(self) -> None:
        self._ordinals = {index: ordinal for ordinal, index in enumerate(self.frame_indices)}
        self._analysis = CompactReplayAnalysis(self.reader, self.frame_indices, self.reader.raw_frame(0))

    @property
    def frame_count(self) -> int:
        return len(self.frame_indices)

    def _ordinal(self, frame_index: int) -> int:
        if type(frame_index) is not int:
            raise TypeError("frame index must be an int")
        try:
            return self._ordinals[frame_index]
        except KeyError:
            raise IndexError(f"frame index {frame_index} is not recorded; gaps are not filled") from None

    def replay_record(self) -> dict[str, object]:
        manifest = self.reader.manifest
        sparse = any(right != left + 1 for left, right in zip(self.frame_indices, self.frame_indices[1:]))
        return {
            "frame_count": self.frame_count,
            "frame_indices": list(self.frame_indices),
            "first_frame_index": self.frame_indices[0],
            "last_frame_index": self.frame_indices[-1],
            "sparse_frame_indices": sparse,
            "frame_index_note": "文件编号与 time.cur_step、actions.cur_step 独立；仅播放已记录帧。",
            "perspective": "GREEN",
            "playback_compatibility": manifest.get("source_frame_compatibility_version", manifest["replay_format"]),
            "replay_format": manifest["replay_format"],
            "strict_replay_evidence_status": "Unavailable",
            "strict_replay_evidence_note": "紧凑录像直接播放记录事实；未执行规则重演校验。",
            **self._analysis.metadata(),
            "archive_hash_verified": self.reader.hash_verified,
            "frame_validation": self.load_validation,
            "scenario_hash": manifest.get("scenario_hash"),
            **{key: json_clone(manifest.get(key)) for key in ("termination", "truncated", "winner", "outcome")},
        }

    def map_record(self) -> dict[str, object]:
        return json_clone(self.map_data)

    def perspectives_record(self) -> dict[str, object]:
        return {
            "GREEN": {"status": "available", "reason": None},
            **{side: {
                "status": "unavailable" if side in self.perspective_errors else "available",
                "reason": self.perspective_errors.get(side),
                "source": "derived_from_recorded_visibility",
            } for side in ("RED", "BLUE")},
        }

    def raw_frame_record(self, frame_index: int) -> dict[str, Any]:
        """Return the codec's exact decoded JSON tree, not a normalized export."""
        ordinal = self._ordinal(frame_index)
        with self._lock:
            try:
                return self.reader.raw_frame(ordinal)
            except (OSError, ValueError, KeyError, BadZipFile) as exc:
                raise ReplayViewerLoadError(f"failed to decode frame {frame_index}: {exc}") from exc

    def frame_record(self, frame_index: int, perspective: str = "GREEN") -> dict[str, object]:
        ordinal = self._ordinal(frame_index)
        if perspective not in {"GREEN", "RED", "BLUE"}:
            raise ValueError("perspective must be GREEN, RED or BLUE")
        if perspective in self.perspective_errors:
            raise ReplayPerspectiveUnavailable(self.perspective_errors[perspective])
        raw = self.raw_frame_record(frame_index)
        return self._render_frame(frame_index, ordinal, perspective, raw)

    def _render_frame(self, frame_index, ordinal, perspective, raw):
        frame = _ViewerSourceFrameImporter().from_dict(raw, source_hash="")
        _validate_explicit_frame_perspective(frame, frame_index)
        if self.scenario_id is not None and frame.scenario_id != self.scenario_id:
            raise ReplayViewerLoadError("frame scenario_id differs from the recording")
        if self.terrain_id is not None and str(frame.terrain_id) != self.terrain_id:
            raise ReplayViewerLoadError("frame terrain_id differs from the recording")
        bounds = self.map_data["bounds"]
        for unit in frame.all_units:
            row, column = unit.row_col
            if not (bounds["min_row"] <= row <= bounds["max_row"]
                    and bounds["min_column"] <= column <= bounds["max_column"]):
                raise ReplayViewerLoadError(f"frame {frame_index} unit {unit.obj_id} is outside map bounds")
        name = self.reader.frame_names[ordinal]
        record = (_frame_record(frame_index, name, frame) if perspective == "GREEN"
                  else recorded_side_frame(frame_index, name, frame, self.reader.manifest, perspective,
                                           self.contact_identities))
        # No per-frame digest is computed, required, or represented as verified.
        record["source_hash"] = None
        record["frame_ordinal"] = ordinal
        return record

    def analysis_record(self, frame_index: int, analysis_model: str | None = None) -> dict[str, object]:
        ordinal = self._ordinal(frame_index)
        return self._analysis.record(frame_index, ordinal, analysis_model)

    def frame_bundle_record(self, frame_index: int, perspective: str = "GREEN",
                            analysis_model: str | None = None) -> dict[str, object]:
        ordinal = self._ordinal(frame_index)
        if perspective not in {"GREEN", "RED", "BLUE"}:
            raise ValueError("perspective must be GREEN, RED or BLUE")
        if perspective in self.perspective_errors:
            raise ReplayPerspectiveUnavailable(self.perspective_errors[perspective])
        raw = self.raw_frame_record(frame_index)
        frame = self._render_frame(frame_index, ordinal, perspective, raw)
        # Invalid requested models are API errors; an invalid optional analysis
        # input must not prevent the already validated factual frame playing.
        if not self._analysis._selection_error:
            self._analysis.model(analysis_model)
        try:
            analysis = self._analysis.record(frame_index, ordinal, analysis_model, raw)
        except (TypeError, ValueError) as exc:
            analysis = {"status": "unavailable", "reason": f"Frame {frame_index}: {exc}"}
        if analysis.get("status") == "unavailable":
            return {"frame": frame, "analysis": None, "analysis_status": "unavailable",
                    "analysis_note": analysis["reason"], "own_unit_analyses": [],
                    "own_analysis_source": "UNAVAILABLE"}
        if perspective == "GREEN":
            return {"frame": frame, "analysis": analysis, "analysis_status": "available"}
        side = perspective.lower()
        own_ids = {unit["operator_id"] for unit in frame["viewer_units"] if unit["side"] == side}
        return {"frame": frame, "analysis": None,
                "own_unit_analyses": [unit for unit in analysis["unit_analyses"]
                                      if unit["side"] == side and unit["operator_id"] in own_ids],
                "own_analysis_source": "GLOBAL_MODEL_DERIVED_OWN_FACTORS"}

    def timeline_record(self, analysis_model: str | None = None) -> dict[str, object]:
        return self._analysis.timeline(analysis_model)


def load_compact_replay_view(
    replay_dir: str | Path, *, map_config: str | Path | None = None,
    native_scenario_map: str | Path | None = None,
    map_coordinate_system: str | None = None, scenario_config: str | Path | None = None,
) -> CompactReplayView:
    """Fast-start embedded maps; stream only when deriving coordinate bounds.

    No full frame collection is retained. Subsequent seeks use bounded
    checkpoint/delta reconstruction and validate projection/visibility on demand.
    Analysis is derived lazily in bounded memory; playback creates no sidecar or frame hashes.
    """
    root = Path(replay_dir)
    try:
        reader = CompactReplayReader(root / "replay.zip")
        if not reader.frame_count:
            raise ValueError("replay must contain at least one frame")
        indices = tuple(int(Path(name).stem) for name in reader.frame_names)
        if any(Path(name).name != name or not Path(name).stem.isascii() or not Path(name).stem.isdigit() or index < 0
               for name, index in zip(reader.frame_names, indices)):
            raise ValueError("compact viewer requires non-negative numbered Source filenames")
        if tuple(sorted(set(indices))) != indices:
            raise ValueError("compact Source filename indices must be unique and increasing")
        if reader.manifest.get("perspective", "GREEN") != "GREEN":
            raise ValueError("replay manifest perspective must be GREEN")
        extrema: dict[str, int] = {}
        scenario_ids: set[str] = set()
        map_ids: set[str] = set()
        perspective_errors: dict[str, str] = {}
        importer = _ViewerSourceFrameImporter()
        has_static_map = isinstance(reader.assets.get("map"), Mapping) and bool(reader.assets["map"])
        load_validation = "on-demand-frame-validation" if has_static_map else "streamed-coordinate-bounds"
        frames = iter((reader.raw_frame(0),)) if has_static_map else reader.iter_raw_frames()
        for ordinal, raw in enumerate(frames):
            index = indices[ordinal]
            frame = importer.from_dict(raw, source_hash="")
            _validate_explicit_frame_perspective(frame, index)
            if frame.scenario_id is not None:
                scenario_ids.add(str(frame.scenario_id))
            if frame.terrain_id is not None:
                map_ids.add(str(frame.terrain_id))
            for unit in frame.all_units:
                row, column = unit.row_col
                for key, value, minimize in (("min_row", row, True), ("max_row", row, False),
                                             ("min_column", column, True), ("max_column", column, False)):
                    extrema[key] = (min if minimize else max)(extrema.get(key, value), value)
            for side in ("RED", "BLUE"):
                if side not in perspective_errors:
                    try:
                        validate_recorded_visibility(frame, reader.manifest, side)
                    except ReplayPerspectiveUnavailable as exc:
                        perspective_errors[side] = f"Frame {index}：{exc}"
        if len(scenario_ids) > 1:
            raise ValueError("replay frames contain multiple scenario identities")
        scenario_id = next(iter(scenario_ids), None)
        map_id = next(iter(map_ids)) if len(map_ids) == 1 else None
        map_data = _map_record(reader, extrema, scenario_id, map_id, map_ids,
                               map_config=map_config, native_scenario_map=native_scenario_map,
                               map_coordinate_system=map_coordinate_system, scenario_config=scenario_config)
        return CompactReplayView(root, reader, indices, map_data, perspective_errors,
                                 scenario_id, map_id, load_validation,
                                 _embedded_contact_identities(reader))
    except (OSError, ValueError, TypeError, KeyError, BadZipFile) as exc:
        raise ReplayViewerLoadError(f"failed to load compact replay: {exc}") from exc


def _embedded_contact_identities(reader: CompactReplayReader) -> dict[int, dict[str, Any]]:
    """Old Native compact assets authorize static labels, not replay verification.

    Identity is never a roster endpoint. A source-frame contact still must be
    explicitly visible this frame and of the expected opposite side.
    """
    from wargame_native_engine.io.scenario import scenario_from_dict
    from wargame_native_engine.rules.perspective import recorded_perspective_policy
    try:
        policy = recorded_perspective_policy(reader.manifest)
    except ValueError:
        return {}
    if (policy.policy_version != "1.1.0"
            or reader.manifest.get("native_frame_projection") != "native-source-playback-projection/1.0"
            or reader.assets.get("schema") != "native-scenario-split/1.0"):
        return {}
    scenario_record, map_record = reader.assets.get("scenario"), reader.assets.get("map")
    if (not isinstance(scenario_record, Mapping) or not isinstance(map_record, Mapping)
            or "map" in scenario_record):
        return {}
    try:
        scenario = scenario_from_dict({**json_clone(scenario_record), "map": json_clone(map_record)})
    except (KeyError, TypeError, ValueError):
        return {}
    if (scenario.scenario_hash != reader.manifest.get("scenario_hash")
            or scenario.scenario_id != reader.manifest.get("scenario_id")):
        return {}
    return {key: {**value, "identity_verification": "HASH_BOUND_EMBEDDED_SCENARIO"}
            for key, value in scenario_contact_identities(scenario).items()}


def _map_record(reader: CompactReplayReader, extrema: Mapping[str, int],
                scenario_id: str | None, map_id: str | None, map_ids: set[str], **options: Any) -> dict[str, Any]:
    embedded = reader.assets.get("map")
    configured = bool(embedded) or options["map_config"] is not None or options["native_scenario_map"] is not None
    if options["native_scenario_map"] is not None:
        if scenario_id is None or map_id is None:
            raise ValueError("native scenario map requires unique replay scenario_id and terrain_id evidence")
        data = load_native_scenario_map_record(
            options["native_scenario_map"], expected_scenario_id=scenario_id, expected_map_id=map_id,
            expected_scenario_hash=reader.manifest.get("scenario_hash"),
            expected_map_hash=reader.manifest.get("map_hash"))
    elif options["map_config"] is not None:
        data = load_viewer_map_config(options["map_config"],
            coordinate_system_override=options["map_coordinate_system"]).to_record()
    elif reader.assets.get("schema") == "native-scenario-split/1.0":
        # Storage retains complete Native assets once each. The lossy display
        # map is derived here only, never substituted for those archived facts.
        from wargame_native_engine.io.scenario import scenario_from_dict
        scenario_record = reader.assets.get("scenario")
        if not isinstance(scenario_record, Mapping) or not isinstance(embedded, Mapping) or "map" in scenario_record:
            raise ValueError("invalid split Native scenario/map assets")
        if scenario_id is None or map_id is None:
            raise ValueError("split Native assets require scenario_id and terrain_id evidence")
        native_scenario = scenario_from_dict({**json_clone(scenario_record), "map": json_clone(embedded)})
        data = load_native_scenario_map_record(
            "embedded-scenario.json", loaded_scenario=native_scenario,
            expected_scenario_id=scenario_id, expected_map_id=map_id,
            expected_scenario_hash=reader.manifest.get("scenario_hash"),
            expected_map_hash=reader.manifest.get("map_hash"))
    elif isinstance(embedded, Mapping):
        data = json_clone(embedded)
    else:
        data = coordinate_fallback_record(())
        if extrema:
            data["bounds"] = dict(extrema)
            data["rows"] = extrema["max_row"] - extrema["min_row"] + 1
            data["columns"] = extrema["max_column"] - extrema["min_column"] + 1
    data.setdefault("scenario", None)
    if configured and data.get("map_id") is not None and map_ids and map_ids != {str(data["map_id"])}:
        raise ValueError("map config map_id does not match replay terrain_id")
    if options["scenario_config"] is not None:
        if scenario_id is None:
            raise ValueError("scenario config requires replay scenario_id evidence")
        data["scenario"] = load_viewer_scenario_config(
            options["scenario_config"], expected_scenario_id=scenario_id, expected_map_id=map_id).to_record()
    if configured:
        bounds = data["bounds"]
        if any(extrema.get(key, bounds[key]) < bounds[key] for key in ("min_row", "min_column")) or any(
            extrema.get(key, bounds[key]) > bounds[key] for key in ("max_row", "max_column")):
            raise ValueError("replay unit is outside map config bounds")
        scenario = data.get("scenario")
        if isinstance(scenario, Mapping):
            if scenario_id is not None and str(scenario.get("scenario_id")) != scenario_id:
                raise ValueError("scenario overlay does not match replay scenario_id")
            for feature in scenario.get("features", []):
                row, column = feature.get("row"), feature.get("column")
                if not (type(row) is int and type(column) is int and bounds["min_row"] <= row <= bounds["max_row"]
                        and bounds["min_column"] <= column <= bounds["max_column"]):
                    raise ValueError("scenario feature is outside map config bounds")
    return data
