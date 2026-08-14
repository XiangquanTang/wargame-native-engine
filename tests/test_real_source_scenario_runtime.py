from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.run_source_native_match import run_source_native_match
from wargame_native_engine.data import (
    EXPLICIT_NATIVE_SUPPLEMENT,
    convert_source_scenario,
    publish_source_scenario_conversion,
)
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)


SNAPSHOT_ROOT = Path(
    "/home/txq/wargame/wargame_benchmark_lab/local_data/"
    "sdk_data_snapshot/phase9a_multireplay_pilot"
)
SOURCE_SCENARIO = SNAPSHOT_ROOT / "scenarios/2201010105.json"
SOURCE_MAP = SNAPSHOT_ROOT / "maps/123/basic.json"
SUPPLEMENT = Path(
    "config/source_scenario_conversion/scenarios/"
    "2201010105.native-acceptance-supplement.json"
)


def test_real_source_scenario_move_shoot_replay_product_loop(tmp_path: Path) -> None:
    if not SOURCE_SCENARIO.is_file() or not SOURCE_MAP.is_file():
        pytest.skip("read-only Map 123 Source acceptance inputs are unavailable")
    hashes_before = (_sha256(SOURCE_SCENARIO), _sha256(SOURCE_MAP))
    conversion = convert_source_scenario(
        SOURCE_SCENARIO,
        SOURCE_MAP,
        conversion_mode=EXPLICIT_NATIVE_SUPPLEMENT,
        source_scenario_identity="scenarios/2201010105.json",
        source_map_identity="maps/123/basic.json",
        expected_scenario_id="2201010105",
        expected_map_id="123",
        supplement_path=SUPPLEMENT,
    )
    published = publish_source_scenario_conversion(
        conversion, tmp_path / "canonical-bundle"
    )
    report = run_source_native_match(
        published.scenario_path,
        tmp_path / "match",
        seed=42,
        decision_interval_ms=60_000,
        frame_interval_ms=60_000,
        max_decision_windows=20,
    )

    assert report["status"] == "passed"
    assert report["scenario"]["operator_count"] == 49
    assert report["scenario"]["red_count"] == 26
    assert report["scenario"]["blue_count"] == 23
    assert report["scenario"]["map_sha256"] == (
        conversion.manifest["native_map"]["sha256"]
    )
    paths = report["move"]["recorded_paths"]
    assert {key: len(value["path"]) for key, value in paths.items()} == {
        "27": 3,
        "28": 4,
        "29": 5,
    }
    for unit_id in ("27", "28", "29"):
        positions = report["move"]["frame_positions"][unit_id]
        assert positions[0]["hex"] == report["move"]["initial_positions"][unit_id]
        assert positions[-1]["hex"] == 2940
        assert len({item["hex"] for item in positions}) >= 2
    assert report["shoot"]["recorded_action"]["action"] == {
        "action_type": "SHOOT",
        "actor_id": 71,
        "parameters": {"weapon_id": 73},
        "side": "red",
        "target_id": 75,
        "target_position": None,
    }
    judge_events = report["shoot"]["judge_info"]["judge_info"][0]["result"][
        "events"
    ]
    validated = next(
        item for item in judge_events if item["event_type"] == "direct_fire_validated"
    )
    assert validated["data"]["legal"] is True
    assert validated["data"]["evidence"]["checks"]["target_observed"] is True
    assert report["runtime"]["terminal"] is True
    assert report["runtime"]["termination_reason"] == "max_ticks"
    assert report["runtime"]["frame_count"] == 16
    assert report["runtime"]["step_count"] == 15
    assert report["runtime"]["final_sim_time_ms"] == 900_000
    assert report["evidence"]["strict_replay_match"] is True
    assert report["evidence"]["online_offline_analysis_match"] is True

    archive = SourceReplayArchiveImporter().import_path(
        tmp_path / "match/replay.zip"
    )
    assert len(archive.frames) == 16
    assert len(archive.steps) == 15
    assert any(
        action["action_type"] == "SHOOT"
        for step in archive.steps
        for action in step["ordered_actions"]
    )
    assert hashes_before == (_sha256(SOURCE_SCENARIO), _sha256(SOURCE_MAP))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
