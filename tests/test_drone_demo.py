from __future__ import annotations

import json
from pathlib import Path

from scripts.run_benchmark import run_capability_benchmark
from scripts.run_demo import run_registered_demo
from wargame_native_engine.demo_registry import load_default_demo_registry
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)


def test_drone_demo_registry_contract() -> None:
    descriptor = load_default_demo_registry().resolve("drone_basic")

    assert descriptor.capability_ids == ("drone",)
    assert descriptor.features == ("DRONE",)


def test_drone_demo_human_agent_observation_and_strict_replay(
    tmp_path: Path,
) -> None:
    output = tmp_path / "drone-demo"
    report = run_registered_demo(
        "drone_basic",
        output_dir=output,
        seed=None,
    )
    validation = json.loads(
        (output / "validation_report.json").read_text(encoding="utf-8")
    )
    checks = {value["name"]: value["result"] for value in validation["checks"]}

    assert report["status"] == "PASS"
    assert report["command_status"] == "APPLIED"
    assert report["visibility_refreshed"] is True
    assert report["no_attack_capability"] is True
    assert report["no_limited_lifecycle"] is True
    assert report["strict_replay_matches"] is True
    assert checks["human_move_capability"] is True
    assert checks["agent_move_capability"] is True
    assert checks["agent_wait_capability"] is True
    assert checks["no_weapon_posture_capability"] is True

    archive = SourceReplayArchiveImporter().import_path(output / "replay.zip")
    ordered = [
        action
        for step in archive.steps
        for action in step.get("ordered_actions", ())
    ]
    assert any(
        action.get("actor_id") == 1 and action.get("action_type") == "MOVE"
        for action in ordered
    )
    assert not any(
        action.get("actor_id") == 1
        and action.get("action_type") in {"SHOOT", "FIRE"}
        for action in ordered
    )


def test_drone_capability_benchmark_passes(tmp_path: Path) -> None:
    report = run_capability_benchmark(
        "drone_basic",
        output_dir=tmp_path / "benchmark",
    )

    assert report["passed"] is True
    assert report["engine_result"]["passed"] is True
    assert report["human_result"]["result"] == "PASS"
    assert report["agent_result"]["result"] == "PASS"
    assert report["replay_result"]["matches"] is True
