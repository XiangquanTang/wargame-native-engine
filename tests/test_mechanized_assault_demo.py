from __future__ import annotations

import json
from pathlib import Path

from scripts.run_demo import run_registered_demo


def test_mechanized_assault_executes_all_phases_and_strict_replay(
    tmp_path: Path,
) -> None:
    output = tmp_path / "mechanized-assault"
    report = run_registered_demo(
        "mechanized_assault_basic",
        output_dir=output,
    )
    validation = json.loads(
        (output / "validation_report.json").read_text(encoding="utf-8")
    )

    assert report["status"] == "PASS"
    assert report["capability_ids"] == [
        "boarding",
        "direct_fire",
        "objective_control",
        "visibility",
    ]
    assert report["phase_count"] == 8
    assert [value["name"] for value in report["phase_results"]] == [
        "boarding_started",
        "boarding_completed",
        "advance_to_objective",
        "unload_started",
        "unload_completed",
        "objective_controlled",
        "contact_confirmed",
        "direct_fire_resolved",
    ]
    assert all(value["pass"] for value in report["phase_results"])
    assert report["strict_replay_matches"] is True
    assert validation["status"] == "PASS"
    assert all(value["result"] for value in validation["checks"])
    assert (output / "replay.zip").is_file()
    assert (output / "evidence.json").is_file()
    assert (output / "summary.json").is_file()
