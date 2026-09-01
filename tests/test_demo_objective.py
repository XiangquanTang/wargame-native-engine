from __future__ import annotations

import json
from pathlib import Path

from scripts.run_demo import run_registered_demo
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.demo_registry import load_default_demo_registry


def test_objective_demo_loads_existing_objective_contract() -> None:
    registry = load_default_demo_registry()
    bundle = ScenarioBundle.from_path(
        registry.scenario_path("objective_control_basic")
    )

    assert len(bundle.scenario.objectives) == 1
    assert bundle.scenario.objectives[0].objective_id == 1
    assert bundle.scenario.objectives[0].controlling_side is None


def test_objective_demo_runner_records_transfer_contest_and_scores(
    tmp_path: Path,
) -> None:
    output = tmp_path / "objective"
    report = run_registered_demo(
        "objective_control_basic",
        output_dir=output,
        seed=42,
        tick_interval_ms=5_000,
    )
    evidence = json.loads(
        (output / "evidence.json").read_text(encoding="utf-8")
    )
    cases = evidence["authority_cases"]

    assert report["status"] == "PASS"
    assert report["strict_replay_matches"] is True
    assert cases["red_enters"]["controlling_side"] == "red"
    assert cases["red_enters"]["score_delta"] == {"blue": 0, "red": 1}
    assert cases["blue_enters"]["controlling_side"] == "blue"
    assert cases["blue_enters"]["score_delta"] == {"blue": 1, "red": 0}
    assert cases["contested"]["controlling_side"] is None
    assert cases["contested"]["score_delta"] == {"blue": 0, "red": 0}
    assert (output / "replay.zip").is_file()
