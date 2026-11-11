"""Offline end-to-end controller contracts; no provider credentials or calls."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.replay.compact import CompactReplayReader
from wargame_native_engine.viewer.replay_view import load_replay_view

ROOT = Path(__file__).resolve().parents[1]


def fixture(*, hidden=False, max_ticks=1000):
    record = json.loads((ROOT / 'examples/source_movement_scenario.json').read_text())
    record['objectives'] = [{'objective_id': 7, 'x': 4, 'y': 1}]
    record['terminal_rules']['max_ticks'] = max_ticks
    for operator in record['operators']:
        operator['raw_attributes']['ammunition'] = {'0': 0}
        if hidden:
            operator['sensor_range'] = 0
    return ScenarioBundle.from_scenario(scenario_from_dict(record))


class FakePlugin:
    def reset(self, context):
        self.side = context.side
    def decide(self, context):
        assert context.side == self.side
        return next((a for a, mask in zip(context.binding.action_space.action_ids,
                                         context.binding.action_space.mask) if mask), None)
    def close(self):
        pass


class InvalidPlugin(FakePlugin):
    def decide(self, context):
        return 'not-an-authoritative-action-id'


class ErrorPlugin(FakePlugin):
    def decide(self, context):
        raise RuntimeError('test-private-error-must-not-be-action')


class SlowPlugin(FakePlugin):
    def decide(self, context):
        time.sleep(10)
        return None


class HiddenPlugin(FakePlugin):
    def decide(self, context):
        assert not context.observation.get('visible_enemies', ())
        assert not hasattr(context, '_manager')
        assert not hasattr(context, 'runtime')
        friendly = context.observation.get('friendly_operators', ())
        assert all(o.get('side') == context.side for o in friendly)
        return None


class FakePlanner(FakePlugin):
    def propose(self, context):
        return [a for a, mask in zip(context.binding.action_space.action_ids,
                                    context.binding.action_space.mask) if mask][:2]


class FakeEvaluator(FakePlugin):
    def evaluate(self, context, proposals):
        return proposals[-1] if proposals else None


class BadPlanner(FakePlanner):
    def propose(self, context):
        return ['not-legal']


class BadEvaluator(FakeEvaluator):
    def evaluate(self, context, proposals):
        return 'not-proposed'


def bad_planner(config): return BadPlanner()
def bad_evaluator(config): return BadEvaluator()
def fake(config): return FakePlugin()
def invalid(config): return InvalidPlugin()
def error(config): return ErrorPlugin()
def slow(config): return SlowPlugin()
def hidden(config): return HiddenPlugin()
def planner(config): return FakePlanner()
def evaluator(config): return FakeEvaluator()


class MatchControllerTests(unittest.TestCase):
    def setUp(self):
        from wargame_native_engine.matches.controllers import (
            NoOpController, RandomController, RulesController, PluginController, HybridController,
        )
        from wargame_native_engine.matches.runner import run_match
        self.NoOp = NoOpController
        self.Random = RandomController
        self.Rules = RulesController
        self.Plugin = PluginController
        self.Hybrid = HybridController
        self.run_match = run_match
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_game(self, red, blue, name='match', **kwargs):
        return self.run_match(kwargs.pop('bundle', fixture()), red, blue,
                              self.root / name, max_steps=kwargs.pop('max_steps', 4),
                              seed=23, failure_policy=kwargs.pop('failure_policy', 'abort'), **kwargs)

    def test_rules_random_native_replay_and_analysis(self):
        self.run_game(self.Rules(), self.Random())
        reader = CompactReplayReader(self.root / 'match/replay/replay.zip')
        self.assertTrue(reader.hash_verified)
        self.assertEqual(reader.manifest['analysis_model'], 'native')
        self.assertEqual(reader.assets['schema'], 'native-scenario-split/1.0')
        self.assertEqual(reader.frame_count, 5)
        view = load_replay_view(self.root / 'match/replay')
        self.assertEqual(view.frame_count, 5)
        self.assertIn('analysis', view.frame_bundle_record(4))
        for side in ('RED', 'BLUE'):
            self.assertEqual(view.frame_record(4, side)['perspective'], side)

    def test_seeded_repeat_has_identical_replay_frames(self):
        for name in ('a', 'b'):
            self.run_game(self.Random(), self.Random(), name)
        a, b = [CompactReplayReader(self.root / n / 'replay/replay.zip') for n in ('a', 'b')]
        self.assertEqual(list(a.iter_records()), list(b.iter_records()))

    def test_output_is_never_overwritten(self):
        existing = self.root / 'match'
        existing.mkdir()
        sentinel = existing / 'user-data.txt'
        sentinel.write_text('keep')
        with self.assertRaises((FileExistsError, ValueError)):
            self.run_game(self.NoOp(), self.NoOp())
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_llm_and_hybrid_fake_plugins(self):
        self.run_game(self.Plugin('checks.test_match_controllers:fake'),
                      self.Hybrid('checks.test_match_controllers:planner',
                                  'checks.test_match_controllers:evaluator'))
        self.assertTrue((self.root / 'match/match.json').exists())

    def test_hybrid_rejects_invalid_proposals_and_evaluator_choice(self):
        for planner_name, evaluator_name in [('bad_planner', 'evaluator'),
                                             ('planner', 'bad_evaluator')]:
            report = self.run_game(
                self.Hybrid('checks.test_match_controllers:' + planner_name,
                            'checks.test_match_controllers:' + evaluator_name),
                self.NoOp(), planner_name + evaluator_name, failure_policy='noop')
            self.assertEqual(report['failures'][0]['phase'], 'decide')
            self.assertEqual(report['failures'][0]['error_type'], 'RuntimeError')

    def test_plugins_receive_only_seat_limited_observation(self):
        report = self.run_game(self.Plugin('checks.test_match_controllers:hidden'),
                              self.Plugin('checks.test_match_controllers:hidden'),
                              bundle=fixture(hidden=True), failure_policy='abort')
        self.assertIsInstance(report, dict)

    def test_invalid_and_error_noop_policy_still_publishes(self):
        for plugin in ('invalid', 'error'):
            report = self.run_game(self.Plugin('checks.test_match_controllers:' + plugin),
                                   self.NoOp(), plugin, failure_policy='noop')
            self.assertTrue(report['failures'])
            self.assertEqual(report['failures'][0]['phase'], 'decide')
            self.assertTrue((self.root / plugin / 'replay/replay.zip').exists())
            self.assertNotIn('test-private-error-must-not-be-action',
                             (self.root / plugin / 'match.json').read_text())

    def test_error_abort_policy_does_not_silently_play(self):
        with self.assertRaises(Exception):
            self.run_game(self.Plugin('checks.test_match_controllers:error'),
                          self.NoOp(), failure_policy='abort')

    def test_timeout_is_bounded_and_does_not_block_game(self):
        started = time.monotonic()
        report = self.run_game(self.Plugin('checks.test_match_controllers:slow', timeout_seconds=2),
                               self.NoOp(), max_steps=2, failure_policy='noop')
        self.assertEqual(report['failures'][0]['error_type'], 'TimeoutError')
        self.assertEqual(report['failures'][0]['phase'], 'decide')
        self.assertLess(time.monotonic() - started, 8)

    def test_unconfigured_llm_is_an_explicit_error(self):
        with self.assertRaises((TypeError, ValueError)):
            self.Plugin('')


class MatchRuntimeTests(unittest.TestCase):
    def make_runtime(self, **kwargs):
        from wargame_native_engine.matches.runtime import MatchRuntime
        runtime = MatchRuntime(kwargs.pop('bundle', fixture()), **kwargs)
        self.addCleanup(runtime.close)
        runtime.reset()
        return runtime

    def test_seat_projections_and_candidates_hide_unobserved_opponent(self):
        runtime = self.make_runtime(bundle=fixture(hidden=True), max_steps=2)
        for side, hidden_id in [('red', 2), ('blue', 1)]:
            seat = runtime.seat(side)
            view = seat._controlled_view()
            self.assertEqual(view.observation['visible_enemies'], [])
            binding = seat.current_decision_binding
            self.assertFalse(any(binding.observation.observed_enemy_mask))
            for record in binding.action_space.actions:
                if record.get('action', {}).get('action_type') == 'SHOOT':
                    self.assertNotEqual(record['action'].get('target_id'), hidden_id)
            view.observation['friendly_operators'][0]['health'] = -999
            self.assertGreater(seat._controlled_view().observation['friendly_operators'][0]['health'], 0)

    def test_catalog_filters_hidden_future_profiles_for_context_and_teacher(self):
        from unittest.mock import patch
        from wargame_native_engine.matches.runner import context_for
        from wargame_native_engine.training.minimal_teacher import SimpleCaptureTeacher
        runtime = self.make_runtime(bundle=fixture(hidden=True), max_steps=2)
        original = runtime._manager.get_weapon_catalog
        extra = {'weapon_id': 987654, 'maximum_range': 999,
                 'secret_future_operator_id': 888888}
        def legacy_catalog(session_id):
            catalog = deepcopy(original(session_id))
            catalog['weapons'] = list(catalog['weapons']) + [extra]
            catalog['sources'] = {'hidden_inventory': [888888]}
            return catalog
        calls = []
        def side_catalog(session_id, *, side=None):
            calls.append(side)
            return legacy_catalog(session_id)
        for wrapper, expects_side in [(legacy_catalog, False), (side_catalog, True)]:
            with patch.object(runtime._manager, 'get_weapon_catalog', wrapper):
                for side, own_weapon, actor_id in [('red', 36, 1), ('blue', 29, 2)]:
                    seat = runtime.seat(side)
                    context = context_for(seat, 42)
                    self.assertEqual([w['weapon_id'] for w in context.weapon_catalog['weapons']],
                                     [own_weapon])
                    self.assertNotIn('sources', context.weapon_catalog)
                    self.assertNotIn('987654', json.dumps(context.weapon_catalog))
                    teacher = SimpleCaptureTeacher()
                    try:
                        teacher.reset(seat)
                        teacher_context = teacher._context(seat, actor_id)
                        catalog = teacher_context.operation_capabilities.operation_summary['policy_weapon_catalog']
                        self.assertEqual([w['weapon_id'] for w in catalog['weapons']], [own_weapon])
                        self.assertNotIn('sources', catalog)
                    finally:
                        teacher.close()
                    with self.assertRaises(ValueError):
                        seat._manager.get_weapon_catalog(seat._require_session_id(),
                                                        side='blue' if side == 'red' else 'red')
            if expects_side:
                self.assertEqual(calls, ['red', 'red', 'blue', 'blue'])

    def test_ppo_refreshes_current_own_profiles_each_decision(self):
        from types import SimpleNamespace as NS
        from wargame_native_engine.matches.controllers import PPOController
        controller = PPOController.__new__(PPOController)
        observed_profiles = []
        controller.encoder = NS(weapon_profiles={36: {'weapon_id': 36}})
        def encode(binding):
            observed_profiles.append(deepcopy(controller.encoder.weapon_profiles))
            return binding
        controller.encoder.encode = encode
        controller.learner = NS(act=lambda value, deterministic: NS(action_index=0))
        binding = NS(action_space=NS(action_ids=('legal-current-action',)))
        new_profile = {'weapon_id': 29, 'maximum_range': 4}
        context = NS(binding=binding, weapon_catalog={'weapons': [new_profile]})
        self.assertEqual(controller.decide(context), 'legal-current-action')
        self.assertEqual(observed_profiles, [{29: new_profile}])
        new_profile['maximum_range'] = 999
        self.assertEqual(controller.encoder.weapon_profiles[29]['maximum_range'], 4)
        context.weapon_catalog = {'weapons': []}
        controller.decide(context)
        self.assertEqual(observed_profiles[-1], {})

    def test_v25_hidden_future_catalog_noninterference_and_arrival_refresh(self):
        from dataclasses import asdict, replace
        from types import SimpleNamespace as NS
        from wargame_native_engine.matches.runtime import MatchRuntime
        from wargame_native_engine.matches.runner import context_for
        from wargame_native_engine.matches.controllers import PPOController
        from wargame_native_engine.io.scenario import load_scenario
        base = load_scenario(ROOT / 'examples/source_movement_scenario.json')
        if 'reinforcements' not in base.__dataclass_fields__:
            self.skipTest('v25 delayed reinforcement schema unavailable in v24')
        metadata = deepcopy(base.metadata)
        metadata['source_compatible']['weapons']['987654'] = deepcopy(
            metadata['source_compatible']['weapons']['29'])
        first = replace(base, schema_version='native-scenario/1.1',
                        reinforcements=({'operator_id': 2, 'arrival_frame': 2},))
        second = replace(first, metadata=metadata)
        contexts = []
        for scenario in (first, second):
            runtime = self.make_runtime(bundle=ScenarioBundle.from_scenario(scenario), max_steps=4)
            contexts.append(asdict(context_for(runtime.seat('red'), 42)))
            blue = context_for(runtime.seat('blue'), 42)
            self.assertFalse(any(blue.binding.observation.own_operator_mask))
            self.assertEqual(blue.weapon_catalog['weapons'], [])
            runtime.step(None, None)
            runtime.step(None, None)
            arrived = context_for(runtime.seat('blue'), 42)
            self.assertEqual(sum(arrived.binding.observation.own_operator_mask), 1)
            self.assertEqual([w['weapon_id'] for w in arrived.weapon_catalog['weapons']], [29])
            controller = PPOController.__new__(PPOController)
            controller.encoder = NS(weapon_profiles={}, encode=lambda binding: binding)
            controller.learner = NS(act=lambda value, deterministic: NS(action_index=0))
            controller.decide(arrived)
            self.assertEqual(set(controller.encoder.weapon_profiles), {29})
        # Random gateway session identity is transport-only, not policy evidence.
        for context in contexts:
            context['binding']['session_id'] = '<session>'
        self.assertEqual(contexts[0], contexts[1])
        self.assertNotIn('987654', json.dumps(contexts[1]))

    def test_both_actions_validated_before_either_submission(self):
        runtime = self.make_runtime(max_steps=2)
        with self.assertRaises(ValueError):
            runtime.step(0, 100000)
        self.assertEqual(runtime.steps, 0)
        self.assertEqual(runtime.seat('red')._controlled_view().step_index, 0)
        self.assertEqual(runtime.step(None, None)['steps'], 1)

    def test_runner_limit_does_not_invent_winner(self):
        runtime = self.make_runtime(max_steps=1)
        result = runtime.step(None, None)
        self.assertTrue(result['runner_limit'])
        self.assertTrue(result['truncated'])
        self.assertFalse(result['terminated'])
        self.assertIsNone(result['winner'])
        with self.assertRaises(RuntimeError):
            runtime.step(None, None)

    def test_native_terminal_not_overridden(self):
        runtime = self.make_runtime(bundle=fixture(max_ticks=1), max_steps=5)
        result = runtime.step(None, None)
        self.assertTrue(result['ended'])
        self.assertFalse(result['runner_limit'])
        self.assertTrue(result['terminated'] or result['truncated'])

    def test_observation_binding_refreshes_after_each_step(self):
        runtime = self.make_runtime(max_steps=2)
        before = runtime.seat('red').current_decision_binding
        runtime.step(None, None)
        after = runtime.seat('red').current_decision_binding
        self.assertEqual(before.observation.frame_index, 0)
        self.assertEqual(after.observation.frame_index, 1)
        self.assertEqual(after.observation.sim_time_ms, 1000)
        self.assertNotEqual(before.action_space.catalog_hash, after.action_space.catalog_hash)


try:
    import torch
except ImportError:
    torch = None


@unittest.skipIf(torch is None, 'optional torch learner dependency unavailable')
class MatchPPOTests(unittest.TestCase):
    """Real weights and encoder; no optimizer call allowed during match inference."""
    setUp = MatchControllerTests.setUp
    run_game = MatchControllerTests.run_game

    def test_v25_actual_ppo_inference_before_and_after_reinforcement(self):
        from dataclasses import replace
        from wargame_native_engine.matches.runtime import MatchRuntime
        from wargame_native_engine.matches.runner import context_for
        from wargame_native_engine.matches.controllers import PPOController
        from wargame_native_engine.training.minimal_ppo import MinimalPPO, PPOConfig
        base = fixture().scenario
        if 'reinforcements' not in base.__dataclass_fields__:
            self.skipTest('v25 delayed reinforcement schema unavailable in v24')
        torch.set_num_threads(1)
        scenario = replace(base, schema_version='native-scenario/1.1',
                           reinforcements=({'operator_id': 2, 'arrival_frame': 2},))
        checkpoint = self.root / 'arrival-synthetic.pt'
        torch.save({'learner': MinimalPPO(PPOConfig(), seed=9).state_dict(), 'metadata': {}}, checkpoint)
        runtime = MatchRuntime(ScenarioBundle.from_scenario(scenario), max_steps=4)
        self.addCleanup(runtime.close)
        runtime.reset()
        policy = PPOController(checkpoint)
        blue = runtime.seat('blue')
        context = context_for(blue, 42)
        policy.reset(context)
        policy.decide(context)
        self.assertEqual(policy.encoder.weapon_profiles, {})
        runtime.step(None, None)
        runtime.step(None, None)
        context = context_for(blue, 42)
        action_id = policy.decide(context)
        self.assertEqual(set(policy.encoder.weapon_profiles), {29})
        self.assertIn(action_id, context.binding.action_space.action_ids)

    def test_checkpoint_rules_both_seats_frozen_and_replay(self):
        from unittest.mock import patch
        from wargame_native_engine.training.minimal_ppo import MinimalPPO, PPOConfig, CompactEncoder
        from wargame_native_engine.training.minimal_runtime import MinimalTrainingEnv
        from wargame_native_engine.matches.controllers import PPOController
        torch.set_num_threads(1)
        learner = MinimalPPO(PPOConfig(), seed=9)
        env = MinimalTrainingEnv(fixture(), horizon=2)
        try:
            env.reset(seed=23)
            profiles = env._manager.get_weapon_catalog(env._require_session_id())
            encoder = CompactEncoder(env.approach.positions, env.approach.spatial_scale,
                                     weapon_profiles={int(w['weapon_id']): w for w in profiles['weapons']})
            observation = encoder.encode(env.current_decision_binding)
            metrics = learner.behavior_clone([(observation, 0)], epochs=1)
            self.assertGreater(metrics['parameter_l2_delta'], 0)
        finally:
            env.close()
        checkpoint = self.root / 'synthetic-trained.pt'
        torch.save({'learner': learner.state_dict(), 'metadata': {'side': 'red', 'opponent': 'random-legal'}}, checkpoint)
        before = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
        with (patch.object(MinimalPPO, 'update', side_effect=AssertionError('match retrained')),
              patch.object(MinimalPPO, 'behavior_clone', side_effect=AssertionError('match retrained'))):
            self.run_game(PPOController(checkpoint), self.Rules(), 'ppo-red')
            self.run_game(self.Rules(), PPOController(checkpoint), 'ppo-blue')
        self.assertEqual(hashlib.sha256(checkpoint.read_bytes()).hexdigest(), before)
        for name in ('ppo-red', 'ppo-blue'):
            self.assertTrue(CompactReplayReader(self.root / name / 'replay/replay.zip').hash_verified)


class MatchCliTests(unittest.TestCase):
    def test_incompatible_controller_flags_rejected(self):
        from scripts.run_match import parser, _controller
        for flags in [
            ['--red', 'ppo', '--red-checkpoint', 'unused.pt', '--red-plugin', 'm:f'],
            ['--red', 'llm', '--red-plugin', 'm:f', '--red-checkpoint', 'unused.pt'],
            ['--red', 'llm', '--red-plugin', 'm:f', '--red-evaluator', 'm:g'],
        ]:
            args = parser().parse_args(['--scenario-bundle', 'unused.json', '--output', 'unused-out'] + flags)
            with self.assertRaises(ValueError):
                _controller(args, 'red')

    def test_host_cleanup_error_still_closes_controllers_and_writes_report(self):
        from unittest.mock import patch
        from wargame_native_engine.matches.controllers import NoOpController
        from wargame_native_engine.matches.runtime import MatchRuntime
        from wargame_native_engine.matches.runner import run_match
        import tempfile
        class Closing(NoOpController):
            closed = False
            def close(self):
                self.closed = True
        close = MatchRuntime.close
        def fail_after_close(runtime):
            close(runtime)
            raise RuntimeError('synthetic cleanup failure')
        red, blue = Closing(), Closing()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'match'
            with patch.object(MatchRuntime, 'close', fail_after_close):
                with self.assertRaises(RuntimeError):
                    run_match(fixture(), red, blue, output, max_steps=1)
            self.assertTrue(red.closed and blue.closed)
            report = json.loads((output/'match.json').read_text())
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['cleanup_errors'][0]['side'], 'host')


if __name__ == '__main__':
    unittest.main()
