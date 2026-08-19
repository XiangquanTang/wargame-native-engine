from __future__ import annotations

import argparse
from pathlib import Path

from examples.generate_public_battlefield import write_records
from examples.verify_replay import verify_replay
from scripts.run_realtime_agent_match import run_smoke
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.viewer.map_config import load_viewer_map_config


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples/public_river_valley_scenario.json"
MAP_CONFIG = ROOT / "examples/public_river_valley_map.json"


def test_public_battlefield_fixtures_are_reproducible_and_renderable(
    tmp_path: Path,
) -> None:
    generated_scenario, generated_map = write_records(tmp_path / "generated")

    assert generated_scenario.read_bytes() == SCENARIO.read_bytes()
    assert generated_map.read_bytes() == MAP_CONFIG.read_bytes()

    bundle = ScenarioBundle.from_path(SCENARIO)
    viewer_map = load_viewer_map_config(MAP_CONFIG)
    map_record = viewer_map.to_record()
    assert bundle.scenario_identity.scenario_id == "public-river-valley-contact"
    assert bundle.map_identity.map_id == "public-river-valley-24x18"
    assert len(bundle.scenario.operators) == 12
    assert len(bundle.scenario.map.hexes) == 432
    assert len(viewer_map.cells) == 432
    assert {cell.terrain_semantic for cell in viewer_map.cells} == {
        "open", "forest", "settlement", "soft_ground", "water",
    }
    assert map_record["elevation"] == {
        "mode": "episode", "minimum": 0.0, "maximum": 6.0, "levels": 7,
    }
    assert any(any(value for value in cell.roads) for cell in viewer_map.cells)
    assert any(any(value for value in cell.rivers) for cell in viewer_map.cells)


def test_public_battlefield_random_agents_publish_strict_replay(
    tmp_path: Path,
) -> None:
    output = tmp_path / "match"
    report = run_smoke(argparse.Namespace(
        scenario_bundle=SCENARIO,
        map_runtime_data=None,
        output_dir=output,
        run_namespace="public-battlefield-test",
        seed=42,
        tick_interval_ms=250,
        max_ticks=2,
        timeout_seconds=30.0,
        shutdown_timeout_seconds=30.0,
        max_catch_up_ticks_per_batch=4,
        red_agent="random",
        blue_agent="random",
        overwrite=False,
    ))

    assert report["map_id"] == "public-river-valley-24x18"
    assert report["tick_count"] == 2
    assert report["fact_frame_count"] == 3
    assert report["agent_failure_count"] == 0
    assert report["truncated"] is True
    verified = verify_replay(output / "replay.zip", scenario_bundle=SCENARIO)
    assert verified["matches"] is True
    assert verified["step_count"] == 2
