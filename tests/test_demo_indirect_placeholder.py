from __future__ import annotations

import json
from pathlib import Path

from scripts.run_demo import run_registered_demo
from wargame_native_engine.demo_registry import load_default_demo_registry


def test_indirect_demo_declares_full_cycle_contract() -> None:
    expected = load_default_demo_registry().expected_behavior(
        "indirect_fire_basic"
    )

    assert expected["preconditions"]["indirect_capability_available"] is True
    assert expected["outcome"]["event_types"] == [
        "indirect_fire_mission_created",
        "indirect_fire_active_started",
        "indirect_fire_area_triggered",
        "indirect_fire_resolved",
    ]


def test_indirect_demo_executes_official_full_cycle(
    tmp_path: Path,
) -> None:
    output = tmp_path / "indirect"
    report = run_registered_demo(
        "indirect_fire_basic",
        output_dir=output,
        seed=5,
        tick_interval_ms=150_000,
    )
    evidence = json.loads(
        (output / "evidence.json").read_text(encoding="utf-8")
    )
    validation = json.loads(
        (output / "validation_report.json").read_text(encoding="utf-8")
    )

    assert report["status"] == "PASS"
    assert report["strict_replay_matches"] is True
    assert evidence["authority_probe"]["event_types"] == [
        "indirect_fire_mission_created",
        "indirect_fire_active_started",
        "indirect_fire_area_triggered",
        "indirect_fire_resolved",
    ]
    assert validation["status"] == "PASS"
    assert all(value["result"] for value in validation["checks"])
    assert (output / "replay.zip").is_file()
