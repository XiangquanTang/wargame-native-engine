"""Moving-fire intent crosses the same public boundary as UI and model tools.

Only the synthetic fixture opts into this capability here. Map123 profile
selection is separately explicit; third-party weapon metadata is not rewritten.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from wargame_native_engine.agents.realtime import (
    CommandStatus, ManualRealtimeClock, RealtimeAgentSession,
)
from wargame_native_engine.agents.seat import SeatOperation
from wargame_native_engine.agents.tool_calling import GameToolAdapter
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.operations import ScenarioMatchFactory
from wargame_native_engine.viewer.human_capabilities import build_human_capabilities

ROOT = Path(__file__).resolve().parents[1]


def scenario_record(*, locked=True, capable=True):
    record = json.loads((ROOT / 'examples/source_movement_scenario.json').read_text())
    for cell in record['map']['hexes']:
        cell.update(elev=0, cond=0)
    record['operators'][0].update(weapon_locked=locked, weapon_unfolded=not locked,
                                   locked_weapon_id=36 if locked else None)
    weapon = record['metadata']['source_compatible']['weapons']['36']
    weapon['can_fire_while_moving'] = capable
    return record


class MovingFireInterfaceTests(unittest.TestCase):
    def make_session(self, directory, *, locked=True, capable=True, replay_mode='compact'):
        scenario = scenario_from_dict(scenario_record(locked=locked, capable=capable))
        session = RealtimeAgentSession(
            ScenarioMatchFactory(
                bundle=ScenarioBundle.from_scenario(scenario),
                storage_root=directory / 'storage',
                red_controller_id='moving-red', blue_controller_id='moving-blue',
                decision_interval_ms=1000, frame_interval_ms=1000, replay_mode=replay_mode,
            ), run_namespace='moving-fire-interfaces', tick_interval_ms=1000,
            clock=ManualRealtimeClock(),
        )
        self.addCleanup(session.close)
        binding = session.reset(seed=42).red_binding
        return scenario, session, binding

    def begin_move(self, session, binding):
        operation = SeatOperation('MOVE', actor_ids=(1,), destination_hex=105)
        version = session.latest_view(binding).observation_version
        preview = session.preview_operation(binding, operation,
            observation_version=version, request_id='begin-move')
        self.assertTrue(preview.can_execute, preview)
        receipt = session.submit_operation(binding, operation=operation,
                                           observation_version=version)
        self.assertTrue(receipt.accepted, receipt)
        session.advance_tick()
        self.assertIs(session.command_event(binding, receipt.command_sequence).status,
                      CommandStatus.APPLIED)
        return receipt

    def actor(self, session, binding):
        return session.latest_view(binding).observation['friendly_operators'][0]

    def tool_preview(self, adapter, *, target_hex=None, target_id=None):
        version = adapter.dispatch('game_observation', {})['observation_version']
        operation = {'operation_type': 'SHOOT', 'actor_ids': [1],
                     'weapon_choices': [[1, 36]]}
        operation.update({'target_hex': target_hex} if target_hex is not None
                         else {'target_id': target_id})
        return adapter.dispatch('preview_game_operation', {
            'observation_version': version, 'operation': operation,
            'request_id': f'shot-{version}',
        })['preview']

    def test_tool_empty_shot_applies_without_cancelling_move_and_replays(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario, session, binding = self.make_session(root / 'firing', replay_mode='legacy-debug')
            _, control, control_binding = self.make_session(root / 'control')
            self.begin_move(session, binding)
            self.begin_move(control, control_binding)
            adapter = GameToolAdapter(session, binding)
            capabilities = adapter.dispatch('game_capabilities', {'actor_ids': [1]})
            self.assertEqual(capabilities['capabilities']['operations']['SHOOT']['applicable_count'], 1)
            self.assertIn(2, capabilities['capabilities']['shoot_target_ids'])
            preview = self.tool_preview(adapter, target_hex=106)
            self.assertTrue(preview['can_execute'], preview)
            receipt = adapter.dispatch('submit_game_operation', {
                'preview_token': preview['preview_token'], 'request_id': 'submit-shot',
                'confirm_partial': False,
            })
            self.assertTrue(receipt['accepted'], receipt)
            # Both runs advance identically; only one fires. Mid-segment progress
            # and the next cell must remain equal, not merely state == MOVING.
            for _ in range(25):
                session.advance_tick()
                control.advance_tick()
                actual = self.actor(session, binding)
                expected = self.actor(control, control_binding)
                for field in ('position', 'speed', 'state', 'stopped',
                              'has_active_segment', 'remaining_path_length',
                              'has_remaining_movement_task', 'movement_task_pause_reason'):
                    self.assertEqual(actual[field], expected[field], field)
            self.assertNotEqual(self.actor(session, binding)['position'], [1.0, 1.0])
            command = adapter.dispatch('game_command_result', {
                'command_sequence': receipt['command_sequence'],
            })
            self.assertEqual(command['command']['status'], 'APPLIED')
            capabilities = adapter.dispatch('game_capabilities', {'actor_ids': [1]})
            _, view, summary = session.operation_capabilities_snapshot(binding, (1,))
            human = build_human_capabilities(summary, view, (1,))
            weapon = human['direct_fire']['units'][0]['weapons'][0]
            self.assertEqual(weapon['ammunition'], 19)
            session.publish(root / 'published')
            replay = replay_source_archive(scenario, root / 'published/replay.zip')
            self.assertTrue(replay.matches, replay)

    def test_targeted_tool_preview_matches_locked_moving_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, session, binding = self.make_session(Path(temporary))
            self.begin_move(session, binding)
            preview = self.tool_preview(GameToolAdapter(session, binding), target_id=2)
            self.assertTrue(preview['can_execute'], preview)

    def test_moving_unlocked_or_unsupported_intent_rejects_at_apply(self):
        for locked, capable, code in (
            (False, True, 'WEAPON_NOT_LOCKED'),
            (False, False, 'ATTACKER_MUST_STOP'),
        ):
            with self.subTest(locked=locked, capable=capable), tempfile.TemporaryDirectory() as temporary:
                _, session, binding = self.make_session(Path(temporary),
                    locked=locked, capable=capable)
                self.begin_move(session, binding)
                adapter = GameToolAdapter(session, binding)
                preview = self.tool_preview(adapter, target_hex=106)
                # Intent preview authenticates ownership/target, not readiness.
                # Admission must never be confused with a successful shot.
                self.assertTrue(preview['can_execute'], preview)
                receipt = adapter.dispatch('submit_game_operation', {
                    'preview_token': preview['preview_token'], 'request_id': 'rejected-shot',
                    'confirm_partial': False,
                })
                self.assertTrue(receipt['accepted'])
                session.advance_tick()
                outcome = adapter.dispatch('game_command_result', {
                    'command_sequence': receipt['command_sequence'],
                })
                self.assertEqual(outcome['command']['status'], 'REJECTED_AT_APPLY')
                self.assertEqual(outcome['command']['error_code'], code)
                self.assertEqual(outcome['executions'][0]['execution_status'], 'NOT_STARTED')
                _, view, summary = session.operation_capabilities_snapshot(binding, (1,))
                human = build_human_capabilities(summary, view, (1,))
                self.assertEqual(human['direct_fire']['units'][0]['weapons'][0]['ammunition'], 20)
                self.assertTrue(self.actor(session, binding)['has_remaining_movement_task'])


if __name__ == '__main__':
    unittest.main()
