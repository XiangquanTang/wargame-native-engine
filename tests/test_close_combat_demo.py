from __future__ import annotations

import argparse
from pathlib import Path

from examples.generate_close_combat_demo import build_records, write_records
from scripts.run_realtime_agent_match import _MatchFactory
from scripts.run_tactical_agent_demo import run_demo
from wargame_native_engine.agents import (
    CommandStatus,
    ManualRealtimeClock,
    RealtimeAgentSession,
    RealtimeCancellationReason,
    SeatDecision,
    SeatOperation,
)
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.viewer.map_config import load_viewer_map_config


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples/public_close_combat_scenario.json"
MAP_CONFIG = ROOT / "examples/public_close_combat_map.json"


def test_close_combat_public_records_are_reproducible_and_contact_ready(
    tmp_path: Path,
) -> None:
    scenario_path, map_path = write_records(tmp_path / "generated")
    assert scenario_path.read_bytes() == SCENARIO.read_bytes()
    assert map_path.read_bytes() == MAP_CONFIG.read_bytes()

    scenario, map_record = build_records()
    bundle = ScenarioBundle.from_path(SCENARIO)
    viewer_map = load_viewer_map_config(MAP_CONFIG)
    assert scenario["scenario_id"] == "public-close-combat-contact"
    assert map_record["map_id"] == "public-close-combat-24x18"
    assert len(bundle.scenario.operators) == 12
    assert len(bundle.scenario.map.hexes) == 432
    assert len(viewer_map.cells) == 432
    assert sum(value.side == "red" for value in bundle.scenario.operators) == 6
    assert sum(value.side == "blue" for value in bundle.scenario.operators) == 6

    session = RealtimeAgentSession(
        _MatchFactory(
            bundle, storage_root=tmp_path / "factory", tick_interval_ms=1_000
        ),
        run_namespace="close-combat-capabilities",
        tick_interval_ms=1_000,
        clock=ManualRealtimeClock(),
    )
    reset = session.reset(seed=42)
    try:
        assert reset.red.observation["visible_enemies"]
        assert reset.blue.observation["visible_enemies"]
        for view in (reset.red, reset.blue):
            action_types = {
                value["action"]["action_type"]
                for value in view.capabilities.frozen_actions
            }
            assert {"MOVE", "SHOOT", "WEAPON_LOCK"} <= action_types
    finally:
        session.close()


def test_close_combat_applies_move_lock_and_shoot_and_strictly_replays(
    tmp_path: Path,
) -> None:
    bundle = ScenarioBundle.from_path(SCENARIO)
    session = RealtimeAgentSession(
        _MatchFactory(
            bundle, storage_root=tmp_path / "factory", tick_interval_ms=1_000
        ),
        run_namespace="close-combat-product-flow",
        tick_interval_ms=1_000,
        clock=ManualRealtimeClock(),
    )
    reset = session.reset(seed=42)
    try:
        decision = SeatDecision((
            SeatOperation(
                "SHOOT",
                (101,),
                target_id=201,
                weapon_choices=((101, 36),),
            ),
            SeatOperation("WEAPON_LOCK", (102,)),
            SeatOperation("MOVE", (105,), destination_hex=810),
        ))
        receipt = session.submit(
            reset.red_binding,
            observation_version=reset.red.observation_version,
            decision=decision,
        )
        assert receipt.accepted is True
        tick = session.advance_tick()
        assert tick.red_command_sequences == (receipt.command_sequence,)
        event = next(
            value
            for value in session.command_events
            if value.command_sequence == receipt.command_sequence
        )
        assert event.status is CommandStatus.APPLIED
        assert [
            value.operation_type for value in event.decision.operations
        ] == ["SHOOT", "WEAPON_LOCK", "MOVE"]

        session.stop_accepting(RealtimeCancellationReason.RUNNER_LIMIT)
        session.stop_realtime(
            cancellation_reason=RealtimeCancellationReason.RUNNER_LIMIT
        )
        publication = session.publish(
            tmp_path / "match",
            truncated=True,
            cancellation_reason=RealtimeCancellationReason.RUNNER_LIMIT,
        )
        assert replay_source_archive(
            bundle.scenario,
            publication.replay_path,
            strict=True,
            map_runtime_data=bundle.map_runtime_data,
        ).matches is True
    finally:
        session.close()


def test_tactical_rule_agents_generate_fire_and_strict_replay(
    tmp_path: Path,
) -> None:
    report = run_demo(argparse.Namespace(
        scenario_bundle=SCENARIO,
        map_runtime_data=None,
        output_dir=tmp_path / "agent-match",
        run_namespace="close-combat-agent-test",
        seed=42,
        tick_interval_ms=50,
        max_ticks=4,
        timeout_seconds=30.0,
        shutdown_timeout_seconds=30.0,
        overwrite=False,
    ))

    assert report["tick_count"] == 4
    assert report["agent_failure_count"] == 0
    assert report["contact_count"] > 0
    assert report["shoot_count"] > 0
    assert report["strict_replay_matches"] is True
    assert Path(str(report["replay"])).is_file()
