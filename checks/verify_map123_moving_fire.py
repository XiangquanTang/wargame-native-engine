"""Public Map123 lock -> MOVE -> SHOOT smoke and strict replay.

Run with prepared Map123 CACHE_DIR and a new OUTPUT_DIR. Does not modify map
files, initial posture, ammo, or scenario data. The release's explicit ordinary
weapon capability is exercised using the normal 75-second lock transition.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from wargame_native_engine.agents.realtime import ManualRealtimeClock, RealtimeAgentSession
from wargame_native_engine.agents.seat import SeatOperation
from wargame_native_engine.agents.tool_calling import GameToolAdapter
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.operations import ScenarioMatchFactory
from wargame_native_engine.viewer.human_capabilities import build_human_capabilities

ROOT = Path(__file__).resolve().parents[1]


def verify(cache: Path, output: Path) -> dict:
    bundle = ScenarioBundle.from_path(ROOT / 'examples/map123/scenario.json').with_map_runtime_data(cache)
    session = RealtimeAgentSession(ScenarioMatchFactory(
        bundle=bundle, storage_root=output / 'storage',
        red_controller_id='map123-moving-red', blue_controller_id='map123-moving-blue',
        decision_interval_ms=10000, frame_interval_ms=10000,
    ), run_namespace='map123-moving-fire', tick_interval_ms=10000,
        clock=ManualRealtimeClock())
    try:
        binding = session.reset(seed=42).red_binding
        adapter = GameToolAdapter(session, binding)

        def actor():
            return next(o for o in session.latest_view(binding).observation['friendly_operators']
                        if o['operator_id'] == 27)

        def weapon():
            _, view, summary = session.operation_capabilities_snapshot(binding, (27,))
            human = build_human_capabilities(summary, view, (27,))
            return next(w for w in human['direct_fire']['units'][0]['weapons'] if w['weapon_id'] == 36)

        def submit(operation, request):
            version = session.latest_view(binding).observation_version
            preview = adapter.dispatch('preview_game_operation', {
                'observation_version': version, 'operation': operation, 'request_id': request,
            })['preview']
            assert preview['can_execute'], preview
            receipt = adapter.dispatch('submit_game_operation', {
                'preview_token': preview['preview_token'], 'request_id': request + '-submit',
                'confirm_partial': False,
            })
            assert receipt['accepted'], receipt
            session.advance_tick()
            result = adapter.dispatch('game_command_result', {'command_sequence': receipt['command_sequence']})
            assert result['command']['status'] == 'APPLIED', result
            return result

        ammo_before = weapon()['ammunition']
        submit({'operation_type': 'WEAPON_LOCK', 'actor_ids': [27], 'weapon_choices': [[27, 36]]}, 'lock')
        lock_ticks = 1
        while actor()['transition_kind'] is not None:
            assert lock_ticks < 12, actor()
            session.advance_tick()
            lock_ticks += 1
        assert lock_ticks == 8, lock_ticks
        selected = None
        for destination in (2745, 2746, 2645, 2845, 2442, 3042):
            candidate = SeatOperation('MOVE', actor_ids=(27,), destination_hex=destination)
            preview = session.preview_operation(binding, candidate,
                observation_version=session.latest_view(binding).observation_version,
                request_id=f'path-{destination}')
            if preview.can_execute and len(preview.per_unit_results[0].path) >= 3:
                selected = destination
                break
        assert selected is not None, 'No usable three-segment path from standard Map123 spawn'
        submit({'operation_type': 'MOVE', 'actor_ids': [27], 'destination_hex': selected}, 'move')
        assert actor()['has_active_segment'] and actor()['has_remaining_movement_task'], actor()
        before_shot = actor()
        submit({'operation_type': 'SHOOT', 'actor_ids': [27], 'target_hex': 4306,
                'weapon_choices': [[27, 36]]}, 'moving-empty-shot')
        after_shot = actor()
        assert after_shot['has_active_segment'] and after_shot['has_remaining_movement_task'], after_shot
        assert not after_shot['stopped'], after_shot
        ammo_after = weapon()['ammunition']
        assert ammo_after == ammo_before - 1, ammo_after
        session.publish(output / 'published')
        replay = replay_source_archive(bundle.scenario, output / 'published/replay.zip',
                                       map_runtime_data=bundle.map_runtime_data)
        assert replay.matches, replay
        return {
            'scenario': 'original Map123 with explicit moving-fire weapon profiles',
            'actor_id': 27, 'weapon_id': 36, 'destination_hex': selected,
            'lock_ready_observed_ms': lock_ticks * 10000,
            'lock_move_shoot_public_tool_status': 'APPLIED',
            'ammunition_before': ammo_before, 'ammunition_after': ammo_after,
            'movement_active_before_and_after_shot': True,
            'position_before_shot': before_shot['position'],
            'position_after_shot': after_shot['position'],
            'strict_replay_matches': replay.matches, 'replay_ticks': replay.ticks,
        }
    finally:
        session.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cache', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    print(json.dumps(verify(args.cache, args.output), ensure_ascii=False, indent=2))
