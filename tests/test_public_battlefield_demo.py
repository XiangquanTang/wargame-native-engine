from __future__ import annotations

import argparse
import json
from pathlib import Path

from examples.generate_public_battlefield import write_records
from examples.verify_replay import verify_replay
from scripts.run_realtime_agent_match import run_smoke
from wargame_native_engine.agents import (
    CommandStatus,
    ManualRealtimeClock,
    RealtimeAgentSession,
    SeatOperation,
)
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.viewer.map_config import load_viewer_map_config


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples/public_river_valley_scenario.json"
MAP_CONFIG = ROOT / "examples/public_river_valley_map.json"


class _PublicMatchFactory:
    def __init__(self, root: Path, bundle: ScenarioBundle) -> None:
        self.root = root
        self.bundle = bundle

    def __call__(self, seed: int, episode_sequence: int):
        app = WargameFacade(
            ScenarioCatalog.from_bundles({
                self.bundle.scenario_identity.scenario_id: self.bundle,
            }),
            storage_root=self.root / f"managed-{episode_sequence}",
        )
        return app.create_match(
            scenario_id=self.bundle.scenario_identity.scenario_id,
            red_controller_id="public-demo-red",
            blue_controller_id="public-demo-blue",
            seed=seed,
            decision_interval_ms=1_000,
            frame_interval_ms=1_000,
        )


def _submit_operation(
    session: RealtimeAgentSession,
    binding,
    operation: SeatOperation,
    *,
    request_id: str,
) -> int:
    view = session.latest_view(binding)
    preview = session.preview_operation(
        binding,
        operation,
        observation_version=view.observation_version,
        request_id=request_id,
    )
    assert preview.can_execute is True
    receipt = session.submit_operation(
        binding,
        observation_version=view.observation_version,
        operation=operation,
    )
    assert receipt.accepted is True
    assert receipt.command_sequence is not None
    return receipt.command_sequence


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
    operators = {value.operator_id: value for value in bundle.scenario.operators}
    assert operators[103].current_hex == 711
    assert operators[203].current_hex == 717


def test_public_battlefield_exposes_fire_and_product_operations_with_strict_replay(
    tmp_path: Path,
) -> None:
    bundle = ScenarioBundle.from_path(SCENARIO)
    session = RealtimeAgentSession(
        _PublicMatchFactory(tmp_path / "factory", bundle),
        run_namespace="public-fire-flow",
        tick_interval_ms=1_000,
        clock=ManualRealtimeClock(),
    )
    reset = session.reset(seed=42)
    try:
        assert [
            value["operator_id"] for value in reset.red.observation["visible_enemies"]
        ] == [202, 203]
        assert [
            value["operator_id"] for value in reset.blue.observation["visible_enemies"]
        ] == [103]
        red_fire = session.operation_capabilities(reset.red_binding, [103])
        blue_fire = session.operation_capabilities(reset.blue_binding, [203])
        red_no_fire = session.operation_capabilities(reset.red_binding, [101])
        assert red_fire["operations"]["SHOOT"]["applicable_count"] == 1
        assert red_fire["shoot_target_ids"] == [202, 203]
        assert blue_fire["operations"]["SHOOT"]["applicable_count"] == 1
        assert blue_fire["shoot_target_ids"] == [103]
        assert red_fire["operations"]["WEAPON_LOCK"]["applicable_count"] == 1
        assert red_fire["operations"]["WEAPON_UNFOLD"]["unavailable"] == [{
            "unit_id": 103,
            "code": "weapon_already_unfolded",
            "message": "武器已经展开",
        }]
        assert red_no_fire["operations"]["SHOOT"]["unavailable"] == [{
            "unit_id": 101,
            "code": "no_visible_enemy",
            "message": "当前未发现可见敌军",
        }]

        sequences = [
            _submit_operation(
                session,
                reset.red_binding,
                SeatOperation("SHOOT", (103,), target_id=203),
                request_id="public-red-shoot",
            ),
            _submit_operation(
                session,
                reset.blue_binding,
                SeatOperation("WEAPON_LOCK", (201,)),
                request_id="public-blue-lock",
            ),
        ]
        session.advance_tick()
        sequences.append(_submit_operation(
            session,
            reset.red_binding,
            SeatOperation("MOVE", (101,), destination_hex=405),
            request_id="public-red-move",
        ))
        session.advance_tick()
        moving = session.operation_capabilities(reset.red_binding, [101])
        assert moving["operations"]["STOP_MOVE"]["applicable_count"] == 1
        assert moving["operations"]["WEAPON_LOCK"]["unavailable"] == [{
            "unit_id": 101,
            "code": "actor_must_be_stopped",
            "message": "单位移动中；停止后才能锁定武器",
        }]
        sequences.append(_submit_operation(
            session,
            reset.red_binding,
            SeatOperation("STOP_MOVE", (101,)),
            request_id="public-red-stop",
        ))
        session.advance_tick()

        events = {value.command_sequence: value for value in session.command_events}
        assert all(events[value].status is CommandStatus.APPLIED for value in sequences)
        assert [
            events[value].decision.operations[0].operation_type
            for value in sequences
        ] == ["SHOOT", "WEAPON_LOCK", "MOVE", "STOP_MOVE"]

        publication = session.publish(tmp_path / "public-fire", truncated=True)
        archive = SourceReplayArchiveImporter().import_path(publication.replay_path)
        assert any(
            "direct_fire_validated" in json.dumps(
                frame.judge_info, ensure_ascii=False, sort_keys=True,
            )
            for frame in archive.frames
        )
        assert replay_source_archive(
            bundle.scenario,
            publication.replay_path,
            strict=True,
            map_runtime_data=bundle.map_runtime_data,
        ).matches is True
    finally:
        session.close()


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
