from __future__ import annotations

import json
from pathlib import Path

from scripts.run_native_training_smoke import main


SCENARIO = Path("examples/source_movement_scenario.json")


def test_native_training_cli_publishes_true_terminal_episode(
    tmp_path: Path,
) -> None:
    record = json.loads(SCENARIO.read_text(encoding="utf-8"))
    record["scenario_id"] = "native-training-cli-terminal"
    blue = next(
        value for value in record["operators"] if value["side"] == "blue"
    )
    blue.update({"health": 0, "blood": 0, "alive": False})
    scenario = tmp_path / "terminal_scenario.json"
    scenario.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"

    assert main([
        "--scenario-bundle", str(scenario),
        "--output-dir", str(output),
        "--run-namespace", "test-native-training-cli",
        "--seed", "42",
        "--decision-interval-ms", "2000",
        "--frame-interval-ms", "1000",
        "--max-decision-windows", "2",
        "--red-agent", "wait",
        "--blue-agent", "wait",
    ]) == 0

    summary = json.loads(
        (output / "match_summary.native.json").read_text(encoding="utf-8")
    )
    assert summary["terminal"] is True
    assert summary["truncated"] is False
    assert summary["winner"] == "red"
    assert summary["decision_window_count"] == 1
    assert summary["transition_count"] == 2
    assert summary["strict_replay_match"] is True
    assert summary["online_offline_analysis_match"] is True
    assert (output / "replay.zip").is_file()
    assert (output / "frame_analysis.native.jsonl").is_file()
    assert (output / "decision_transitions.native.jsonl").is_file()
