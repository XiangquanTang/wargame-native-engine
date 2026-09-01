from __future__ import annotations

import json
from pathlib import Path

from scripts.run_demo import run_registered_demo
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.demo_registry import load_default_demo_registry


def test_visibility_demo_scenario_contains_three_declared_cases() -> None:
    registry = load_default_demo_registry()
    bundle = ScenarioBundle.from_path(
        registry.scenario_path("visibility_blocking")
    )
    expected = registry.expected_behavior("visibility_blocking")

    assert len(expected["cases"]) == 3
    assert len(bundle.scenario.operators) == 6
    assert any(value.elevation == 9 for value in bundle.scenario.map.hexes)
    assert any(value.terrain == 1 for value in bundle.scenario.map.hexes)


def test_visibility_demo_runner_matches_observation_and_writes_evidence(
    tmp_path: Path,
) -> None:
    output = tmp_path / "visibility"
    report = run_registered_demo(
        "visibility_blocking", output_dir=output, seed=42
    )
    evidence = json.loads(
        (output / "visibility_evidence.json").read_text(encoding="utf-8")
    )
    cases = {value["case_id"]: value for value in evidence["cases"]}

    assert report["status"] == "PASS"
    assert report["strict_replay_matches"] is True
    assert cases["unobstructed"]["observable"] is True
    assert cases["elevation_blocked"]["reason"] == "line_of_sight_blocked"
    assert cases["elevation_blocked"]["blocking_hexes"] == [202]
    assert cases["forest_range_modifier"]["terrain_modifier"] is True
    assert cases["forest_range_modifier"]["observable"] is False
    assert (output / "replay.zip").is_file()
