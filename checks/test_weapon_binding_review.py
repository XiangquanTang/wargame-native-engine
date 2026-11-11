"""Independent cross-boundary regression coverage for explicit weapon binding."""
from copy import deepcopy
import unittest

from checks.test_fire_effect_query import fixture, guided_fixture, PROFILE
from wargame_native_engine.adjudication.fire_effect_query import (
    FireEffectQuery, FireEffectQueryError,
)
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.projection.operator_lifecycle import project_operator_lifecycle
from wargame_native_engine.rules.objective_capture import ObjectiveCaptureService, load_objective_capture_policy


class WeaponBindingIndependentReviewTests(unittest.TestCase):
    def engine(self, record=None):
        engine = NativeEngine(scenario_from_dict(fixture() if record is None else record))
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        return engine

    def lock(self, engine, weapon_id=36, duration=None):
        result = engine.step([EngineAction('red', 1, ActionType.WEAPON_LOCK,
            parameters={'weapon_id': weapon_id})],
            delta_time_ms=engine._source_policy.transition_ms if duration is None else duration)
        self.assertTrue(result.decisions[0].accepted, result.decisions)

    def projection(self, engine):
        observation = engine.observe('red').to_dict()
        visible = {item['operator_id'] for item in observation['visible_enemies']}
        source = {'operators': [
            {'obj_id': actor.operator_id, 'cur_hex': actor.current_hex,
             **({'carry_weapon_ids': list(actor.weapon_ids)} if actor.side == 'red' else {})}
            for actor in engine._require_world().operators.values()
            if actor.side == 'red' or actor.operator_id in visible
        ]}
        return observation, source, [item.to_dict() for item in engine.valid_actions('red')], engine.own_weapon_inventory('red')

    def test_indirect_cannot_switch_bound_weapon_even_with_stale_prelock_action(self):
        record = fixture()
        record['operators'][0]['weapon_ids'] = [36, 72]
        engine = self.engine(record)
        capability = engine.get_indirect_fire_capabilities('red')[0]
        self.assertEqual(capability.status.value, 'AVAILABLE')
        stale_action = engine.indirect_fire_action(capability, 106)
        self.lock(engine)
        self.assertEqual(engine.get_indirect_fire_capabilities('red')[0].status.value, 'BLOCKED')
        before = deepcopy(engine._require_world().operators[1].raw_attributes['ammunition'])
        result = engine.step([stale_action], delta_time_ms=1)
        self.assertFalse(result.decisions[0].accepted, result.decisions)
        self.assertEqual(engine._require_world().operators[1].raw_attributes['ammunition'], before)
        self.assertFalse(engine._require_world().indirect_fire_missions)

    def test_guided_legal_catalog_cannot_switch_bound_weapon(self):
        record = guided_fixture()
        record['operators'][0]['weapon_ids'] = [36, 73]
        # Isolate binding from the independent unfold requirement.
        record['metadata']['source_compatible']['weapons']['73']['requires_unfolded'] = False
        engine = self.engine(record)
        self.assertTrue(engine.get_guided_fire_capabilities('red'))
        self.lock(engine)
        capabilities = engine.get_guided_fire_capabilities('red')
        self.assertFalse([item for item in capabilities if item.shooter_id == 1 and item.weapon_id == 73])

    def test_unlock_does_not_project_a_corrupted_weapon_posture(self):
        engine = self.engine()
        self.lock(engine)
        result = engine.step([EngineAction('red', 1, ActionType.WEAPON_UNLOCK)], delta_time_ms=1)
        self.assertTrue(result.decisions[0].accepted, result.decisions)
        actor = engine._require_world().operators[1]
        self.assertIsNone(actor.locked_weapon_id)
        self.assertFalse(actor.weapon_locked)
        view = project_operator_lifecycle(engine._require_world(), 1)
        self.assertNotEqual(view.weapon_lifecycle.posture.value, 'INCONSISTENT')
        self.assertNotIn('WEAPON_POSTURE_INCONSISTENT', view.integrity.codes)

    def test_lock_id_roundtrips_mid_transition_and_off_map(self):
        engine = self.engine()
        self.lock(engine, duration=1)
        snapshot = engine.snapshot()
        self.assertEqual(snapshot['world']['operators']['1']['locked_weapon_id'], 36)
        engine.restore(snapshot)
        actor = engine._require_world().operators[1]
        self.assertEqual(actor.locked_weapon_id, 36)
        self.assertFalse(actor.weapon_locked)
        self.assertEqual(actor.transition_kind, 'WEAPON_LOCK')
        clone = WorldState.from_dict(engine._require_world().to_dict())
        clone.operators[1].current_hex = None
        self.assertEqual(OperatorState.from_dict(clone.operators[1].to_dict()).locked_weapon_id, 36)

    def test_default_lock_id_does_not_rewrite_legacy_scenario_serialization(self):
        engine = self.engine()
        self.assertNotIn('locked_weapon_id', engine.scenario.operators[0].to_dict())
        for actor in engine.scenario.operators:
            restored = OperatorState.from_dict(actor.to_dict())
            self.assertEqual(restored.to_dict(), actor.to_dict())

    def test_malformed_frozen_inventory_lock_ids_fail_closed(self):
        engine = self.engine()
        query = FireEffectQuery(1, 36, target_id=2, target_profile=PROFILE)
        for malformed in (True, '36', 36.0, -1, [], {}):
            with self.subTest(value=malformed):
                projection = list(self.projection(engine))
                inventory = deepcopy(projection[3])
                inventory[0]['locked_weapon_id'] = malformed
                projection[3] = inventory
                with self.assertRaises(FireEffectQueryError):
                    engine.query_fire_effect_from_projection('red', *projection, query=query)

    def test_wrong_weapon_same_pool_rejected_for_targeted_and_empty_shots(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                record = fixture()
                record['operators'][0]['weapon_ids'] = [36, 54]
                weapons = record['metadata']['source_compatible']['weapons']
                weapons['36']['can_fire_while_moving'] = True
                weapons['54'] = deepcopy(weapons['36'])
                engine = self.engine(record)
                self.lock(engine)
                move = EngineAction('red', 1, ActionType.MOVE,
                    parameters={'path': [102, 103, 104], 'movement_mode': 'NORMAL'})
                self.assertTrue(engine.step([move], delta_time_ms=1).decisions[0].accepted)
                before = deepcopy(engine._require_world().operators[1].to_dict())
                parameters = {'weapon_id': 54, **({'target_hex': 106} if empty else {})}
                shot = EngineAction('red', 1, ActionType.SHOOT,
                    target_id=None if empty else 2, parameters=parameters)
                result = engine.step([shot], delta_time_ms=1)
                self.assertFalse(result.decisions[0].accepted)
                self.assertEqual(result.decisions[0].code, 'WEAPON_LOCKED_TO_OTHER_WEAPON')
                after = engine._require_world().operators[1].to_dict()
                self.assertEqual(before['raw_attributes']['ammunition'], after['raw_attributes']['ammunition'])
                self.assertEqual(after['weapon_cooldown_remaining_ms'], 0)
                self.assertEqual(after['locked_weapon_id'], 36)
                self.assertGreater(after['segment_elapsed_ms'], before['segment_elapsed_ms'])

    def capture_engine(self):
        record = fixture()
        record['objectives'] = [{'objective_id': 1, 'x': 2, 'y': 1,
            'radius': 0.25, 'value': 1, 'controlling_side': None}]
        return self.engine(record)

    def test_capture_readiness_is_actor_only_and_old_policy_is_frozen(self):
        engine = self.capture_engine()
        world = engine._require_world()
        service = engine._capture_service
        actor = world.operators[1]
        kwargs = {'side': 'red', 'actor_id': 1, 'objective_id': 1}
        self.assertTrue(service.assess(world, **kwargs).allowed)
        actor.stopped = False
        actor.segment_to_hex = 102
        self.assertFalse(service.assess(world, **kwargs).allowed)
        old = ObjectiveCaptureService(engine._movement_map_reader, load_objective_capture_policy('2'))
        self.assertTrue(old.assess(world, **kwargs).allowed)
        actor.stopped, actor.segment_to_hex = True, None
        enemy = world.operators[2]
        enemy.current_hex, enemy.x, enemy.y = 102, 2, 1
        enemy.stopped, enemy.segment_to_hex = False, 103
        assessment = service.assess(world, **kwargs)
        self.assertFalse(assessment.allowed)
        self.assertEqual(assessment.reason, 'OBJECTIVE_CONTESTED_BY_GROUND_PRESENCE')

    def test_capture_uses_prestate_not_same_tick_stop_completion(self):
        engine = self.capture_engine()
        actor = engine._require_world().operators[1]
        actor.stopped = False
        actor.state = 'TRANSITIONING_TO_STOP'
        actor.move_to_stop_remaining_ms = 1
        capture = EngineAction('red', 1, ActionType.CAPTURE, target_id=1)
        result = engine.step([capture], delta_time_ms=1)
        self.assertFalse(result.decisions[0].accepted)
        self.assertTrue(engine._require_world().operators[1].stopped)
        self.assertIsNone(engine._require_world().objectives[1].controlling_side)
        result = engine.step([capture], delta_time_ms=1)
        self.assertTrue(result.decisions[0].accepted, result.decisions)
        self.assertEqual(engine._require_world().objectives[1].controlling_side, 'red')


if __name__ == '__main__':
    unittest.main()
