from __future__ import annotations

import json
from pathlib import Path

from scripts.measure_decision_window_scaling import _scaling_scenario
from scripts.run_engine_contract_acceptance import run_acceptance
from wargame_native_engine.io.scenario import load_scenario


def test_reversed_side_arrival_has_identical_exact_time_evidence(
    tmp_path: Path,
) -> None:
    fixture = load_scenario(Path("examples/source_movement_scenario.json"))
    scenario = _scaling_scenario(fixture.map.to_dict(), 4)
    report = run_acceptance(
        scenario,
        tmp_path / "acceptance",
        decision_windows=3,
        frame_interval_ms=250,
        seed=42,
    )

    assert report["status"] == "passed"
    assert report["replay_bytes_equal"] is True
    assert report["analysis_bytes_equal"] is True
    assert report["expected_sim_time_ms"] == 750
    for value in report["runs"].values():
        assert value["strict_replay_match"] is True
        assert value["online_offline_analysis_equal"] is True
        assert value["final_frame_index"] == 3
        assert value["final_sim_time_ms"] == 750
        assert value["frame_count"] == 4
        assert value["step_count"] == 3

    persisted = json.loads(
        (tmp_path / "acceptance" / "engine_contract_acceptance.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["replay_bytes_equal"] is True
