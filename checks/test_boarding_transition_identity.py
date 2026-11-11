"""Reusable boarding commands have deterministic, distinct execution identities."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from wargame_native_engine.core.action import ActionType
from wargame_native_engine.core.action_router import RoutedAction
from wargame_native_engine.core.boarding_runtime import apply_source_boarding_intents
from wargame_native_engine.core.boarding import (
    BoardingTransitionType, boarding_transition_id, normalize_boarding_transitions,
)
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.replay import ReplayWriter, replay_actions
from wargame_native_engine.io.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[1]


class BoardingTransitionIdentityTests(unittest.TestCase):
    def engine(self):
        scenario = load_scenario(ROOT / 'examples/maps/scenarios/boarding.json')
        scenario = replace(scenario, terminal_rules=replace(scenario.terminal_rules, max_ticks=10000))
        engine = NativeEngine(scenario)
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        return engine

    def action(self, engine, kind):
        return next(item.action for item in engine.valid_actions('red')
                    if item.action.action_type is kind)

    def apply(self, engine, kind, dt=75000):
        action = self.action(engine, kind)
        result = engine.step([action], delta_time_ms=dt)
        self.assertTrue(result.decisions[0].accepted, result.decisions)
        return action, result

    def test_twenty_cycles_reuse_candidates_but_not_transition_ids(self):
        engine = self.engine()
        actions = {}
        for cycle in range(20):
            for kind in (ActionType.BOARD, ActionType.UNLOAD):
                action, result = self.apply(engine, kind)
                actions.setdefault(kind, action.action_id)
                self.assertEqual(action.action_id, actions[kind])
                base = boarding_transition_id(action.action_id, BoardingTransitionType(kind.value))
                expected = base if cycle == 0 else f'{base}-{cycle + 1}'
                started = next(event for event in result.events if event.event_type.value == f'{kind.value.lower()}_started')
                completed = next(event for event in result.events if event.event_type.value == f'{kind.value.lower()}_completed')
                self.assertEqual(started.data['transition_id'], expected)
                self.assertEqual(completed.data['transition_id'], expected)
                world = engine._require_world()
                self.assertEqual(world.operators[101].mounted_on, 102 if kind is ActionType.BOARD else None)
                self.assertEqual(world.operators[102].passenger_id, 101 if kind is ActionType.BOARD else None)
        history = engine.snapshot()['world']['boarding_transitions']
        self.assertEqual(len(history), 40)
        self.assertEqual(len({item['transition_id'] for item in history}), 40)

    def test_restore_completed_history_and_inflight_transition_is_deterministic(self):
        engine = self.engine()
        for _ in range(2):
            self.apply(engine, ActionType.BOARD)
            self.apply(engine, ActionType.UNLOAD)
        snapshot = engine.snapshot()
        self.apply(engine, ActionType.BOARD, dt=1)
        inflight = engine.snapshot()
        engine.step([], delta_time_ms=75000)
        self.apply(engine, ActionType.UNLOAD)
        final = engine.snapshot()
        restored = self.engine()
        restored.restore(snapshot)
        self.apply(restored, ActionType.BOARD, dt=1)
        self.assertEqual(restored.snapshot(), inflight)
        restored.restore(inflight)
        restored.step([], delta_time_ms=75000)
        self.apply(restored, ActionType.UNLOAD)
        self.assertEqual(restored.snapshot(), final)

    def test_duplicate_batch_and_active_command_do_not_start_second_transition(self):
        for kind in (ActionType.BOARD, ActionType.UNLOAD):
            engine = self.engine()
            if kind is ActionType.UNLOAD:
                self.apply(engine, ActionType.BOARD)
            action = self.action(engine, kind)
            before = len(engine._require_world().boarding_transitions)
            result = engine.step([action, action], delta_time_ms=1)
            self.assertEqual(sum(item.accepted for item in result.decisions), 1)
            self.assertEqual(len(engine._require_world().boarding_transitions), before + 1)
            result = engine.step([action], delta_time_ms=1)
            self.assertFalse(result.decisions[0].accepted)
            self.assertEqual(len(engine._require_world().boarding_transitions), before + 1)

    def test_active_participant_and_duplicate_id_invariants_remain_strict(self):
        engine = self.engine()
        self.apply(engine, ActionType.BOARD, dt=1)
        transition = engine._require_world().boarding_transitions[0]
        with self.assertRaisesRegex(ValueError, 'IDs must be unique'):
            normalize_boarding_transitions((transition, transition))
        with self.assertRaisesRegex(ValueError, 'carrier cannot have multiple'):
            normalize_boarding_transitions((transition, replace(transition, transition_id='distinct')))
        with self.assertRaisesRegex(ValueError, 'passenger cannot have multiple'):
            normalize_boarding_transitions((transition, replace(transition, transition_id='distinct', carrier_id=999)))

    def test_runtime_allocator_does_not_bypass_active_conflict_or_mutate_on_rejection(self):
        engine = self.engine()
        action = self.action(engine, ActionType.BOARD)
        error, intent = engine._stage_source_boarding(
            RoutedAction(0, action), engine._require_world())
        self.assertIsNone(error)
        self.assertIsNotNone(intent)
        engine.step([action], delta_time_ms=1)
        before = engine.snapshot()
        with self.assertRaisesRegex(ValueError, 'carrier cannot have multiple'):
            list(apply_source_boarding_intents(
                engine._require_world(), [intent], engine._boarding_authority))
        self.assertEqual(engine.snapshot(), before)

    def test_allocator_skips_existing_suffixes_in_restored_history(self):
        engine = self.engine()
        action, _ = self.apply(engine, ActionType.BOARD)
        self.apply(engine, ActionType.UNLOAD)
        world = engine._require_world()
        first = next(item for item in world.boarding_transitions
                     if item.transition_type is BoardingTransitionType.BOARD)
        # A serialized history can contain an already occupied suffix. Allocation
        # checks actual IDs rather than trusting a count or a process-local cache.
        world.boarding_transitions = normalize_boarding_transitions(
            (*world.boarding_transitions, replace(first, transition_id=first.transition_id + '-2')))
        restored = self.engine()
        restored.restore(engine.snapshot())
        self.apply(restored, ActionType.BOARD)
        self.assertIn(first.transition_id + '-3',
                      {item.transition_id for item in restored._require_world().boarding_transitions})

    def test_repeated_cycles_strict_replay(self):
        engine = self.engine()
        with tempfile.TemporaryDirectory() as directory:
            writer = ReplayWriter(directory, run_id='boarding-identity', scenario=engine.scenario,
                seed=42, agents={'red': 'test', 'blue': 'test'},
                max_ticks=engine.terminal_rules.max_ticks, ruleset=engine.ruleset, project_commit=None)
            writer.record_initial_state(engine.snapshot())
            for _ in range(3):
                for kind in (ActionType.BOARD, ActionType.UNLOAD):
                    _, result = self.apply(engine, kind)
                    writer.record_step(result, engine.snapshot())
            writer.finalize({'result': engine.result().to_dict()}, status='completed')
            self.assertTrue(replay_actions(engine.scenario, directory, strict=True).matches)


if __name__ == '__main__':
    unittest.main()
