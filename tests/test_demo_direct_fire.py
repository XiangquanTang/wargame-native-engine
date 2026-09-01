from __future__ import annotations

import json
from pathlib import Path

from scripts.run_demo import run_registered_demo
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.demo_registry import load_default_demo_registry


def _bundle() -> ScenarioBundle:
    registry = load_default_demo_registry()
    return ScenarioBundle.from_path(
        registry.scenario_path("direct_fire_basic")
    )


def test_direct_fire_demo_exposes_legacy_shoot_with_explicit_weapon() -> None:
    engine = NativeEngine(_bundle().scenario)
    try:
        engine.reset(42)
        matches = [
            value.action for value in engine.valid_actions("red")
            if value.action.action_type.value == "SHOOT"
        ]
        assert len(matches) == 1
        assert matches[0].actor_id == 1
        assert matches[0].target_id == 2
        assert matches[0].parameters["weapon_id"] == 36
    finally:
        engine.close()


def test_direct_fire_demo_runner_changes_target_and_strict_replays(
    tmp_path: Path,
) -> None:
    output = tmp_path / "direct"
    report = run_registered_demo(
        "direct_fire_basic", output_dir=output, seed=42
    )

    assert report["status"] == "PASS"
    assert report["command_status"] == "APPLIED"
    assert report["target_state_changed"] is True
    assert report["strict_replay_matches"] is True
    validation = json.loads(
        (output / "validation_report.json").read_text(encoding="utf-8")
    )
    assert validation["status"] == "PASS"
    assert all(value["result"] for value in validation["checks"])
    for name in ("replay.zip", "evidence.json", "summary.json"):
        assert (output / name).is_file()
