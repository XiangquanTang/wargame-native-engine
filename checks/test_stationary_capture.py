"""Capture v3 readiness is actor-only; old policy identities stay immutable."""
import json
from pathlib import Path
import unittest
from types import SimpleNamespace

from wargame_native_engine.core.world import ObjectiveState
from wargame_native_engine.rules.objective_capture import (
    ObjectiveCaptureService, load_objective_capture_policy,
    resolve_objective_capture_policy_identity,
)


class HexMap:
    # Native map adjacency is authority (not a square/Manhattan radius).
    cells = {101, 102, 201, 202, 203, 301, 302, 909}

    def contains(self, value):
        return value in self.cells

    def neighbors(self, value):
        return (101, 102, 201, 203, 301, 302) if value == 202 else ()


def actor(**changes):
    values = dict(alive=True, deployed=True, mounted_on=None,
                  current_hex=202, operator_type='PERSONNEL', subtype=None,
                  side='red', moving=False, stopped=True, state='STOPPED',
                  move_to_stop_remaining_ms=0, transition_kind=None,
                  transition_remaining_ms=0)
    values.update(changes)
    return SimpleNamespace(**values)


class StationaryCaptureTests(unittest.TestCase):
    def assess(self, capturer=None, enemy=None, version='3'):
        units = {1: capturer or actor()}
        if enemy is not None:
            units[2] = enemy
        world = SimpleNamespace(operators=units,
                                objectives={7: ObjectiveState(7, 2, 2)})
        return ObjectiveCaptureService(HexMap(), load_objective_capture_policy(version)).assess(
            world, side='red', actor_id=1, objective_id=7)

    def test_center_and_each_native_neighbor_allowed_outside_rejected(self):
        for cell in sorted(HexMap.cells):
            with self.subTest(cell=cell):
                result = self.assess(actor(current_hex=cell))
                self.assertEqual(result.allowed, cell != 909)
                self.assertEqual(result.region, (101, 102, 201, 202, 203, 301, 302))

    def test_moving_and_movement_state_are_rejected(self):
        for changes in ({'moving': True}, {'state': 'MOVING'}, {'state': 'MARCHING'},
                        {'stopped': False}):
            with self.subTest(changes=changes):
                result = self.assess(actor(**changes))
                self.assertFalse(result.allowed)
                self.assertIn(result.reason, {'ACTOR_MOVING', 'ACTOR_NOT_STOPPED'})

    def test_all_transition_authorities_independently_block_capture(self):
        for changes in (
            {'move_to_stop_remaining_ms': 1}, {'transition_remaining_ms': 1},
            {'transition_kind': 'WEAPON_LOCK'}, {'transition_kind': 'WEAPON_UNFOLD'},
            {'state': 'TRANSITIONING'}, {'state': 'TRANSITIONING_TO_STOP'},
        ):
            with self.subTest(changes=changes):
                result = self.assess(actor(**changes))
                self.assertFalse(result.allowed)
                self.assertEqual(result.reason, 'ACTOR_TRANSITION_IN_PROGRESS')

    def test_readiness_becomes_available_only_after_stop_and_transition(self):
        unit = actor(moving=True, stopped=False)
        self.assertFalse(self.assess(unit).allowed)
        unit.moving = False
        unit.move_to_stop_remaining_ms = 1
        unit.state = 'TRANSITIONING_TO_STOP'
        self.assertFalse(self.assess(unit).allowed)
        unit.move_to_stop_remaining_ms = 0
        unit.state = 'STOPPED'
        self.assertFalse(self.assess(unit).allowed)
        unit.stopped = True
        self.assertTrue(self.assess(unit).allowed)

    def test_moving_or_transitioning_enemy_still_contests(self):
        for changes in ({'moving': True, 'stopped': False},
                        {'transition_remaining_ms': 100},
                        {'state': 'TRANSITIONING_TO_STOP', 'move_to_stop_remaining_ms': 1}):
            with self.subTest(changes=changes):
                result = self.assess(enemy=actor(side='blue', **changes))
                self.assertEqual(result.reason, 'OBJECTIVE_CONTESTED_BY_GROUND_PRESENCE')

    def test_dead_actor_rejected_and_dead_enemy_does_not_contest(self):
        self.assertEqual(self.assess(actor(alive=False)).reason, 'ACTOR_DESTROYED')
        self.assertTrue(self.assess(enemy=actor(alive=False, side='blue')).allowed)

    def test_v2_keeps_old_readiness_semantics_v3_has_new_identity(self):
        unit = actor(moving=True, stopped=False, transition_remaining_ms=100)
        self.assertTrue(self.assess(unit, version='2').allowed)
        self.assertFalse(self.assess(unit, version='3').allowed)
        self.assertEqual(load_objective_capture_policy().policy_version, '3.0.0')
        for version in ('1', '2', '3'):
            policy = load_objective_capture_policy(version)
            self.assertEqual(resolve_objective_capture_policy_identity(policy.identity_record()), policy)
        self.assertNotEqual(load_objective_capture_policy('2').content_hash,
                            load_objective_capture_policy('3').content_hash)


class StationaryCaptureEngineTests(unittest.TestCase):
    def engine(self):
        from wargame_native_engine.core.engine import NativeEngine
        from wargame_native_engine.io.scenario import scenario_from_dict
        root = Path(__file__).resolve().parents[1]
        record = json.loads((root / 'examples/source_movement_scenario.json').read_text())
        record['objectives'] = [{'objective_id': 7, 'x': 1, 'y': 1}]
        engine = NativeEngine(scenario_from_dict(record), objective_capture_policy='3')
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        return engine

    def test_actual_route_arrival_and_stop_transition_gate_capture(self):
        from wargame_native_engine.core.action import ActionType, EngineAction
        engine = self.engine()
        capture = EngineAction('red', 1, ActionType.CAPTURE, target_id=7)
        move = EngineAction('red', 1, ActionType.MOVE, parameters={'path': [102]})
        self.assertTrue(engine.step([move], delta_time_ms=1).decisions[0].accepted)
        self.assertEqual(engine.step([capture], delta_time_ms=1).decisions[0].code,
                         'ACTOR_MOVING')
        for _ in range(100):
            engine.step([], delta_time_ms=1000)
            state = engine.snapshot()['world']['operators']['1']
            if state['state'] == 'TRANSITIONING_TO_STOP':
                break
        self.assertEqual(state['current_hex'], 102)
        self.assertGreater(state['move_to_stop_remaining_ms'], 0)
        self.assertEqual(engine.step([capture], delta_time_ms=1).decisions[0].code,
                         'ACTOR_TRANSITION_IN_PROGRESS')
        self.assertIsNone(engine.snapshot()['world']['objectives']['7']['controlling_side'])
        for _ in range(100):
            engine.step([], delta_time_ms=1000)
            if engine.snapshot()['world']['operators']['1']['stopped']:
                break
        self.assertTrue(engine.step([capture], delta_time_ms=1).decisions[0].accepted)
        self.assertEqual(engine.snapshot()['world']['objectives']['7']['controlling_side'], 'red')


if __name__ == '__main__':
    unittest.main()
