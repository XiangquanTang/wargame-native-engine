"""Deterministic target-free game shot and ten-frame signal regression tests."""
import copy
import unittest
from unittest.mock import patch

from test_direct_fire_range import fixture_record
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.io.scenario import scenario_from_dict


class EmptyFireRulesTests(unittest.TestCase):
    def engine(self, record=None):
        engine = NativeEngine(scenario_from_dict(record or fixture_record()))
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        return engine

    def shot(self, engine, target_hex=106):
        return engine.direct_fire_intent_action('red', 1, 36, target_hex=target_hex)

    def test_occupied_and_unoccupied_cell_never_damage_or_draw_rng(self):
        for cell in (102, 106):
            with self.subTest(cell=cell):
                engine = self.engine()
                before = engine.snapshot()
                result = engine.step([self.shot(engine, cell)], delta_time_ms=1)
                after = engine.snapshot()
                self.assertTrue(result.decisions[0].accepted)
                self.assertEqual(before['rng'], after['rng'])
                # Observation arrays may change due to the firing signal.
                target = copy.deepcopy(after['world']['operators']['2'])
                target['raw_attributes']['see_enemy_bop_ids'] = before['world']['operators']['2']['raw_attributes']['see_enemy_bop_ids']
                self.assertEqual(target, before['world']['operators']['2'])
                self.assertEqual(after['world']['operators']['1']['raw_attributes']['ammunition']['0'], 19)
                self.assertGreater(after['world']['operators']['1']['weapon_cooldown_remaining_ms'], 0)
                self.assertFalse({EventType.DICE_ROLLED, EventType.LOSSES_APPLIED,
                                  EventType.SUPPRESSION_APPLIED} & {e.event_type for e in result.events})

    def test_signal_lasts_exactly_ten_result_frames_independent_of_delta(self):
        for delta in (1, 300, 1000, 1500):
            with self.subTest(delta=delta), patch(
                'wargame_native_engine.core.engine.within_maximum_observation_distance',
                return_value=False,
            ):
                engine = self.engine()
                self.assertFalse(engine.observe('blue').visible_enemies)
                posture = engine.snapshot()['world']['operators']['1'].get('concealment_posture')
                engine.step([self.shot(engine)], delta_time_ms=delta)
                for remaining in range(10, 0, -1):
                    self.assertEqual([v.operator_id for v in engine.observe('blue').visible_enemies], [1])
                    state = engine.snapshot()['world']['operators']['1']
                    self.assertEqual(state['raw_attributes']['empty_fire_exposure_frames_remaining'], remaining)
                    self.assertEqual(state.get('concealment_posture'), posture)
                    engine.step([], delta_time_ms=delta)
                self.assertFalse(engine.observe('blue').visible_enemies)
                self.assertNotIn('empty_fire_exposure_frames_remaining', engine.snapshot()['world']['operators']['1']['raw_attributes'])

    def test_normal_sensor_visibility_survives_signal_expiry(self):
        engine = self.engine()
        engine.step([self.shot(engine)], delta_time_ms=1)
        for _ in range(10):
            engine.step([], delta_time_ms=1)
        self.assertEqual([v.operator_id for v in engine.observe('blue').visible_enemies], [1])

    def test_targeted_overshoot_preserves_concealment_and_expires(self):
        record = fixture_record()
        record['operators'][0]['concealment_posture'] = 'CONCEALED'
        engine = self.engine(record)
        # The shooter can observe the target; hide only the reverse-side
        # ordinary sensor projection to isolate the temporary location signal.
        result = engine.step([EngineAction('red', 1, ActionType.SHOOT,
                            target_id=2, parameters={'weapon_id':36})],
                             delta_time_ms=1)
        self.assertTrue(result.decisions[0].accepted)
        self.assertTrue(engine._require_world().operators[1].concealed)
        with patch('wargame_native_engine.core.engine.within_maximum_observation_distance', return_value=False):
            engine._refresh_source_observation()
            self.assertEqual([v.operator_id for v in engine.observe('blue').visible_enemies], [1])
            for _ in range(10):
                engine.step([], delta_time_ms=1)
            self.assertFalse(engine.observe('blue').visible_enemies)
        self.assertTrue(engine._require_world().operators[1].concealed)

    def test_snapshot_restore_preserves_signal_remaining_and_results(self):
        with patch('wargame_native_engine.core.engine.within_maximum_observation_distance', return_value=False):
            original = self.engine()
            restored = self.engine()
            original.step([self.shot(original)], delta_time_ms=300)
            for _ in range(4):
                original.step([], delta_time_ms=300)
            restored.restore(original.snapshot())
            for _ in range(7):
                original.step([], delta_time_ms=300)
                restored.step([], delta_time_ms=300)
                self.assertEqual(original.snapshot(), restored.snapshot())

    def test_signal_is_private_and_invalid_counters_do_not_grant_visibility(self):
        with patch('wargame_native_engine.core.engine.within_maximum_observation_distance', return_value=False):
            engine = self.engine()
            engine.step([self.shot(engine)], delta_time_ms=1)
            visible = engine.observe('blue').to_dict()['visible_enemies'][0]
            for field in ('raw_attributes', 'ammunition', 'empty_fire_exposure_frames_remaining',
                          'weapon_cooldown_remaining_ms', 'health'):
                self.assertNotIn(field, visible)
            for invalid in (11, 1000000, -1, True, '10', 10.0):
                engine._require_world().operators[1].raw_attributes['empty_fire_exposure_frames_remaining'] = invalid
                engine._refresh_source_observation()
                self.assertFalse(engine.observe('blue').visible_enemies)
                engine.step([], delta_time_ms=1)
                self.assertNotIn('empty_fire_exposure_frames_remaining', engine.snapshot()['world']['operators']['1']['raw_attributes'])

    def test_invalid_resources_and_targets_reject_without_signal(self):
        cases = [('ammo', 'AMMUNITION_UNAVAILABLE'), ('cooldown', 'WEAPON_ON_COOLDOWN'),
                 ('unfolded', 'WEAPON_NOT_UNFOLDED'), ('moving', 'ATTACKER_MUST_STOP')]
        for kind, code in cases:
            with self.subTest(kind=kind):
                record = fixture_record()
                actor = record['operators'][0]
                if kind == 'ammo': actor['raw_attributes']['ammunition']['0'] = 0
                elif kind == 'cooldown': actor['weapon_cooldown_remaining_ms'] = 3000
                elif kind == 'unfolded': actor['weapon_unfolded'] = False
                else: actor['stopped'] = False
                engine = self.engine(record)
                result = engine.step([self.shot(engine)], delta_time_ms=1)
                self.assertEqual(result.decisions[0].code, code)
                self.assertNotIn('empty_fire_exposure_frames_remaining', engine.snapshot()['world']['operators']['1']['raw_attributes'])
        engine = self.engine()
        for kwargs, code in [({'target_hex': 999}, 'TARGET_HEX_NOT_FOUND'),
                             ({'target_hex': True}, 'TARGET_HEX_INVALID')]:
            action = EngineAction('red', 1, ActionType.SHOOT, parameters={'weapon_id': 36, **kwargs})
            self.assertEqual(engine.step([action], delta_time_ms=1).decisions[0].code, code)
        with self.assertRaisesRegex(ValueError, 'EXACTLY_ONE_FIRE_TARGET_REQUIRED'):
            engine.direct_fire_intent_action('red', 1, 36, 2, target_hex=106)
        action = EngineAction('red', 1, ActionType.SHOOT, target_id=2,
                              parameters={'weapon_id': 36, 'target_hex':106})
        self.assertEqual(engine.step([action], delta_time_ms=1).decisions[0].code, 'AMBIGUOUS_FIRE_TARGET')

if __name__ == '__main__':
    unittest.main()
