from __future__ import annotations

import json
from pathlib import Path

import pytest

from wargame_native_engine.adjudication import AdjudicationTableSet
from wargame_native_engine.adjudication.replay import replay_direct_fire
from wargame_native_engine.cli.inspect_source_frame import main as inspect_main
from wargame_native_engine.cli.resolve_direct_fire import main as direct_fire_main
from wargame_native_engine.cli.run_source_compatible_game import main as source_run_main


ROOT = Path(__file__).resolve().parents[1]
SOURCE_96 = ROOT / "local_data/source_frames/96.json"
FINAL_TABLES = ROOT / "config/adjudication/source_tables_v1/native_adjudication_tables_final.json"


def test_direct_fire_cli_outputs_strict_replay(tmp_path: Path) -> None:
    output = tmp_path / "direct"
    assert direct_fire_main([
        "--scenario", str(ROOT / "examples/direct_fire_scenario.json"),
        "--attacker", "101", "--target", "201", "--weapon", "9101",
        "--seed", "42", "--output", str(output),
    ]) == 0
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "native-direct-fire-replay/1.0"
    assert replay_direct_fire(output, tables=AdjudicationTableSet(FINAL_TABLES)).matches


def test_source_frame_inspection_cli(tmp_path: Path) -> None:
    if not SOURCE_96.exists():
        pytest.skip("96.json remains local_data")
    output = tmp_path / "inspect"
    assert inspect_main(["--input", str(SOURCE_96),
                         "--output", str(output)]) == 0
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["operators"] == 34 and summary["passengers"] == 23
    assert summary["valid_actions_used_as_rule_truth"] is False


def test_source_compatible_cli_reports_missing_map_instead_of_guessing(
        tmp_path: Path) -> None:
    if not SOURCE_96.exists():
        pytest.skip("96.json remains local_data")
    output = tmp_path / "source-run"
    assert source_run_main([
        "--frame", str(SOURCE_96),
        "--ruleset", str(ROOT / "config/rulesets/native_default_v1.json"),
        "--table-set", str(ROOT / "config/adjudication/source_tables_v1/manifest.json"),
        "--max-events", "20", "--output", str(output),
    ]) == 0
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "COMPATIBILITY_LOAD_ONLY"
    assert manifest["events_processed"] == 0
    assert any("terrain elevation" in item for item in manifest["warnings"])
