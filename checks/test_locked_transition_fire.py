"""Completed locks permit DIRECT during movement/posture transitions only."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from test_direct_fire_range import fixture_record, shoot
import test_fire_effect_query as effect_query
from test_moving_fire_interfaces import scenario_record
from wargame_native_engine.adjudication import DirectFireAdjudicator, FixedDiceRoller
from wargame_native_engine.adjudication.fire_effect_query import FireEffectQuery
from wargame_native_engine.agents.realtime import ManualRealtimeClock, RealtimeAgentSession
from wargame_native_engine.agents.tool_calling import GameToolAdapter
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.fire_intent import FireMode
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.replay import ReplayWriter, replay_actions, replay_source_archive
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.operations import ScenarioMatchFactory
from wargame_native_engine.operations.product import _capability_unavailable
from wargame_native_engine.viewer.human_capabilities import build_human_capabilities


class LockedTransitionFireTests(unittest.TestCase):
    def engine(self, *, in_range=False):
        record = fixture_record()
        if in_range:
            record['metadata']['source_compatible']['weapons']['36']['maximum_range'] = 10
        engine = NativeEngine(scenario_from_dict(record))
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        return engine

    def apply(self, engine, kind, *, dt=1, **parameters):
        result = engine.step([EngineAction('red', 1, kind, parameters=parameters)], delta_time_ms=dt)
        self.assertTrue(result.decisions[0].accepted, result.decisions)
        return result

    def transition(self, engine, kind):
        self.apply(engine, ActionType.WEAPON_LOCK, weapon_id=36,
                   dt=engine._source_policy.transition_ms)
        if kind == 'STOP':
            self.apply(engine, ActionType.MOVE, path=[102, 103, 104], movement_mode='NORMAL')
            self.apply(engine, ActionType.STOP_MOVE)
            actor = engine._require_world().operators[1]
            engine.step([], delta_time_ms=actor.segment_traversal_ms - actor.segment_elapsed_ms)
            self.assertGreater(engine._require_world().operators[1].move_to_stop_remaining_ms, 0)
        else:
            self.apply(engine, ActionType.CHANGE_STATE, state='MARCHING',
                       dt=engine._source_policy.transition_ms if kind == 'EXIT_MARCHING' else 1)
            if kind == 'EXIT_MARCHING':
                self.apply(engine, ActionType.CHANGE_STATE, state='NORMAL')
            self.assertEqual(engine._require_world().operators[1].transition_kind, kind)

    def shot(self, empty):
        return (EngineAction('red', 1, ActionType.SHOOT,
                             parameters={'weapon_id': 36, 'target_hex': 106}) if empty else shoot())

    def test_completed_lock_fires_through_actual_transitions_without_resetting_them(self):
        for kind in ('ENTER_MARCHING', 'EXIT_MARCHING', 'STOP'):
            for empty, in_range in ((True, False), (False, False), (False, True)):
                with self.subTest(kind=kind, empty=empty, in_range=in_range):
                    engine = self.engine(in_range=in_range)
                    self.transition(engine, kind)
                    before = deepcopy(engine._require_world().operators[1].to_dict())
                    result = engine.step([self.shot(empty)], delta_time_ms=1)
                    self.assertTrue(result.decisions[0].accepted, result.decisions)
                    actor = engine._require_world().operators[1]
                    self.assertTrue(actor.weapon_locked)
                    self.assertEqual(actor.locked_weapon_id, 36)
                    self.assertEqual(actor.raw_attributes['ammunition']['0'], 19)
                    self.assertGreater(actor.weapon_cooldown_remaining_ms, 0)
                    for key in ('transition_kind', 'movement_mode', 'move_path', 'movement_action_id'):
                        self.assertEqual(actor.to_dict()[key], before[key], key)
                    for key in ('transition_remaining_ms', 'move_to_stop_remaining_ms'):
                        self.assertEqual(actor.to_dict()[key], max(0, before[key] - 1), key)
                    second = engine.step([self.shot(empty)], delta_time_ms=1)
                    self.assertEqual(second.decisions[0].code, 'WEAPON_ON_COOLDOWN')
                    self.assertEqual(engine._require_world().operators[1].raw_attributes['ammunition']['0'], 19)

    def test_unlocked_and_incomplete_locks_never_gain_transition_exemption(self):
        for empty in (True, False):
            for kind, locked, selected in (
                ('ENTER_MARCHING', False, None),
                ('WEAPON_LOCK', False, 36),
                ('WEAPON_LOCK', True, 36),
                ('WEAPON_UNFOLD', True, 36),
            ):
                with self.subTest(empty=empty, kind=kind, locked=locked):
                    engine = self.engine()
                    actor = engine._require_world().operators[1]
                    # Preserve unfolded/stopped only to isolate the transition gate.
                    actor.transition_kind, actor.transition_remaining_ms = kind, 5000
                    actor.weapon_locked, actor.locked_weapon_id = locked, selected
                    result = engine.step([self.shot(empty)], delta_time_ms=1)
                    self.assertEqual(result.decisions[0].code, 'ATTACKER_TRANSITIONING')
                    self.assertEqual(engine._require_world().operators[1].raw_attributes['ammunition']['0'], 20)

    def test_capabilities_and_frozen_effect_query_agree_without_mutation(self):
        for kind in ('ENTER_MARCHING', 'EXIT_MARCHING', 'STOP'):
            engine = self.engine(in_range=True)
            self.transition(engine, kind)
            before = engine.snapshot()
            projection = effect_query.FireEffectQueryTests.projection(self, engine)
            self.assertTrue(any(item['action']['action_type'] == 'SHOOT'
                                and item['action']['actor_id'] == 1 for item in projection[2]))
            result = engine.query_fire_effect_from_projection('red', *projection,
                query=FireEffectQuery(1, 36, target_id=2, target_profile=effect_query.PROFILE))
            self.assertTrue(result['can_execute'], result)
            self.assertEqual(result['status'], 'RESOLVED', result)
            self.assertEqual(engine.snapshot(), before)

    def test_other_fire_modes_and_resources_keep_existing_rules(self):
        engine = self.engine(in_range=True)
        self.transition(engine, 'ENTER_MARCHING')
        world = engine._require_world()
        request = engine._direct_fire_request(shoot(), world.operators[1], world.operators[2])
        adjudicator = DirectFireAdjudicator(engine._adjudication_tables, FixedDiceRoller([1]))
        # Give GUIDED its old deployed posture to isolate its retained transition gate.
        guided = replace(request, fire_mode=FireMode.GUIDED, weapon_unfolded=True)
        self.assertEqual(adjudicator.validate(guided).reason_code, 'ATTACKER_TRANSITIONING')
        for changes, code in (({'ammunition_before': 0}, 'AMMUNITION_UNAVAILABLE'),
                              ({'locked_weapon_id': 37}, 'WEAPON_LOCKED_TO_OTHER_WEAPON'),
                              ({'weapon_cooldown_before': 1}, 'WEAPON_ON_COOLDOWN')):
            self.assertEqual(adjudicator.validate(replace(request, **changes)).reason_code, code)

    def test_transition_does_not_allow_unlock_switch_or_unfold(self):
        for kind in ('ENTER_MARCHING', 'EXIT_MARCHING', 'STOP'):
            for action, parameters in ((ActionType.WEAPON_UNLOCK, {}),
                    (ActionType.WEAPON_LOCK, {'weapon_id': 36}),
                    (ActionType.WEAPON_UNFOLD, {})):
                with self.subTest(kind=kind, action=action):
                    engine = self.engine()
                    self.transition(engine, kind)
                    actor = engine._require_world().operators[1]
                    actor.weapon_cooldown_remaining_ms = 5000
                    result = engine.step([EngineAction('red', 1, action,
                        parameters=parameters)], delta_time_ms=1)
                    self.assertFalse(result.decisions[0].accepted)
                    actor = engine._require_world().operators[1]
                    self.assertTrue(actor.weapon_locked)
                    self.assertEqual(actor.locked_weapon_id, 36)
                    self.assertEqual(actor.weapon_cooldown_remaining_ms, 4999)

    def test_switching_preserves_all_timers_and_independent_ammunition_pools(self):
        record = fixture_record()
        record['operators'][0]['weapon_ids'] = [36, 37]
        record['operators'][0]['raw_attributes']['ammunition']['1'] = 7
        weapons = record['metadata']['source_compatible']['weapons']
        weapons['37'] = dict(weapons['36'], ammunition_key='1')
        engine = NativeEngine(scenario_from_dict(record))
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        self.transition(engine, 'ENTER_MARCHING')
        result = engine.step([shoot()], delta_time_ms=1)
        self.assertTrue(result.decisions[0].accepted, result.decisions)
        engine.step([], delta_time_ms=engine._source_policy.transition_ms)
        actor = engine._require_world().operators[1]
        timers = ('weapon_cooldown_remaining_ms', 'guided_fire_recovery_remaining_ms',
                  'indirect_fire_planned_cooldown_remaining_ms')
        for field in timers:
            setattr(actor, field, 100000)
        self.apply(engine, ActionType.WEAPON_UNLOCK)
        self.apply(engine, ActionType.WEAPON_LOCK, weapon_id=37)
        actor = engine._require_world().operators[1]
        for field in timers:
            self.assertEqual(getattr(actor, field), 99998, field)
        self.assertEqual(actor.raw_attributes['ammunition'], {'0': 19, '1': 7})
        engine.step([], delta_time_ms=100000)
        result = engine.step([shoot(weapon_id=37)], delta_time_ms=1)
        self.assertTrue(result.decisions[0].accepted, result.decisions)
        self.assertEqual(engine._require_world().operators[1].raw_attributes['ammunition'],
                         {'0': 19, '1': 6})

    def test_blocked_menu_never_claims_movement_or_transition_alone_prevents_fire(self):
        for state in ('MOVING', 'TRANSITIONING', 'TRANSITIONING_TO_STOP'):
            menu = SimpleNamespace(available={},
                unit={'state': state, 'observed_enemy_ids': [2]})
            code, message = _capability_unavailable('SHOOT', menu)
            self.assertEqual(code, 'no_legal_shoot')
            self.assertIn('冷却', message)

    def test_unfold_requires_normal_command_control_and_keeps_timers(self):
        engine = self.engine()
        actor = engine._require_world().operators[1]
        actor.weapon_unfolded = False
        actor.command_lost = True
        actor.weapon_cooldown_remaining_ms = 5000
        self.assertNotIn(ActionType.WEAPON_UNFOLD, [item.action.action_type
            for item in engine.valid_actions('red') if item.action.actor_id == 1])
        result = engine.step([EngineAction('red', 1, ActionType.WEAPON_UNFOLD)], delta_time_ms=1)
        self.assertEqual(result.decisions[0].code, 'ACTOR_CONTROL_LOST')
        actor = engine._require_world().operators[1]
        self.assertIsNone(actor.transition_kind)
        self.assertEqual(actor.weapon_cooldown_remaining_ms, 4999)

    def test_transition_shot_strict_replay(self):
        engine = self.engine()
        with tempfile.TemporaryDirectory() as temporary:
            writer = ReplayWriter(temporary, run_id='locked-transition', scenario=engine.scenario,
                seed=42, agents={'red': 'test', 'blue': 'test'},
                max_ticks=engine.terminal_rules.max_ticks,
                ruleset=engine.ruleset, project_commit=None)
            writer.record_initial_state(engine.snapshot())
            for action, dt in (
                (EngineAction('red', 1, ActionType.WEAPON_LOCK, parameters={'weapon_id': 36}), engine._source_policy.transition_ms),
                (EngineAction('red', 1, ActionType.CHANGE_STATE, parameters={'state': 'MARCHING'}), 1),
                (shoot(), 1),
            ):
                result = engine.step([action], delta_time_ms=dt)
                self.assertTrue(result.decisions[0].accepted, result.decisions)
                writer.record_step(result, engine.snapshot())
            writer.finalize({'result': engine.result().to_dict()}, status='completed')
            self.assertTrue(replay_actions(engine.scenario, temporary, strict=True).matches)

    def test_human_tool_preview_submit_and_apply_in_transition(self):
        for empty in (False, True):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                record = scenario_record(locked=True, capable=False)
                record['operators'][0].update(state='TRANSITIONING', stopped=False,
                    transition_kind='ENTER_MARCHING', transition_remaining_ms=5000)
                scenario = scenario_from_dict(record)
                session = RealtimeAgentSession(ScenarioMatchFactory(
                    bundle=ScenarioBundle.from_scenario(scenario), storage_root=root / 'storage',
                    red_controller_id='red', blue_controller_id='blue', decision_interval_ms=1000,
                    frame_interval_ms=1000, replay_mode='legacy-debug'), run_namespace='locked-transition',
                    tick_interval_ms=1000, clock=ManualRealtimeClock())
                self.addCleanup(session.close)
                binding = session.reset(seed=42).red_binding
                adapter = GameToolAdapter(session, binding)
                caps = adapter.dispatch('game_capabilities', {'actor_ids': [1]})
                self.assertEqual(caps['capabilities']['operations']['SHOOT']['applicable_count'], 1)
                _, view, summary = session.operation_capabilities_snapshot(binding, (1,))
                human = build_human_capabilities(summary, view, (1,))
                self.assertTrue(human['direct_fire']['units'])
                observation = adapter.dispatch('game_observation', {})
                operation = {'operation_type': 'SHOOT', 'actor_ids': [1], 'weapon_choices': [[1, 36]],
                             **({'target_hex': 106} if empty else {'target_id': 2})}
                preview = adapter.dispatch('preview_game_operation', {'operation': operation,
                    'observation_version': observation['observation_version'], 'request_id': 'preview'})['preview']
                self.assertTrue(preview['can_execute'], preview)
                receipt = adapter.dispatch('submit_game_operation', {'preview_token': preview['preview_token'],
                    'request_id': 'submit', 'confirm_partial': False})
                self.assertTrue(receipt['accepted'], receipt)
                session.advance_tick()
                result = adapter.dispatch('game_command_result', {'command_sequence': receipt['command_sequence']})
                self.assertEqual(result['command']['status'], 'APPLIED', result)
                actor = session.latest_view(binding).observation['friendly_operators'][0]
                self.assertEqual(actor['transition_kind'], 'ENTER_MARCHING')
                self.assertGreater(actor['weapon_cooldown_remaining_ms'], 0)
                session.publish(root / 'published')
                self.assertTrue(replay_source_archive(scenario, root / 'published/replay.zip', strict=True).matches)


if __name__ == '__main__': unittest.main()
