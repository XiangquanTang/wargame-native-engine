"""Travelling direct fire uses a locked weapon without cancelling its route."""
import unittest

from test_direct_fire_range import fixture_record, shoot, TABLES, ROOT
from wargame_native_engine.adjudication import AdjudicationTableSet, DirectFireAdjudicator, FixedDiceRoller
from wargame_native_engine.adjudication.replay import direct_fire_request_from_dict, direct_fire_request_to_dict
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.fire_intent import FireMode
from wargame_native_engine.io.scenario import scenario_from_dict, load_scenario


class LockedMovingFireTests(unittest.TestCase):
    def engine(self, *, capability=True, in_range=False):
        record = fixture_record()
        weapon = record['metadata']['source_compatible']['weapons']['36']
        weapon['can_fire_while_moving'] = capability
        if in_range:
            weapon['maximum_range'] = 10
        engine = NativeEngine(scenario_from_dict(record))
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        return engine

    def action(self, *, empty=False):
        if empty:
            return EngineAction('red', 1, ActionType.SHOOT,
                                parameters={'weapon_id': 36, 'target_hex': 106})
        return shoot()

    def lock_and_move(self, engine, mode='NORMAL'):
        locked = engine.step([EngineAction('red', 1, ActionType.WEAPON_LOCK, parameters={'weapon_id': 36})],
                             delta_time_ms=engine._source_policy.transition_ms)
        self.assertTrue(locked.decisions[0].accepted)
        actor = engine._require_world().operators[1]
        self.assertTrue(actor.weapon_locked)
        self.assertFalse(actor.weapon_unfolded)
        moved = engine.step([EngineAction('red', 1, ActionType.MOVE,
            parameters={'path': [102, 103, 104], 'movement_mode': mode})],
            delta_time_ms=1)
        self.assertTrue(moved.decisions[0].accepted, moved.decisions)
        self.assertTrue(engine._require_world().operators[1].moving)

    def test_targeted_and_empty_fire_preserve_route_progress_and_resources(self):
        for empty in (False, True):
            for mode in ('NORMAL', 'MARCHING'):
                with self.subTest(empty=empty, mode=mode):
                    engine = self.engine(capability=False)
                    self.lock_and_move(engine, mode)
                    before = engine.snapshot()['world']['operators']['1']
                    rng = engine.snapshot()['rng']
                    result = engine.step([self.action(empty=empty)], delta_time_ms=1)
                    self.assertTrue(result.decisions[0].accepted, result.decisions)
                    after = engine.snapshot()['world']['operators']['1']
                    for key in ('move_path', 'movement_action_id', 'movement_mode',
                                'segment_to_hex', 'segment_traversal_ms'):
                        self.assertEqual(before[key], after[key], key)
                    self.assertGreater(after['segment_elapsed_ms'], before['segment_elapsed_ms'])
                    self.assertFalse(after['stopped'])
                    self.assertEqual(after['raw_attributes']['ammunition']['0'], 19)
                    self.assertGreater(after['weapon_cooldown_remaining_ms'], 0)
                    self.assertEqual(after['raw_attributes']['empty_fire_exposure_frames_remaining'], 10)
                    self.assertEqual(engine.snapshot()['rng'], rng)
                    engine.step([], delta_time_ms=120000)
                    self.assertEqual(engine._require_world().operators[1].current_hex, 104)
                    self.assertFalse(engine._require_world().operators[1].moving)
                    self.assertEqual(engine._require_world().operators[1].move_path, [])

    def test_moving_unlocked_and_unsupported_reject(self):
        for empty in (False, True):
            for capability, locked, expected in (
                (True, False, 'WEAPON_NOT_LOCKED'),
                (False, False, 'ATTACKER_MUST_STOP'),
            ):
                with self.subTest(empty=empty, capability=capability):
                    engine = self.engine(capability=capability)
                    actor = engine._require_world().operators[1]
                    actor.stopped = False
                    actor.weapon_locked = locked
                    actor.locked_weapon_id = 36 if locked else None
                    actor.weapon_unfolded = not locked
                    result = engine.step([self.action(empty=empty)], delta_time_ms=1)
                    self.assertEqual(result.decisions[0].code, expected)
                    self.assertEqual(engine._require_world().operators[1].raw_attributes['ammunition']['0'], 20)

    def test_stationary_locked_is_ready_without_unfolding(self):
        for empty in (False, True):
            engine = self.engine()
            actor = engine._require_world().operators[1]
            actor.weapon_locked = True
            actor.locked_weapon_id = 36
            actor.weapon_unfolded = False
            result = engine.step([self.action(empty=empty)], delta_time_ms=1)
            self.assertTrue(result.decisions[0].accepted, result.decisions)

    def test_cooldown_ammunition_transition_and_air_guards_remain(self):
        for empty in (False, True):
            for kind, expected in (('ammo', 'AMMUNITION_UNAVAILABLE'),
                ('cooldown', 'WEAPON_ON_COOLDOWN'), ('transition', 'ATTACKER_TRANSITIONING'),
                ('air', 'AIR_UNIT_MUST_HOVER')):
                with self.subTest(empty=empty, kind=kind):
                    engine = self.engine()
                    actor = engine._require_world().operators[1]
                    actor.stopped, actor.weapon_locked, actor.weapon_unfolded = False, True, False
                    actor.locked_weapon_id = 36
                    if kind == 'ammo': actor.raw_attributes['ammunition']['0'] = 0
                    elif kind == 'cooldown': actor.weapon_cooldown_remaining_ms = 1000
                    elif kind == 'transition':
                        actor.transition_kind, actor.transition_remaining_ms = 'WEAPON_LOCK', 1000
                    else: actor.operator_type = 'AIR'
                    result = engine.step([self.action(empty=empty)], delta_time_ms=1)
                    self.assertEqual(result.decisions[0].code, expected)

    def test_request_roundtrip_preserves_lock_without_broadening_other_modes(self):
        engine = self.engine(in_range=True)
        self.lock_and_move(engine)
        world = engine._require_world()
        request = engine._direct_fire_request(shoot(), world.operators[1], world.operators[2])
        restored = direct_fire_request_from_dict(direct_fire_request_to_dict(request))
        self.assertTrue(restored.weapon_locked)
        adjudicator = DirectFireAdjudicator(AdjudicationTableSet(TABLES), FixedDiceRoller([1]))
        self.assertTrue(adjudicator.validate(restored).legal)
        restored.fire_mode = FireMode.GUIDED
        self.assertEqual(adjudicator.validate(restored).reason_code, 'WEAPON_NOT_UNFOLDED')
        old = direct_fire_request_to_dict(request)
        old.pop('weapon_locked')
        self.assertFalse(direct_fire_request_from_dict(old).weapon_locked)

    def test_moving_self_as_munition_keeps_existing_route_interrupt_rule(self):
        engine = NativeEngine(load_scenario(
            ROOT / 'examples/maps/scenarios/loitering-munition.json'))
        self.addCleanup(engine.close)
        engine.reset(seed=42)
        actor = engine._require_world().operators[1]
        actor.current_hex = 102
        actor.x = 2
        actor.stopped = False
        self.assertFalse(actor.weapon_locked)
        result = engine.step([shoot(weapon_id=76)], delta_time_ms=1)
        self.assertTrue(result.decisions[0].accepted, result.decisions)
        # Existing loitering policy consumes the attacking unit, not bullets.
        self.assertFalse(engine._require_world().operators[1].alive)

    def test_in_range_shot_runs_existing_combat_and_keeps_moving(self):
        engine = self.engine(in_range=True)
        self.lock_and_move(engine)
        result = engine.step([shoot()], delta_time_ms=1)
        self.assertTrue(result.decisions[0].accepted, result.decisions)
        self.assertTrue(engine._require_world().operators[1].moving)
        self.assertIn('dice_rolled', {event.event_type.value for event in result.events})


if __name__ == '__main__':
    unittest.main()
