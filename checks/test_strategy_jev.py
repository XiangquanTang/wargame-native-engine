"""Real native engine and HTTP contract tests with fake model replies; no GPU."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from checks.test_match_controllers import fixture
from wargame_native_engine.matches.controllers import PluginController, NoOpController
from wargame_native_engine.matches.runner import context_for, run_match
from wargame_native_engine.matches.runtime import MatchRuntime
from wargame_native_engine.matches.strategy_jev import (
    LocalStrategyJev, StrategyJevError, _validate_plan, _decode, PLAN_SCHEMA,
)


def plan(state):
    return {'schema': PLAN_SCHEMA, 'stance': 'capture',
            'objective_priorities': [v['objective_id'] for v in state['objectives']], 'assignments': []}


def qwen_response(request):
    state = json.loads(request['messages'][-1]['content'])['state']
    return {'text': json.dumps(plan(state)), 'input_tokens': 100, 'output_tokens': 30,
            'finish_reason': 'stop', 'generation_seconds': .01}


def jev_response(request):
    row = request['states'][0]
    keys = list(row['questions']['action']['criteria'])
    return {'schema_version': 'openjev-toy-inference-v1', 'states': [
        {'id': row['id'], 'answers': {'action': {'type': 'choice', 'choice': keys[0],
          'value': keys[0], 'probabilities': {k: 1. if k == keys[0] else 0. for k in keys}}}}]}


class StrategyJevTests(unittest.TestCase):
    def setUp(self):
        self.runtime = MatchRuntime(fixture(hidden=True), max_steps=100)
        self.runtime.reset()
        self.addCleanup(self.runtime.close)
        self.context = context_for(self.runtime.seat('blue'), 42)
        self.calls = []
        self.plugin = LocalStrategyJev({})
        self.plugin.reset(self.context)
        self.addCleanup(self.plugin.close)
        def fake(url, request):
            self.calls.append((url, deepcopy(request)))
            return qwen_response(request) if url.endswith('/plan') else jev_response(request)
        self.plugin._post = fake

    def context_at(self, index, ms):
        space = replace(self.context.binding.action_space, decision_window_index=index, sim_time_ms=ms)
        observation = self.context.binding.observation
        observation = replace(observation, base=replace(observation.base, decision_window_index=index, sim_time_ms=ms))
        return replace(self.context, decision_index=index, binding=replace(self.context.binding, action_space=space, observation=observation))

    def test_real_native_candidates_and_strategy_handoff(self):
        selected = self.plugin.decide(self.context)
        self.assertIn(selected, self.context.binding.action_space.action_ids)
        self.assertEqual(len(self.calls), 2)
        request = self.calls[1][1]['states'][0]
        state = json.loads(request['state'])
        self.assertEqual(state['strategic_intent'], self.plugin.plan)
        self.assertEqual(state['visible_state']['visible_enemies'], [])
        self.assertEqual(len(request['questions']['action']['criteria']), len(self.context.binding.action_space.action_ids))
        self.assertNotIn('observation_hash', request['state'])
        self.assertNotIn('public_map', request['state'])

    def test_sim_time_cache_and_periodic_refresh(self):
        self.plugin.decide(self.context)
        self.plugin.decide(self.context_at(1, 1000))
        self.assertEqual(len([u for u, _ in self.calls if u.endswith('/plan')]), 1)
        self.plugin.decide(self.context_at(30, 30000))
        self.assertEqual(len([u for u, _ in self.calls if u.endswith('/plan')]), 2)

    def test_ownership_event_uses_minimum_interval(self):
        self.plugin.decide(self.context)
        context = self.context_at(1, 1000)
        obs = deepcopy(context.observation)
        obs['objectives'][0]['controlling_side'] = 'blue'
        self.plugin.decide(replace(context, observation=obs))
        self.assertEqual(self.plugin.plan_version, 1)
        self.plugin.decide(replace(self.context_at(5, 5000), observation=obs))
        self.assertEqual(self.plugin.plan_version, 2)

    def test_zero_and_one_bypass_models(self):
        for n in (0, 1):
            p = LocalStrategyJev({}); p.reset(self.context)
            p._post = lambda *a: self.fail('unexpected model call')
            old = self.context.binding.action_space
            space = replace(old, action_ids=old.action_ids[:n], canonical_actions_json=old.canonical_actions_json[:n],
                            mask=(True,) * n + (False,) * (255 - n))
            c = replace(self.context, binding=replace(self.context.binding, action_space=space))
            self.assertEqual(p.decide(c), old.action_ids[0] if n else None)

    def test_unknown_duplicate_and_hidden_plan_targets_rejected(self):
        state = self.plugin._state(self.context)
        value = plan(state)
        value['objective_priorities'] = [999]
        with self.assertRaises(StrategyJevError): _validate_plan(value, state)
        value = plan(state)
        value['assignments'] = [{'unit_ids': [2], 'task': 'attack', 'target_id': 1}]
        with self.assertRaises(StrategyJevError): _validate_plan(value, state)
        with self.assertRaises(ValueError): _decode('{"x":1,"x":2}')
        with self.assertRaises(ValueError): _decode('{"x":NaN}')

    def test_stale_snapshot_and_wrong_side_rejected(self):
        self.plugin.decide(self.context)
        with self.assertRaises(StrategyJevError): self.plugin.decide(self.context)
        with self.assertRaises(StrategyJevError): self.plugin.decide(replace(self.context_at(1, 1000), side='red'))

    def test_truncated_and_nonjson_plans_abort_without_jev(self):
        for malformed in ('length', 'not json'):
            p = LocalStrategyJev({}); p.reset(self.context)
            def fake(url, request):
                self.assertTrue(url.endswith('/plan'))
                answer = qwen_response(request)
                if malformed == 'length': answer['finish_reason'] = 'length'
                else: answer['text'] = malformed
                return answer
            p._post = fake
            with self.assertRaises(StrategyJevError): p.decide(self.context)

    def test_choice_response_identity_probabilities_and_argmax(self):
        mapping = {'c0': 'a', 'c1': 'b'}
        good = {'schema_version': 'openjev-toy-inference-v1', 'states': [{'id': 'snap', 'answers': {'action':
            {'type': 'choice', 'choice': 'c0', 'value': 'c0', 'probabilities': {'c0': .8, 'c1': .2}}}}]}
        self.assertEqual(LocalStrategyJev._validate_jev(good, 'snap', mapping), 'c0')
        for bad_probs in ({'c0': float('nan'), 'c1': .2}, {'c0': .1, 'c1': .9}, {'c0': 1.}, {'c0': .8, 'c1': .8}):
            bad = deepcopy(good); bad['states'][0]['answers']['action']['probabilities'] = bad_probs
            with self.assertRaises(StrategyJevError): LocalStrategyJev._validate_jev(bad, 'snap', mapping)
        with self.assertRaises(StrategyJevError): LocalStrategyJev._validate_jev(good, 'other', mapping)

    def test_nonlocal_endpoints_and_state_budget_rejected(self):
        for url in ('http://example.com/api/plan', 'https://127.0.0.1:8766/api/plan',
                    'http://user@127.0.0.1:8766/api/plan', 'http://127.0.0.1:8766/api/plan?x=1'):
            with self.assertRaises(ValueError): LocalStrategyJev({'qwen_url': url})
        p = LocalStrategyJev({'max_state_characters': 100}); p.reset(self.context)
        with self.assertRaises(StrategyJevError): p.decide(self.context)

    def test_255_candidates_all_sent_256_rejected(self):
        from wargame_native_engine.io.serialization import canonical_json
        for n in (255, 256):
            p = LocalStrategyJev({}); p.reset(self.context)
            calls = []
            def fake(url, request):
                calls.append(request)
                return qwen_response(request) if url.endswith('/plan') else jev_response(request)
            p._post = fake
            old = self.context.binding.action_space
            rows = []
            for i in range(n):
                row = deepcopy(old.actions[0]); row['action_id'] = 'synthetic-' + str(i)
                rows.append(row)
            space = replace(old, action_ids=tuple(r['action_id'] for r in rows),
                            canonical_actions_json=tuple(canonical_json(r) for r in rows), mask=(True,) * n)
            context = replace(self.context, binding=replace(self.context.binding, action_space=space))
            if n == 256:
                with self.assertRaises(StrategyJevError): p.decide(context)
                self.assertEqual(calls, [])
            else:
                p.decide(context)
                self.assertEqual(len(calls[-1]['states'][0]['questions']['action']['criteria']), 255)

    def test_provider_failure_no_retry_no_fallback(self):
        called = []
        def fail(url, request):
            called.append(url)
            raise StrategyJevError('provider_transport_error')
        self.plugin._post = fail
        with self.assertRaises(StrategyJevError): self.plugin.decide(self.context)
        self.assertEqual(len(called), 1)
        self.assertIsNone(self.plugin.plan)

    def test_assignment_target_disappears_forces_refresh(self):
        self.plugin.decide(self.context)
        self.plugin.plan['assignments'] = [{'unit_ids': [2], 'task': 'hold', 'target_id': 7}]
        context = self.context_at(1, 1000)
        observation = deepcopy(context.observation); observation['objectives'] = []
        self.plugin.decide(replace(context, observation=observation))
        self.assertEqual(self.plugin.plan_version, 2)
        self.assertEqual(self.plugin.plan['assignments'], [])

    def test_launcher_delegates_to_existing_match_and_preserves_inputs(self):
        from unittest.mock import patch
        from scripts.run_llm_jev_match import main
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for name in ('map.zip', 'scenario.json', 'final.pt'): (root / name).write_text('fixture')
            output = root / 'out'
            with patch('scripts.run_match.main', return_value=0) as run:
                self.assertEqual(main(['--input', str(root/'map.zip'), '--scenario-input', str(root/'scenario.json'),
                    '--red-checkpoint', str(root/'final.pt'), '--output', str(output)]), 0)
                args = run.call_args.args[0]
                self.assertEqual(args[args.index('--max-steps')+1], '20')
                self.assertEqual(args[args.index('--failure-policy')+1], 'abort')
                self.assertEqual(args[args.index('--candidate-limit')+1], '255')
                self.assertEqual(args[args.index('--decision-timeout-seconds')+1], '150.0')
            self.assertEqual((root/'final.pt').read_text(), 'fixture')
            self.assertTrue((root/'out-controller.json').is_file())

    def test_http_and_spawned_plugin_native_match(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((self.path, request))
                reply = qwen_response(request) if self.path == '/api/plan' else jev_response(request)
                data = json.dumps(reply).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        port = server.server_address[1]
        with tempfile.TemporaryDirectory() as root:
            config = {'qwen_url': f'http://127.0.0.1:{port}/api/plan', 'jev_url': f'http://127.0.0.1:{port}/api/evaluate',
                      'log_path': str(Path(root) / 'controller.jsonl')}
            plugin = PluginController('wargame_native_engine.matches.strategy_jev:create', config, 10.)
            result = run_match(fixture(hidden=True), NoOpController(), plugin, Path(root) / 'match',
                               max_steps=3, failure_policy='abort', decision_timeout_seconds=10.)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['failures'], [])
            logs = [json.loads(line) for line in (Path(root) / 'controller.jsonl').read_text().splitlines()]
            self.assertEqual(len([v for v in logs if v['event'] == 'decision']), 3)
            self.assertTrue(any(path == '/api/evaluate' for path, _ in received))
            from wargame_native_engine.matches.decision_records import validate_record
            samples = [json.loads(line) for line in (Path(root)/'match/decisions.jsonl').read_text().splitlines()]
            for sample in samples:
                validate_record(sample)
                if sample['side'] == 'blue':
                    self.assertIsNotNone(sample['policy_input']['strategic_plan'])
                    self.assertEqual(sample['policy_input']['strategic_plan'], sample['model_details']['strategic_plan'])
                    self.assertEqual(sample['execution']['status'], 'accepted')
                    self.assertTrue(sample['model_details']['probabilities'])
            for entry in logs:
                self.assertNotIn('action-', json.dumps(entry))
                self.assertNotIn('catalog_hash', json.dumps(entry))
            for path, request in received:
                text = json.dumps(request)
                self.assertNotIn('friendly_operators', text)
                if path == '/api/evaluate':
                    self.assertEqual(json.loads(request['states'][0]['state'])['visible_state']['visible_enemies'], [])


if __name__ == '__main__':
    unittest.main()
