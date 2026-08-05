from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from wargame_native_engine.calibration.calibrator import (
    _calibration_recommendation, calibrate,
)
from wargame_native_engine.calibration.parameter_space import load_parameter_space
from wargame_native_engine.cli.calibrate_rules import main as calibration_main
from wargame_native_engine.rules.config import load_ruleset


ROOT = Path(__file__).resolve().parents[1]
SPLIT = ROOT / "config" / "calibration" / "replay_split_v1.json"
PARAMETERS = ROOT / "config" / "calibration" / "parameter_space_v1.json"
WEIGHTS = ROOT / "config" / "calibration" / "metric_weights_v1.json"
BASELINE = ROOT / "config" / "rulesets" / "native_default_v1.json"


@pytest.fixture(scope="module")
def calibration_output(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("calibration") / "result"
    calibrate(split_path=SPLIT, baseline_ruleset_path=BASELINE,
              parameter_space_path=PARAMETERS, weights_path=WEIGHTS,
              output_dir=output)
    return output


def test_parameter_space_validation(tmp_path: Path) -> None:
    space = load_parameter_space(PARAMETERS)
    assert space.search_method == "grid_search"
    invalid = json.loads(PARAMETERS.read_text())
    invalid["parameters"][0]["minimum"] = -1
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(invalid))
    with pytest.raises(ValueError, match="bounds"):
        load_parameter_space(path)


def test_candidate_ruleset_hash(calibration_output: Path) -> None:
    manifest = json.loads((calibration_output / "manifest.json").read_text())
    rules = load_ruleset(calibration_output / "calibrated_ruleset.json")
    assert rules.content_hash == manifest["candidate_ruleset_hash"]


def test_baseline_ruleset_is_not_modified(tmp_path: Path) -> None:
    before = hashlib.sha256(BASELINE.read_bytes()).hexdigest()
    calibrate(split_path=SPLIT, baseline_ruleset_path=BASELINE,
              parameter_space_path=PARAMETERS, weights_path=WEIGHTS,
              output_dir=tmp_path / "run")
    assert hashlib.sha256(BASELINE.read_bytes()).hexdigest() == before


def test_calibration_uses_only_calibration_split(calibration_output: Path) -> None:
    manifest = json.loads((calibration_output / "manifest.json").read_text())
    assert manifest["calibration_replay_ids"] == [
        "calibration-action-01", "calibration-action-02"]
    candidates = [json.loads(line) for line in
                  (calibration_output / "candidates.jsonl").read_text().splitlines()]
    assert all(len(item["per_replay"]) == 2 for item in candidates)
    baseline = json.loads((calibration_output / "baseline_report.json").read_text())
    assert "trajectory_metrics" in baseline["per_replay"][0]


def test_validation_split_is_not_used_for_search(calibration_output: Path) -> None:
    manifest = json.loads((calibration_output / "manifest.json").read_text())
    report = json.loads((calibration_output / "validation_report.json").read_text())
    assert manifest["validation_used_during_search"] is False
    assert report["search_completed_before_load"] is True


def test_candidate_ranking_is_deterministic(tmp_path: Path,
                                            calibration_output: Path) -> None:
    second = tmp_path / "second"
    calibrate(split_path=SPLIT, baseline_ruleset_path=BASELINE,
              parameter_space_path=PARAMETERS, weights_path=WEIGHTS,
              output_dir=second)
    first = json.loads((calibration_output / "manifest.json").read_text())
    other = json.loads((second / "manifest.json").read_text())
    assert first["candidate_ruleset_hash"] == other["candidate_ruleset_hash"]
    assert (calibration_output / "candidates.jsonl").read_bytes() == (
        second / "candidates.jsonl").read_bytes()


def test_parameter_bounds_are_respected(calibration_output: Path) -> None:
    candidates = [json.loads(line) for line in
                  (calibration_output / "candidates.jsonl").read_text().splitlines()]
    assert len(candidates) == 8
    assert all(1.5 <= item["parameters"][
        "movement.operator_type_ranges.ground_unit"] <= 2.0
               for item in candidates)


def test_overfitting_is_reported() -> None:
    recommendation, overfit = _calibration_recommendation(
        calibration_gain=0.2, validation_gain=-0.2,
        calibration_count=5, validation_count=3)
    assert recommendation == "OVERFITTING_SUSPECTED"
    assert overfit is True


def test_calibrated_ruleset_records_provenance(calibration_output: Path) -> None:
    value = json.loads((calibration_output / "calibrated_ruleset.json").read_text())
    provenance = value["calibration_provenance"]
    assert provenance["parent_ruleset_hash"] == load_ruleset(BASELINE).content_hash
    assert provenance["split_id"] == "native-replay-calibration-v1"
    assert len(provenance["source_replay_hashes"]) == 3
    assert "small_sample_calibration" in provenance["limitations"]
    comparison = json.loads((calibration_output / "comparison.json").read_text())
    assert comparison["improvements"]
    assert comparison["regressions"] == []
    assert comparison["unsupported_evidence"]["candidate_validation_actions"] == 0


def test_calibration_cli_outputs_reports(tmp_path: Path) -> None:
    output = tmp_path / "cli"
    assert calibration_main([
        "--split", str(SPLIT), "--baseline-ruleset", str(BASELINE),
        "--parameter-space", str(PARAMETERS), "--weights", str(WEIGHTS),
        "--output", str(output)]) == 0
    assert {"manifest.json", "candidates.jsonl", "baseline_report.json",
            "best_candidate_report.json", "validation_report.json",
            "comparison.json", "calibrated_ruleset.json"} <= {
                item.name for item in output.iterdir()}
