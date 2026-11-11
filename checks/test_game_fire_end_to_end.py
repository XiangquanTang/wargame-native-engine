"""Independent public-operation checks for target-free game fire.

Use the small published fixture here; full unchanged Map123 data is exercised
by verify_map123_fire.py with an explicitly supplied prepared runtime cache.
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from wargame_native_engine.agents.realtime import (
    CommandStatus, ManualRealtimeClock, RealtimeAgentSession,
)
from wargame_native_engine.agents.seat import SeatOperation
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.operations import ScenarioMatchFactory

ROOT = Path(__file__).resolve().parents[1]


def record(*, visible=False):
    value = json.loads((ROOT / 'examples/source_movement_scenario.json').read_text())
    if visible:
        for cell in value['map']['hexes']:
            cell.update(elev=0, cond=0)
    return value


class EmptyFireEndToEndTests(unittest.TestCase):
    def engine(self, *, visible=False):
        engine = NativeEngine(scenario_from_dict(record(visible=visible)))
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        return engine

    def test_occupied_cell_is_not_an_implicit_target_and_exposure_expires_by_frame(self):
        engine = self.engine()
        self.assertEqual(engine.observe('blue').visible_enemies, ())
        before = engine.snapshot()
        result = engine.step([EngineAction(
            'red', 1, ActionType.SHOOT,
            parameters={'weapon_id': 36, 'target_hex': 106},
        )], delta_time_ms=1)
        self.assertTrue(result.decisions[0].accepted, result.decisions)
        after = engine.snapshot()
        self.assertEqual(before['rng'], after['rng'])
        expected_target = copy.deepcopy(before['world']['operators']['2'])
        actual_target = copy.deepcopy(after['world']['operators']['2'])
        # Observation identity legitimately changes when the shooter exposes itself.
        for value in (expected_target, actual_target):
            value['raw_attributes'].pop('see_enemy_bop_ids', None)
        self.assertEqual(expected_target, actual_target)
        shooter = after['world']['operators']['1']
        self.assertEqual(shooter['raw_attributes']['ammunition']['0'], 19)
        self.assertGreater(shooter['weapon_cooldown_remaining_ms'], 0)
        self.assertEqual({c.operator_id for c in engine.observe('blue').visible_enemies}, {1})
        for _ in range(9):
            engine.step([], delta_time_ms=1)
            self.assertEqual({c.operator_id for c in engine.observe('blue').visible_enemies}, {1})
        engine.step([], delta_time_ms=1)
        self.assertEqual(engine.observe('blue').visible_enemies, ())
        # 11 one-millisecond steps have not advanced the whole-second world tick.
        self.assertEqual(engine.snapshot()['world']['tick'], 0)

    def test_expired_signal_does_not_hide_a_unit_detected_by_normal_sensors(self):
        engine = self.engine(visible=True)
        self.assertEqual({c.operator_id for c in engine.observe('blue').visible_enemies}, {1})
        result = engine.step([EngineAction(
            'red', 1, ActionType.SHOOT,
            parameters={'weapon_id': 36, 'target_hex': 103},
        )], delta_time_ms=1)
        self.assertTrue(result.decisions[0].accepted)
        for _ in range(10):
            engine.step([], delta_time_ms=1)
        self.assertEqual({c.operator_id for c in engine.observe('blue').visible_enemies}, {1})

    def test_public_operation_apply_and_strict_replay(self):
        scenario = scenario_from_dict(record())
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            session = RealtimeAgentSession(
                ScenarioMatchFactory(
                    bundle=ScenarioBundle.from_scenario(scenario),
                    storage_root=directory / 'storage',
                    red_controller_id='fire-red', blue_controller_id='fire-blue',
                    decision_interval_ms=1000, frame_interval_ms=1000, replay_mode='legacy-debug',
                ), run_namespace='empty-fire-qa', tick_interval_ms=1000,
                clock=ManualRealtimeClock(),
            )
            self.addCleanup(session.close)
            binding = session.reset(seed=42).red_binding
            version = session.latest_view(binding).observation_version
            operation = SeatOperation('SHOOT', actor_ids=(1,), target_hex=106,
                                      weapon_choices=((1, 36),))
            preview = session.preview_operation(binding, operation,
                observation_version=version, request_id='empty-occupied')
            self.assertEqual((preview.executable_count, preview.rejected_count), (1, 0))
            receipt = session.submit_operation(binding, operation=operation,
                                               observation_version=version)
            self.assertTrue(receipt.accepted)
            session.advance_tick()
            self.assertIs(session.command_event(binding, receipt.command_sequence).status,
                          CommandStatus.APPLIED)
            for _ in range(10):
                session.advance_tick()
            output = directory / 'published'
            session.publish(output)
            replay = replay_source_archive(scenario, output / 'replay.zip')
            self.assertTrue(replay.matches)

    def test_same_actor_empty_and_targeted_shots_conflict_without_double_consumption(self):
        empty = EngineAction('red', 1, ActionType.SHOOT,
                             parameters={'weapon_id': 36, 'target_hex': 106})
        targeted = EngineAction('red', 1, ActionType.SHOOT, target_id=2,
                                parameters={'weapon_id': 36})
        for actions in ((empty, targeted), (targeted, empty)):
            with self.subTest(first=actions[0].action_id):
                snapshots = []
                for _ in range(2):
                    engine = self.engine(visible=True)
                    result = engine.step(actions, delta_time_ms=1)
                    self.assertEqual([d.accepted for d in result.decisions], [True, False])
                    self.assertEqual(result.decisions[1].code, 'DUPLICATE_ACTOR_ACTION')
                    snapshot = engine.snapshot()
                    self.assertEqual(snapshot['world']['operators']['1']['raw_attributes']['ammunition']['0'], 19)
                    snapshots.append(snapshot)
                self.assertEqual(snapshots[0], snapshots[1])

    def test_target_id_and_target_hex_cannot_accidentally_mix(self):
        with self.assertRaises((ValueError, TypeError)):
            SeatOperation('SHOOT', actor_ids=(1,), target_hex=106,
                          target_id=2, weapon_choices=((1, 36),))


if __name__ == '__main__':
    unittest.main()
