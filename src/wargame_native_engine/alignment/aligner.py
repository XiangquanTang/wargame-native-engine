from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.alignment.lanchester_adapter import (
    compare_advantage_curves, native_advantage_curve,
)
from wargame_native_engine.alignment.metrics import (
    alignment_score, combat_result_metrics, event_metrics, terminal_metrics,
    time_metrics, trajectory_metrics,
)
from wargame_native_engine.alignment.operator_matching import OperatorMatcher
from wargame_native_engine.alignment.time_axis import (
    TimeAlignmentConfig, align_times,
)
from wargame_native_engine.calibration.replay_driver import (
    HistoricalReplayDriver, _native_frame,
)
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.importers.historical_replay import (
    EvidenceLevel, HistoricalReplay,
)
from wargame_native_engine.io.importers.historical_scenario import (
    HistoricalScenarioBuilder,
)
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.rules.config import Ruleset
from wargame_native_engine.version import ENGINE_VERSION


DEFAULT_WEIGHTS = {"position": 0.35, "event": 0.20,
                   "terminal": 0.30, "advantage": 0.15}


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    manifest: Mapping[str, Any]
    operator_mapping: Mapping[str, Any]
    time_alignment: Mapping[str, Any]
    trajectory_metrics: Mapping[str, Any]
    event_metrics: Mapping[str, Any]
    terminal_metrics: Mapping[str, Any]
    advantage_metrics: Mapping[str, Any]
    warnings: tuple[Mapping[str, Any], ...]
    summary: Mapping[str, Any]
    replay_driven: Mapping[str, Any] | None = None

    def write(self, output_dir: str | Path) -> Path:
        root = Path(output_dir)
        if root.exists() and any(root.iterdir()):
            raise FileExistsError(f"refusing to overwrite alignment: {root}")
        root.mkdir(parents=True, exist_ok=True)
        values = {
            "manifest.json": self.manifest,
            "operator_mapping.json": self.operator_mapping,
            "time_alignment.json": self.time_alignment,
            "trajectory_metrics.json": self.trajectory_metrics,
            "event_metrics.json": self.event_metrics,
            "terminal_metrics.json": self.terminal_metrics,
            "advantage_metrics.json": self.advantage_metrics,
            "warnings.json": {"warnings": list(self.warnings)},
            "summary.json": self.summary,
        }
        if self.replay_driven is not None:
            values["replay_driven.json"] = self.replay_driven
        for name, value in values.items():
            (root / name).write_text(json.dumps(
                json_clone(value), ensure_ascii=False, sort_keys=True,
                indent=2) + "\n", encoding="utf-8")
        return root


class ReplayAligner:
    def align(self, replay: HistoricalReplay, ruleset: Ruleset, *,
              weights: Mapping[str, float] | None = None,
              unsupported_policy: str = "skip_tick") -> AlignmentResult:
        active_weights = _validate_weights(weights or DEFAULT_WEIGHTS)
        build = HistoricalScenarioBuilder().build(replay, ruleset)
        warnings: list[Mapping[str, Any]] = list(replay.warnings) + list(build.warnings)
        replay_driven: Mapping[str, Any] | None = None
        if replay.evidence_level is EvidenceLevel.ACTION_LEVEL:
            driven = HistoricalReplayDriver().run(
                replay, ruleset, unsupported_policy=unsupported_policy)
            native_frames = driven.frames
            native_events = driven.events
            native_summary = driven.summary
            replay_driven = driven.to_dict()
            warnings.extend(driven.conflicts)
            mode = "REPLAY_DRIVEN_ADJUDICATION"
        else:
            engine = NativeEngine(build.scenario, ruleset=ruleset)
            engine.reset(0)
            HistoricalReplayDriver._deploy(engine)
            native_frames = (_native_frame(
                engine, replay.frames[0].historical_time if replay.frames else 0.0),)
            native_events = ()
            native_summary = engine.result().to_dict()
            engine.close()
            mode = {
                EvidenceLevel.STATE_TRAJECTORY_LEVEL: "STATE_TRAJECTORY_ALIGNMENT",
                EvidenceLevel.EVENT_LEVEL: "EVENT_ALIGNMENT",
                EvidenceLevel.SUMMARY_ONLY: "SUMMARY_ALIGNMENT",
            }[replay.evidence_level]
            warnings.append({"code": "replay_driven_not_permitted",
                             "evidence_level": replay.evidence_level.value,
                             "invented_action_count": 0})
        first_native = native_frames[0]["operators"] if native_frames else []
        matches = OperatorMatcher().match(
            replay.frames[0].operators if replay.frames else (), first_native)
        alignment_config = _time_config(replay)
        pairs = align_times(
            (item.historical_time for item in replay.frames),
            (float(item["native_tick"]) for item in native_frames),
            alignment_config)
        trajectory = trajectory_metrics(replay, native_frames, pairs, matches)
        times = time_metrics(replay, native_frames, native_events)
        events = dict(event_metrics(replay, native_events))
        events["time_metrics"] = times
        terminal = dict(terminal_metrics(replay, native_summary, native_frames))
        terminal["combat_results"] = combat_result_metrics(replay, native_frames)
        advantage = compare_advantage_curves(
            replay.advantage_curve, native_advantage_curve(native_frames))
        score = alignment_score(trajectory, events, terminal, advantage,
                                active_weights)
        summary = {
            "replay_id": replay.replay_id, "mode": mode,
            "evidence_level": replay.evidence_level.value,
            **score,
            "unsupported_action_count": sum(
                item.get("code") == "unsupported_action" for item in warnings),
            "warning_count": len(warnings),
            "comparable": trajectory["status"] == "comparable",
        }
        manifest = {
            "schema_version": "native-alignment/1.0",
            "replay_id": replay.replay_id, "source_hash": replay.source_hash,
            "scenario_id": replay.scenario_id, "map_id": replay.map_id,
            "evidence_level": replay.evidence_level.value, "mode": mode,
            "engine_version": ENGINE_VERSION,
            "ruleset_id": ruleset.ruleset_id,
            "ruleset_version": ruleset.ruleset_version,
            "ruleset_hash": ruleset.content_hash,
            "scenario_hash": build.scenario.scenario_hash,
            "alignment_hash": canonical_hash({
                "replay": replay.source_hash, "ruleset": ruleset.content_hash,
                "time": {"strategy": alignment_config.strategy,
                         "scale": alignment_config.scale,
                         "offset": alignment_config.offset},
                "weights": active_weights}),
        }
        return AlignmentResult(
            manifest,
            {"matches": [item.to_dict() for item in matches],
             "matched_count": sum(item.native_operator_id is not None
                                  for item in matches),
             "unmatched_count": sum(item.native_operator_id is None
                                    for item in matches)},
            {"strategy": alignment_config.strategy,
             "alignment_scale": alignment_config.scale,
             "alignment_offset": alignment_config.offset,
             "historical_time_unit": replay.time_unit,
             "native_time_unit": "native_tick",
             "pairs": [item.to_dict() for item in pairs]},
            trajectory, events, terminal, advantage, tuple(warnings), summary,
            replay_driven)


def load_weights(path: str | Path) -> Mapping[str, float]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping) or not isinstance(value.get("weights"), Mapping):
        raise ValueError("alignment weights document is invalid")
    return _validate_weights(value["weights"])


def _time_config(replay: HistoricalReplay) -> TimeAlignmentConfig:
    value = replay.metadata.get("time_alignment", {})
    return TimeAlignmentConfig(
        strategy=str(value.get("strategy", "nearest")),
        scale=float(value.get("scale", 1.0)),
        offset=float(value.get("offset", 0.0)))


def _validate_weights(value: Mapping[str, float]) -> Mapping[str, float]:
    expected = {"position", "event", "terminal", "advantage"}
    if set(value) != expected:
        raise ValueError("alignment weights must contain all components")
    result = {str(key): float(item) for key, item in value.items()}
    if any(item < 0 for item in result.values()) or abs(sum(result.values()) - 1.0) > 1e-9:
        raise ValueError("alignment weights must be non-negative and sum to one")
    return result
