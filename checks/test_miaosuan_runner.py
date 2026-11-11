"""Runner-only contracts using fake external policies, never uploaded code."""
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from wargame_native_engine.agents import NoOpAgent
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.evaluation import load_manifest, run_benchmark, NoOpPolicy
from wargame_native_engine.evaluation.miaosuan_runner import run_miaosuan_match
from wargame_native_engine.evaluation.runner import _engine_identity

ROOT = Path(__file__).resolve().parents[1]

class FakeAgent:
    def setup(self, info):
        self.seat = info['seat']
    def step(self, obs):
        return [{'actor': self.seat, 'type': 200}]
    def close(self):
        self.closed = True

class MiaosuanRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'run'
        self.bundle = ScenarioBundle.from_path(ROOT / 'examples/source_movement_scenario.json')

    def run_fake(self, factory=lambda side, seed: FakeAgent(), **kwargs):
        # Runtime-data construction is separately tested by the adapter tests.
        setup = lambda bundle, side: {'seat': 1, 'faction': 0 if side == 'red' else 1, 'role': 1}
        with patch('wargame_native_engine.agents.miaosuan.build_miaosuan_setup', setup), patch(
            'wargame_native_engine.evaluation.miaosuan_runner._engine_identity', return_value=_engine_identity()):
            return run_miaosuan_match(self.bundle, self.output, factory,
                                      policy_id='fake/1', max_decisions=2, **kwargs)

    def test_nonnoop_opponent_and_strict_replay(self):
        result = self.run_fake()
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(result['strict_replay_matches'])
        self.assertEqual(result['applied_decisions'], 2)
        rows = [json.loads(line) for line in (self.output / 'decisions.jsonl').read_text().splitlines()]
        self.assertTrue(rows[0]['sides']['blue']['actions'])
        self.assertEqual(rows[0]['sides']['red']['trace'][0]['status'], 'unsupported')
        self.assertEqual(result['comparison_scope'], 'unequal-action-budget-integration-only')

    def test_self_play_fresh_seeds_and_close(self):
        agents, calls = [], []
        def factory(side, seed):
            calls.append((side, seed))
            agent = FakeAgent()
            agents.append(agent)
            return agent
        result = self.run_fake(factory, self_play=True)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual([c[0] for c in calls], ['red', 'blue'])
        self.assertNotEqual(calls[0][1], calls[1][1])
        self.assertTrue(all(agent.closed for agent in agents))

    def test_failed_policy_preserves_strict_prefix(self):
        class Broken(FakeAgent):
            def step(self, obs):
                raise ValueError('secret not logged')
        result = self.run_fake(lambda side, seed: Broken())
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['strict_replay_matches'], result)
        self.assertNotIn('secret', (self.output / 'result.json').read_text())

    def test_custom_opponent_requires_identity(self):
        with self.assertRaises(ValueError):
            self.run_fake(opponent_factory=lambda side, seed: NoOpAgent())
        self.assertFalse(self.output.exists())

    def test_trace_budget_stops_without_advance(self):
        result = self.run_fake(max_trace_bytes=1)
        self.assertEqual(result['stop_reason'], 'trace_budget_exceeded')
        self.assertEqual(result['applied_decisions'], 0)
        self.assertTrue(result['strict_replay_matches'])

    def test_cli_original_package_imports_authored_fixture_only(self):
        import sys
        from scripts.run_miaosuan_baseline import main
        root = Path(self.temp.name) / 'external'
        (root / 'ai').mkdir(parents=True)
        # Exact import styles read from uploaded ZIP; bodies are ours only.
        fixtures = {
            '__init__.py': 'from .agent import Agent\n',
            'base_agent.py': 'from abc import ABC, abstractmethod\nfrom typing import List\nclass BaseAgent: pass\n',
            'map.py': 'import heapq\nimport random\nclass Map: pass\n',
            'execute.py': 'from copy import deepcopy\nfrom functools import partial, reduce\n',
            'agent.py': 'import json\nimport os\nimport random\nfrom .base_agent import BaseAgent\nfrom .map import Map\nfrom .execute import *\nclass Agent(BaseAgent): pass\n',
        }
        for name, body in fixtures.items():
            (root / 'ai' / name).write_text(body)
        original_path = sys.path[:]
        names = ('ai', 'ai.agent', 'ai.base_agent', 'ai.map', 'ai.execute')
        saved = {name: sys.modules.pop(name, None) for name in names}
        captured = []
        def run(bundle, output, factory, **kwargs):
            self.assertTrue(kwargs['self_play'])
            self.assertIsNone(kwargs['opponent_factory'])
            self.assertEqual(kwargs['opponent_id'], 'self-play')
            captured.append(factory('red', 42).__class__.__name__)
            return dict(status='completed', strict_replay_matches=True, applied_decisions=1, errors=[])
        try:
            with patch('scripts.run_miaosuan_baseline.run_miaosuan_match', run):
                self.assertEqual(main(['--scenario', str(ROOT / 'examples/source_movement_scenario.json'),
                    '--agent-root', str(root), '--output', str(self.output), '--allow-external-code']), 0)
        finally:
            sys.path[:] = original_path
            for name, value in saved.items():
                sys.modules.pop(name, None)
                if value is not None:
                    sys.modules[name] = value
        self.assertEqual(captured, ['Agent'])

    def test_catalog_custom_opponent_fresh_and_hashed(self):
        manifest = replace(load_manifest(ROOT / 'benchmarks/manifests/smoke.json'),
                           max_decisions=1, seeds=(42,), sides=('red', 'blue'))
        contexts = []
        def factory(context):
            contexts.append(context)
            return NoOpAgent()
        with patch('wargame_native_engine.evaluation.runner._engine_identity', return_value=_engine_identity()):
            result = run_benchmark(manifest, self.output, lambda _: NoOpPolicy(),
                policy_id='fake', opponent_factory=factory, opponent_id='custom-noop/1',
                opponent_provenance={'revision': 'test'})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([c.side for c in contexts], ['blue', 'red'])
        self.assertNotEqual(result['manifest_hash'], manifest.semantic_hash)
        self.assertEqual(result['manifest']['opponent']['id'], 'custom-noop/1')

if __name__ == '__main__':
    unittest.main()
