"""Weapon-specific locks remain explicit through movement and snapshots."""
import copy
import unittest
from test_direct_fire_range import fixture_record, shoot
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.io.scenario import scenario_from_dict

class WeaponBindingTests(unittest.TestCase):
    def engine(self):
        record = fixture_record()
        profile = record['metadata']['source_compatible']['weapons']['36']
        profile['can_fire_while_moving'] = True
        record['metadata']['source_compatible']['weapons']['37'] = copy.deepcopy(profile)
        record['operators'][0]['weapon_ids'] = [36, 37]
        engine = NativeEngine(scenario_from_dict(record)); engine.reset(seed=42)
        self.addCleanup(engine.close)
        return engine

    def command(self, engine, kind, params=None, dt=1):
        return engine.step([EngineAction('red', 1, kind, parameters=params or {})],
                           delta_time_ms=dt).decisions[0]

    def test_lock_requires_exact_carried_id(self):
        for params in ({}, {'weapon_id': True}, {'weapon_id': '36'}, {'weapon_id': 999},
                       {'weapon_id': 36, 'other': 1}):
            engine = self.engine()
            self.assertFalse(self.command(engine, ActionType.WEAPON_LOCK, params).accepted)
            self.assertIsNone(engine._require_world().operators[1].locked_weapon_id)

    def test_binding_roundtrip_and_explicit_unlock(self):
        engine = self.engine()
        self.assertTrue(self.command(engine, ActionType.WEAPON_LOCK, {'weapon_id':36}).accepted)
        actor = engine._require_world().operators[1]
        self.assertEqual(actor.locked_weapon_id,36)
        clone = OperatorState.from_dict(actor.to_dict())
        self.assertEqual(clone.locked_weapon_id,36)
        self.assertEqual(clone.transition_kind,'WEAPON_LOCK')
        self.assertFalse(self.command(engine, ActionType.WEAPON_UNLOCK).accepted)
        engine.step([], delta_time_ms=engine._source_policy.transition_ms)
        self.assertFalse(self.command(engine, ActionType.WEAPON_LOCK, {'weapon_id':37}).accepted)
        self.assertEqual(self.command(engine, ActionType.WEAPON_UNFOLD).code,'WEAPON_UNLOCK_REQUIRED')
        self.assertTrue(self.command(engine, ActionType.WEAPON_UNLOCK).accepted)
        actor = engine._require_world().operators[1]
        self.assertFalse(actor.weapon_locked); self.assertIsNone(actor.locked_weapon_id)
        self.assertTrue(self.command(engine, ActionType.WEAPON_LOCK, {'weapon_id':37}).accepted)

    def test_moving_other_weapon_shot_and_unlock_rejected_without_spend(self):
        engine = self.engine()
        self.command(engine, ActionType.WEAPON_LOCK, {'weapon_id':36},
                     dt=engine._source_policy.transition_ms)
        self.assertTrue(self.command(engine, ActionType.MOVE,
            {'path':[102,103,104], 'movement_mode':'NORMAL'}).accepted)
        before = copy.deepcopy(engine._ammunition(engine._require_world().operators[1]))
        result = engine.step([shoot(weapon_id=37)],delta_time_ms=1)
        self.assertEqual(result.decisions[0].code,'WEAPON_LOCKED_TO_OTHER_WEAPON')
        self.assertEqual(engine._ammunition(engine._require_world().operators[1]), before)
        self.assertFalse(self.command(engine, ActionType.WEAPON_UNLOCK).accepted)
        engine.step([],delta_time_ms=120000)
        engine.step([],delta_time_ms=120000)
        self.assertFalse(self.command(engine,ActionType.WEAPON_LOCK,{'weapon_id':37}).accepted)
        self.assertTrue(self.command(engine,ActionType.WEAPON_UNLOCK).accepted)

    def test_legacy_unknown_lock_failclosed_but_unlockable(self):
        engine = self.engine(); actor=engine._require_world().operators[1]
        actor.weapon_locked=True
        result=engine.step([shoot()],delta_time_ms=1)
        self.assertEqual(result.decisions[0].code,'LOCKED_WEAPON_ID_REQUIRED')
        self.assertTrue(self.command(engine,ActionType.WEAPON_UNLOCK).accepted)

    def test_default_snapshot_wire_does_not_gain_null_lock_id(self):
        actor=self.engine()._require_world().operators[1]
        self.assertNotIn('locked_weapon_id',actor.to_dict())
        actor.locked_weapon_id=36; actor.current_hex=None
        self.assertEqual(OperatorState.from_dict(actor.to_dict()).locked_weapon_id,36)

if __name__=='__main__': unittest.main()
