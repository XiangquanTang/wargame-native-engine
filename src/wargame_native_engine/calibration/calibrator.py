from __future__ import annotations

from dataclasses import dataclass
import itertools
import json
from pathlib import Path
import statistics
import tempfile
from typing import Any, Iterable, Mapping

from wargame_native_engine.alignment.aligner import ReplayAligner, load_weights
from wargame_native_engine.calibration.parameter_space import (
    ParameterSpace, load_parameter_space,
)
from wargame_native_engine.io.importers.historical_replay import (
    HistoricalReplay, HistoricalReplayImporter,
)
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.rules.config import Ruleset, load_ruleset
from wargame_native_engine.version import ENGINE_VERSION


@dataclass(frozen=True, slots=True)
class ReplaySplit:
    split_id: str
    calibration_timestamp: str
    smoke: tuple[Mapping[str, Any], ...]
    calibration: tuple[Mapping[str, Any], ...]
    validation: tuple[Mapping[str, Any], ...]
    repository_root: Path


@dataclass(frozen=True, slots=True)
class CalibrationOutcome:
    calibration_id: str
    recommendation: str
    candidate_ruleset_hash: str
    output_dir: str
    comparison: Mapping[str, Any]


def load_replay_split(path: str | Path) -> ReplaySplit:
    source = Path(path).resolve()
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("replay split root must be an object")
    if value.get("schema_version") != "native-calibration-split/1.0":
        raise ValueError("unsupported replay split schema")
    partitions = {name: tuple(json_clone(value.get(name, [])))
                  for name in ("smoke", "calibration", "validation")}
    if not partitions["calibration"] or not partitions["validation"]:
        raise ValueError("calibration and validation partitions must be non-empty")
    ids = {name: [str(item["replay_id"]) for item in entries]
           for name, entries in partitions.items()}
    flattened = [item for values in ids.values() for item in values]
    if len(flattened) != len(set(flattened)):
        raise ValueError("replay partitions must be disjoint")
    return ReplaySplit(
        str(value["split_id"]), str(value.get("calibration_timestamp", "")),
        partitions["smoke"], partitions["calibration"],
        partitions["validation"], source.parents[2])


def calibrate(*, split_path: str | Path, baseline_ruleset_path: str | Path,
              parameter_space_path: str | Path, output_dir: str | Path,
              weights_path: str | Path | None = None) -> CalibrationOutcome:
    output = Path(output_dir)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to overwrite calibration: {output}")
    output.mkdir(parents=True, exist_ok=True)
    split = load_replay_split(split_path)
    space = load_parameter_space(parameter_space_path)
    baseline = load_ruleset(baseline_ruleset_path)
    baseline_document = baseline.to_dict()
    for definition in space.parameters:
        actual = float(_get_parameter(baseline_document, definition.parameter))
        if abs(actual - definition.baseline) > 1e-12:
            raise ValueError(
                f"parameter baseline does not match ruleset: {definition.parameter}")
    weights = (load_weights(weights_path) if weights_path is not None
               else {"position": 0.35, "event": 0.20,
                     "terminal": 0.30, "advantage": 0.15})
    calibration_replays = _load_partition(split, "calibration")
    baseline_calibration = _evaluate_replays(calibration_replays, baseline, weights)
    calibration_id = "calibration-" + canonical_hash({
        "split_id": split.split_id, "parent_hash": baseline.content_hash,
        "parameter_space": space.parameter_space_id,
        "weights": weights})[:20]
    candidates: list[Mapping[str, Any]] = []
    definitions = space.parameters
    for values in itertools.product(*(item.values() for item in definitions)):
        parameters = {definition.parameter: value
                      for definition, value in zip(definitions, values)}
        documents = _candidate_documents(
            baseline, parameters,
            provenance={"calibration_id": calibration_id,
                        "split_id": split.split_id,
                        "parent_ruleset_id": baseline.ruleset_id,
                        "parent_ruleset_version": baseline.ruleset_version,
                        "parent_ruleset_hash": baseline.content_hash,
                        "parameter_changes": parameters})
        candidate = _load_documents(documents)
        report = _evaluate_replays(calibration_replays, candidate, weights)
        record = {
            "parameters": parameters, "ruleset_hash": candidate.content_hash,
            "calibration_score": report["aggregate_score"],
            "per_replay": report["per_replay"],
            "parameter_hash": canonical_hash(parameters),
        }
        candidates.append(record)
    candidates.sort(key=_rank_key)
    best_record = candidates[0]
    best_documents = _candidate_documents(
        baseline, best_record["parameters"],
        provenance={
            "calibration_id": calibration_id, "split_id": split.split_id,
            "calibration_run_id": calibration_id,
            "parent_ruleset_id": baseline.ruleset_id,
            "parent_ruleset_version": baseline.ruleset_version,
            "parent_ruleset_hash": baseline.content_hash,
            "calibration_partition_replays": [item.replay_id
                                               for item in calibration_replays],
            "source_replay_hashes": [item["source_hash"] for item in
                                     (*split.calibration, *split.validation)],
            "objective_weights": weights,
            "calibration_timestamp": split.calibration_timestamp,
            "parameter_changes": best_record["parameters"],
            "limitations": ["small_sample_calibration",
                            "reduced_sanitized_fixtures",
                            "native_simplified_rules_only"],
        })
    best_ruleset = _load_documents(best_documents)
    best_calibration = _evaluate_replays(calibration_replays, best_ruleset, weights)
    validation_replays = _load_partition(split, "validation")
    baseline_validation = _evaluate_replays(validation_replays, baseline, weights)
    candidate_validation = _evaluate_replays(validation_replays, best_ruleset, weights)
    calibration_gain = (best_calibration["aggregate_score"]
                        - baseline_calibration["aggregate_score"])
    validation_gain = (candidate_validation["aggregate_score"]
                       - baseline_validation["aggregate_score"])
    recommendation, overfitting = _calibration_recommendation(
        calibration_gain=calibration_gain, validation_gain=validation_gain,
        calibration_count=len(calibration_replays),
        validation_count=len(validation_replays))
    comparison = {
        "calibration_id": calibration_id,
        "baseline_ruleset": {"id": baseline.ruleset_id,
                             "version": baseline.ruleset_version,
                             "hash": baseline.content_hash},
        "candidate_ruleset": {"id": best_ruleset.ruleset_id,
                              "version": best_ruleset.ruleset_version,
                              "hash": best_ruleset.content_hash},
        "split_id": split.split_id,
        "parameter_changes": best_record["parameters"],
        "candidate_count": len(candidates),
        "baseline_calibration_score": baseline_calibration["aggregate_score"],
        "candidate_calibration_score": best_calibration["aggregate_score"],
        "baseline_validation_score": baseline_validation["aggregate_score"],
        "candidate_validation_score": candidate_validation["aggregate_score"],
        "calibration_improvement": calibration_gain,
        "validation_improvement": validation_gain,
        "improvements": _metric_changes(
            baseline_calibration, best_calibration,
            baseline_validation, candidate_validation, positive=True),
        "regressions": _metric_changes(
            baseline_calibration, best_calibration,
            baseline_validation, candidate_validation, positive=False),
        "unsupported_evidence": {
            "baseline_calibration_actions": _unsupported_count(
                baseline_calibration),
            "candidate_calibration_actions": _unsupported_count(
                best_calibration),
            "baseline_validation_actions": _unsupported_count(
                baseline_validation),
            "candidate_validation_actions": _unsupported_count(
                candidate_validation),
        },
        "validation_passed": validation_gain >= 0 and not overfitting,
        "overfitting_suspected": overfitting,
        "small_sample_calibration": (
            len(calibration_replays) < 3 or len(validation_replays) < 2),
        "recommendation": recommendation,
        "limitations": best_documents["root"]["calibration_provenance"]["limitations"],
        "warnings": (["small_sample_calibration"]
                     if len(calibration_replays) < 3
                     or len(validation_replays) < 2 else []),
    }
    _write_json(output / "manifest.json", {
        "schema_version": "native-calibration/1.0",
        "calibration_id": calibration_id, "engine_version": ENGINE_VERSION,
        "split_id": split.split_id, "parameter_space_id": space.parameter_space_id,
        "search_method": space.search_method, "candidate_count": len(candidates),
        "baseline_ruleset_hash": baseline.content_hash,
        "candidate_ruleset_hash": best_ruleset.content_hash,
        "calibration_replay_ids": [item.replay_id for item in calibration_replays],
        "validation_replay_ids": [item.replay_id for item in validation_replays],
        "source_replay_hashes": [item["source_hash"] for item in
                                 (*split.calibration, *split.validation)],
        "calibration_timestamp": split.calibration_timestamp,
        "validation_used_during_search": False,
        "recommendation": recommendation})
    _write_jsonl(output / "candidates.jsonl", candidates)
    _write_json(output / "baseline_report.json", baseline_calibration)
    _write_json(output / "best_candidate_report.json", best_calibration)
    _write_json(output / "validation_report.json", {
        "partition": "validation", "search_completed_before_load": True,
        "baseline": baseline_validation, "candidate": candidate_validation})
    _write_json(output / "comparison.json", comparison)
    _write_candidate(output, best_documents, best_ruleset.content_hash)
    return CalibrationOutcome(calibration_id, recommendation,
                              best_ruleset.content_hash, str(output), comparison)


def evaluate_partition(*, split_path: str | Path, ruleset_path: str | Path,
                       partition: str, output_dir: str | Path,
                       weights_path: str | Path | None = None) -> Mapping[str, Any]:
    if partition not in {"smoke", "calibration", "validation"}:
        raise ValueError("unknown split partition")
    output = Path(output_dir)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to overwrite evaluation: {output}")
    output.mkdir(parents=True, exist_ok=True)
    split = load_replay_split(split_path)
    ruleset = load_ruleset(ruleset_path)
    weights = (load_weights(weights_path) if weights_path is not None
               else {"position": 0.35, "event": 0.20,
                     "terminal": 0.30, "advantage": 0.15})
    replays = _load_partition(split, partition)
    report = _evaluate_replays(replays, ruleset, weights)
    _write_json(output / "manifest.json", {
        "schema_version": "native-calibrated-evaluation/1.0",
        "split_id": split.split_id, "partition": partition,
        "ruleset_id": ruleset.ruleset_id,
        "ruleset_version": ruleset.ruleset_version,
        "ruleset_hash": ruleset.content_hash,
        "replay_ids": [item.replay_id for item in replays]})
    _write_json(output / "report.json", report)
    return report


def _load_partition(split: ReplaySplit, partition: str) -> tuple[HistoricalReplay, ...]:
    entries = getattr(split, partition)
    values: list[HistoricalReplay] = []
    for entry in entries:
        source = (split.repository_root / str(entry["source_identifier"])).resolve()
        result = HistoricalReplayImporter().import_path(source)
        replay = result.replay
        if replay.replay_id != entry["replay_id"]:
            raise ValueError("split replay ID mismatch")
        if replay.source_hash != entry["source_hash"]:
            raise ValueError(f"split source hash mismatch: {replay.replay_id}")
        if replay.evidence_level.value != entry["evidence_level"]:
            raise ValueError(f"split evidence mismatch: {replay.replay_id}")
        values.append(replay)
    return tuple(values)


def _evaluate_replays(replays: Iterable[HistoricalReplay], ruleset: Ruleset,
                      weights: Mapping[str, float]) -> Mapping[str, Any]:
    results = [ReplayAligner().align(replay, ruleset, weights=weights)
               for replay in replays]
    per_replay = [{
        **dict(item.summary),
        "trajectory_metrics": dict(item.trajectory_metrics),
        "event_metrics": dict(item.event_metrics),
        "terminal_metrics": dict(item.terminal_metrics),
        "advantage_metrics": dict(item.advantage_metrics),
        "time_alignment": dict(item.time_alignment),
        "operator_mapping": dict(item.operator_mapping),
        "warnings": list(item.warnings),
    } for item in results]
    return {
        "ruleset_id": ruleset.ruleset_id,
        "ruleset_version": ruleset.ruleset_version,
        "ruleset_hash": ruleset.content_hash,
        "replay_count": len(results),
        "aggregate_score": statistics.fmean(
            float(item.summary["alignment_score"]) for item in results),
        "aggregate_components": {
            name: statistics.fmean(float(item.summary["components"][name])
                                   for item in results)
            for name in ("position", "event", "terminal", "advantage")},
        "per_replay": per_replay,
    }


def _candidate_documents(baseline: Ruleset, parameters: Mapping[str, float],
                         provenance: Mapping[str, Any]) -> Mapping[str, Any]:
    expanded = baseline.to_dict()
    for path, value in sorted(parameters.items()):
        target: Any = expanded
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    expanded["ruleset_id"] = "native-replay-calibrated"
    expanded["ruleset_version"] = "1.0.0"
    expanded["ruleset_type"] = "historical_replay_calibrated_native_simplified"
    expanded["description"] = (
        "Historical-replay-calibrated native simplified rules candidate")
    expanded["calibration_provenance"] = json_clone(provenance)
    operators = expanded.pop("operator_types")
    weapons = expanded.pop("weapon_types")
    terrain = expanded.pop("terrain_types")
    expanded["operator_types"] = "operator_types.json"
    expanded["weapon_types"] = "weapon_types.json"
    expanded["terrain_types"] = "terrain_types.json"
    return {"root": expanded, "operators": operators,
            "weapons": weapons, "terrain": terrain}


def _get_parameter(document: Mapping[str, Any], path: str) -> Any:
    target: Any = document
    for part in path.split("."):
        if not isinstance(target, Mapping) or part not in target:
            raise ValueError(f"parameter path does not exist in baseline: {path}")
        target = target[part]
    return target


def _load_documents(documents: Mapping[str, Any]) -> Ruleset:
    with tempfile.TemporaryDirectory(prefix="native-calibration-", dir="/tmp") as root:
        path = Path(root)
        _write_json(path / "ruleset.json", documents["root"])
        _write_json(path / "operator_types.json", documents["operators"])
        _write_json(path / "weapon_types.json", documents["weapons"])
        _write_json(path / "terrain_types.json", documents["terrain"])
        return load_ruleset(path / "ruleset.json")


def _write_candidate(output: Path, documents: Mapping[str, Any],
                     content_hash: str) -> None:
    root = json_clone(documents["root"])
    root["declared_content_hash"] = content_hash
    _write_json(output / "calibrated_ruleset.json", root)
    _write_json(output / "operator_types.json", documents["operators"])
    _write_json(output / "weapon_types.json", documents["weapons"])
    _write_json(output / "terrain_types.json", documents["terrain"])
    loaded = load_ruleset(output / "calibrated_ruleset.json")
    if loaded.content_hash != content_hash:
        raise RuntimeError("written calibrated ruleset hash mismatch")


def _rank_key(record: Mapping[str, Any]) -> tuple[float, str]:
    return (-float(record["calibration_score"]), str(record["parameter_hash"]))


def _calibration_recommendation(*, calibration_gain: float,
                                validation_gain: float,
                                calibration_count: int,
                                validation_count: int) -> tuple[str, bool]:
    overfitting = calibration_gain > 0.05 and validation_gain < -0.05
    if overfitting:
        return "OVERFITTING_SUSPECTED", True
    if calibration_count < 3 or validation_count < 2:
        return "INSUFFICIENT_EVIDENCE", False
    if validation_gain >= 0:
        return "ACCEPT_CANDIDATE", False
    return "REJECT_CANDIDATE", False


def _unsupported_count(report: Mapping[str, Any]) -> int:
    return sum(int(item["unsupported_action_count"])
               for item in report["per_replay"])


def _metric_changes(baseline_calibration: Mapping[str, Any],
                    candidate_calibration: Mapping[str, Any],
                    baseline_validation: Mapping[str, Any],
                    candidate_validation: Mapping[str, Any], *,
                    positive: bool) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for partition, baseline, candidate in (
            ("calibration", baseline_calibration, candidate_calibration),
            ("validation", baseline_validation, candidate_validation)):
        values = {
            "alignment_score": (float(baseline["aggregate_score"]),
                                float(candidate["aggregate_score"])),
            **{
                f"component.{name}": (
                    float(baseline["aggregate_components"][name]),
                    float(candidate["aggregate_components"][name]))
                for name in ("position", "event", "terminal", "advantage")
            },
        }
        for metric, (before, after) in values.items():
            delta = after - before
            if (delta > 1e-12) is positive and abs(delta) > 1e-12:
                result.append({"partition": partition, "metric": metric,
                               "baseline": before, "candidate": after,
                               "delta": delta})
    return result


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(json_clone(value), ensure_ascii=False,
                               sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    path.write_text("".join(json.dumps(json_clone(value), ensure_ascii=False,
                                       sort_keys=True, separators=(",", ":")) + "\n"
                            for value in values), encoding="utf-8")
