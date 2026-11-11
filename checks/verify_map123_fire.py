"""Verify original Map123 ammo rejection and labelled ammo2 fire lifecycle.

Usage: PYTHONPATH=src python checks/verify_map123_fire.py CACHE_DIR OUTPUT_DIR
No source scenario or expanded lookup files are modified.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from wargame_native_engine.agents.realtime import (
    CommandStatus, ManualRealtimeClock, RealtimeAgentSession,
)
from wargame_native_engine.agents.seat import SeatOperation
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.operations import ScenarioMatchFactory

ROOT = Path(__file__).resolve().parents[1]


def verify(cache: Path, output: Path) -> dict:
    original = ScenarioBundle.from_path(ROOT / 'examples/map123/scenario.json')
    original = original.with_map_runtime_data(cache)
    engine = NativeEngine(original.scenario, map_runtime_data=original.map_runtime_data)
    world = engine.reset(seed=42)
    initial_artillery = []
    for side in ('red', 'blue'):
        for capability in engine.get_indirect_fire_capabilities(side):
            operator = world.operators[capability.shooter_id]
            assert operator.raw_attributes['ammunition']['0'] == 0
            validation = engine.build_indirect_fire_plan_request(capability, 5000)
            assert not validation.valid, validation
            assert validation.reason == 'INDIRECT_FIRE_AMMUNITION_UNAVAILABLE', validation
            initial_artillery.append(capability.shooter_id)
    assert len(initial_artillery) == 12
    engine.close()
    original_session = RealtimeAgentSession(ScenarioMatchFactory(
        bundle=original, storage_root=output / 'original-storage',
        red_controller_id='qa-original-red', blue_controller_id='qa-original-blue',
        decision_interval_ms=1000, frame_interval_ms=1000,
    ), run_namespace='map123-original-rejection', tick_interval_ms=1000,
       clock=ManualRealtimeClock())
    try:
        binding = original_session.reset(seed=42).red_binding
        version = original_session.latest_view(binding).observation_version
        operation = SeatOperation('INDIRECT_FIRE', actor_ids=(47,),
            target_hex=4306, fire_mode='INDIRECT', shooter_id=47, weapon_id=72)
        preview = original_session.preview_operation(binding, operation,
            observation_version=version, request_id='original-empty-ammo')
        assert not preview.can_execute, preview
        assert preview.rejected_count == 1, preview
        rejection = preview.per_unit_results[0]
        assert 'ammunition_unavailable' in (rejection.reject_message or '').lower(), preview
    finally:
        original_session.close()

    # The existing, explicitly labelled demonstration variant grants two rounds.
    demo = ScenarioBundle.from_path(ROOT / 'examples/map123/scenario-artillery-ammo2.json')
    demo = demo.with_map_runtime_data(cache)
    session = RealtimeAgentSession(ScenarioMatchFactory(
        bundle=demo, storage_root=output / 'storage',
        red_controller_id='qa-red', blue_controller_id='qa-blue',
        decision_interval_ms=150000, frame_interval_ms=150000,
    ), run_namespace='map123-fire-verification', tick_interval_ms=150000,
       clock=ManualRealtimeClock())
    try:
        binding = session.reset(seed=42).red_binding
        version = session.latest_view(binding).observation_version
        operation = SeatOperation('INDIRECT_FIRE', actor_ids=(47,),
            target_hex=4306, fire_mode='INDIRECT', shooter_id=47, weapon_id=72)
        preview = session.preview_operation(binding, operation,
            observation_version=version, request_id='indirect-empty-occupied')
        assert preview.can_execute, preview
        receipt = session.submit_operation(binding, operation=operation,
            observation_version=version)
        assert receipt.accepted, receipt
        session.advance_tick()
        command = session.command_event(binding, receipt.command_sequence)
        assert command.status is CommandStatus.APPLIED, command
        session.advance_tick()
        session.advance_tick()
        session.publish(output / 'published')
        replay = replay_source_archive(demo.scenario, output / 'published/replay.zip',
                                       map_runtime_data=demo.map_runtime_data)
        assert replay.matches, replay
    finally:
        session.close()

    # Verify authoritative resource/state outcomes without publication projections.
    engine = NativeEngine(demo.scenario, map_runtime_data=demo.map_runtime_data)
    try:
        world = engine.reset(seed=42)
        before_rng = engine.snapshot()['rng']
        before_health = {key: (o.health, o.blood, o.suppression_remaining_ms)
                         for key, o in world.operators.items()}
        capability = next(c for c in engine.get_indirect_fire_capabilities('red')
                          if c.shooter_id == 47)
        # Occupied blue artillery coordinate must NOT acquire a target implicitly.
        action = engine.indirect_fire_action(capability, 4306)
        result = engine.step([action], delta_time_ms=1000)
        assert result.decisions[0].accepted, result.decisions
        state = engine.snapshot()['world']
        assert state['operators']['47']['raw_attributes']['ammunition']['0'] == 1
        assert state['operators']['47']['indirect_fire_planned_cooldown_remaining_ms'] == 299000
        assert state['indirect_fire_missions'][0]['phase'] == 'FLYING'
        result = engine.step([], delta_time_ms=149000)
        state = engine.snapshot()['world']
        assert state['indirect_fire_missions'][0]['phase'] == 'RESOLVED'
        assert any(e.event_type.value == 'indirect_fire_resolved'
                   and e.data.get('outcome') == 'EMPTY_FIRE' for e in result.events)
        assert engine.snapshot()['rng'] == before_rng
        assert {int(key): (o['health'], o['blood'], o['suppression_remaining_ms'])
                for key, o in state['operators'].items()} == before_health
        engine.step([], delta_time_ms=150000)
        assert engine.snapshot()['world']['operators']['47'].get('indirect_fire_planned_cooldown_remaining_ms', 0) == 0
    finally:
        engine.close()
    return {'original_zero_ammo_artillery_ids': sorted(initial_artillery),
            'original_data_preserved': True, 'original_preview_rejects_zero_ammo': True, 'demo_variant': 'artillery-ammo2',
            'preview_submit_apply': True, 'flight_resolved_ms': 150000,
            'cooldown_finished_ms': 300000, 'empty_occupied_hex_no_damage': True,
            'empty_fire_rng_unchanged': True, 'strict_replay_matches': replay.matches,
            'replay_ticks': replay.ticks}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cache', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    print(json.dumps(verify(args.cache, args.output), ensure_ascii=False, indent=2))
