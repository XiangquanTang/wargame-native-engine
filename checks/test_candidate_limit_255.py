"""New candidate cap and explicit legacy compatibility; no silent truncation."""
import argparse
import ast
from dataclasses import replace
import hashlib
import inspect
from pathlib import Path
import tempfile
import types
import unittest

from wargame_native_engine.training.candidate_limits import MAX_NEW_CANDIDATES, validate_new_candidate_limit
from wargame_native_engine.training.decision_encoder import DecisionActionEncoder, DecisionEncodingError
from wargame_native_engine.projection.decision_capability import ActionCatalogEntry, ActionCatalogSnapshot
from wargame_native_engine.projection.decision_semantic import DecisionCapabilityFamily, DecisionSide, DECISION_ACTION_CATALOG_SCHEMA_VERSION
from checks.test_match_controllers import fixture
ROOT = Path(__file__).resolve().parents[1]


def catalog(n):
    entries = tuple(ActionCatalogEntry('test:' + str(i), i, DecisionCapabilityFamily.WAIT,
        'WAIT', None, None, '{}', 0, 'test', hashlib.sha256(str(i).encode()).hexdigest()) for i in range(n))
    return ActionCatalogSnapshot(DECISION_ACTION_CATALOG_SCHEMA_VERSION, DecisionSide.RED, 0, 0, 0, entries)


class CandidateLimitTests(unittest.TestCase):
    def test_new_limit_rejects_zero_bool_float_and_256(self):
        for value in (0, -1, True, 255.0, '255', 256):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_new_candidate_limit(value)
        self.assertEqual(validate_new_candidate_limit(1), 1)
        self.assertEqual(validate_new_candidate_limit(255), 255)

    def test_zero_one_255_candidates_encode_without_truncation(self):
        encoder = DecisionActionEncoder(MAX_NEW_CANDIDATES)
        for n in (0, 1, 255):
            with self.subTest(n=n):
                original = catalog(n)
                result = encoder.encode(original)
                self.assertEqual(result.action_ids, original.action_ids)
                self.assertEqual(sum(result.mask), n)
                self.assertEqual(len(result.mask), 255)
                self.assertEqual(len(result.actions), n)
                if n:
                    self.assertEqual(encoder.resolve(result, original, n - 1), original.action_ids[-1])

    def test_256_catalog_explicitly_rejected_not_sliced(self):
        original = catalog(256)
        with self.assertRaisesRegex(DecisionEncodingError, 'max_actions=255'):
            DecisionActionEncoder(255).encode(original)
        self.assertEqual(original.action_count, 256)
        legacy = DecisionActionEncoder(256).encode(original)
        self.assertEqual(len(legacy.action_ids), 256)

    def test_match_defaults_are_255(self):
        from scripts.run_match import parser
        from wargame_native_engine.matches.runner import run_match
        from wargame_native_engine.matches.runtime import MatchRuntime
        self.assertEqual(parser().parse_args(['--scenario-bundle', 'unused', '--output', 'unused']).candidate_limit, 255)
        self.assertEqual(inspect.signature(run_match).parameters['candidate_limit'].default, 255)
        self.assertEqual(inspect.signature(MatchRuntime).parameters['candidate_limit'].default, 255)

    def test_match_rejects_256_before_creating_output(self):
        from scripts.run_match import main
        from wargame_native_engine.matches.runner import run_match
        from wargame_native_engine.matches.runtime import MatchRuntime
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / 'not-created'
            with self.assertRaisesRegex(ValueError, '255'):
                main(['--scenario-bundle', 'missing', '--output', str(out), '--candidate-limit', '256'])
            self.assertFalse(out.exists())
            with self.assertRaisesRegex(ValueError, '255'):
                run_match(None, None, None, out, candidate_limit=256)
            self.assertFalse(out.exists())
            with self.assertRaisesRegex(ValueError, '255'):
                MatchRuntime(None, candidate_limit=256)

    def test_native_short_match_uses_255_and_preserves_training_metadata(self):
        from wargame_native_engine.matches.runner import run_match
        from wargame_native_engine.matches.controllers import NoOpController
        red = NoOpController()
        red.training_metadata = {'candidate_limit': 256}
        with tempfile.TemporaryDirectory() as temporary:
            result = run_match(fixture(), red, NoOpController(), Path(temporary) / 'match', max_steps=2, failure_policy='abort')
        self.assertEqual(result['candidate_limit'], 255)
        self.assertEqual(result['steps'], 2)
        self.assertEqual(result['failures'], [])
        self.assertEqual(result['controllers']['red']['match_transfer']['candidate_limit'], {'training': 256, 'match': 255, 'changed': True})
        self.assertEqual(red.training_metadata['candidate_limit'], 256)

    def test_training_parser_and_guard_without_optional_torch(self):
        # Execute exact parser/argument-validation definitions, not a reimplemented
        # parser. Excludes optional model imports; this is not a torch/Ray test.
        tree = ast.parse((ROOT / 'scripts/run_rl.py').read_text())
        funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in ('parser', 'prepare_training_args', '_CandidateLimitAction')]
        ns = {'argparse': argparse, 'Path': Path, '__doc__': '', 'MAX_NEW_CANDIDATES': MAX_NEW_CANDIDATES,
              'validate_new_candidate_limit': validate_new_candidate_limit, 'encoder_schema': lambda args: 'test'}
        exec(compile(ast.Module(body=funcs, type_ignores=[]), 'run_rl_argument_contract', 'exec'), ns)
        parser = ns['parser']()
        args = parser.parse_args(['train', '--output', 'unused'])
        self.assertEqual(args.candidate_limit, 255)
        ns['prepare_training_args'](args)
        for extra in ([], ['--warm-start', 'old.pt']):
            args = parser.parse_args(['train', '--output', 'unused', '--candidate-limit', '256'] + extra)
            with self.assertRaisesRegex(ValueError, '255'):
                ns['prepare_training_args'](args)
        args = parser.parse_args(['train', '--output', 'unused', '--candidate-limit', '256', '--resume', 'old.pt'])
        ns['prepare_training_args'](args)
        self.assertEqual(args.candidate_limit, 256)
        calls = []
        def load(path, **kwargs):
            calls.append((path, kwargs))
            return {'metadata': {'candidate_limit': 256}}
        ns['torch'] = types.SimpleNamespace(load=load)
        args = parser.parse_args(['train', '--output', 'unused', '--resume', 'old.pt'])
        ns['prepare_training_args'](args)
        self.assertEqual(args.candidate_limit, 256)
        self.assertEqual(calls[0][1], {'map_location': 'cpu', 'weights_only': True})
        ns['prepare_training_args'](args)
        self.assertEqual(len(calls), 1)
        args = parser.parse_args(['train', '--output', 'unused', '--resume', 'old.pt', '--candidate-limit', '255'])
        ns['prepare_training_args'](args)
        self.assertEqual(args.candidate_limit, 255)  # later strict checkpoint comparison rejects mismatch
        self.assertEqual(len(calls), 1)
        args = parser.parse_args(['evaluate', '--output', 'unused', '--checkpoint', 'old.pt'])
        self.assertIsNone(args.candidate_limit)  # loader still inherits checkpoint metadata

    def test_minimal_env_default_changes_but_explicit_legacy_capacity_remains(self):
        from wargame_native_engine.training.minimal_runtime import MinimalTrainingEnv
        self.assertEqual(inspect.signature(MinimalTrainingEnv).parameters['candidate_limit'].default, 255)
        for capacity in (255, 256):
            env = MinimalTrainingEnv(fixture(), horizon=1, candidate_limit=capacity, opponent='no-op')
            try:
                env.reset(42)
                self.assertEqual(len(env.current_decision_binding.action_space.mask), capacity)
            finally:
                env.close()


if __name__ == '__main__':
    unittest.main()
