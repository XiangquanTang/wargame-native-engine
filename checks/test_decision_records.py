"""Detached samples and native acceptance receipts, no real models required."""
from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from checks.test_match_controllers import fixture
from wargame_native_engine.matches.controllers import NoOpController, RandomController, RulesController, PluginController
from wargame_native_engine.matches.runner import run_match, context_for
from wargame_native_engine.matches.runtime import MatchRuntime
from wargame_native_engine.matches.decision_records import make_record, finish_record, validate_record, candidates, compact_public_map, expand_public_map, readable


class DecisionRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_game(self, red=None, blue=None, **kwargs):
        output = self.root / 'match'
        report = run_match(fixture(hidden=True), red or RandomController(), blue or NoOpController(), output,
                           max_steps=3, failure_policy='abort', **kwargs)
        return report, [json.loads(x) for x in (output/'decisions.jsonl').read_text().splitlines()]

    def test_native_records_both_sides_are_self_contained_and_hash_free(self):
        report, rows = self.run_game()
        self.assertEqual(len(rows), 6)
        self.assertEqual(report['decision_data']['schema'], 'native-decision-sample/1.0')
        for row in rows:
            validate_record(row)
            self.assertEqual(row['policy_input']['observation']['visible_enemies'], [])
            self.assertEqual(row['feedback']['sim_time_ms'], row['sim_time_ms'] + 1000)
            self.assertNotIn('catalog_hash', json.dumps(row))
            self.assertNotIn('action-', json.dumps(row))
            self.assertNotIn('expected_state_hash', json.dumps(row))
            self.assertEqual(len(row['history']), row['window'])
            if row['side'] == 'red':
                self.assertIn(row['execution']['status'], ('accepted', 'rejected'))
                self.assertIn('accepted', row['execution']['receipt'])
                self.assertIsNotNone(row['selection']['action']['action_type'])
            else:
                self.assertEqual(row['execution']['status'], 'no_controller_command')

    def test_rules_generated_move_candidates_include_selected_action(self):
        _, rows = self.run_game(red=RulesController())
        for row in rows:
            validate_record(row)
            if row['selection']:
                self.assertIn(row['selection'], row['policy_input']['legal_candidates'])

    def test_record_embeds_plan_and_is_detached_from_plugin_mutation(self):
        runtime = MatchRuntime(fixture(hidden=True), max_steps=1)
        self.addCleanup(runtime.close); runtime.reset()
        ctx = context_for(runtime.seat('blue'), 42)
        plan = {'stance': 'capture', 'assignments': []}
        row = make_record(ctx, ctx.binding, match_id='m', controller='llm', action_id=ctx.binding.action_space.action_ids[0],
                          status='ok', elapsed=0, details={'strategic_plan': plan})
        plan['stance'] = 'attack'
        self.assertEqual(row['policy_input']['strategic_plan']['stance'], 'capture')
        validate_record(json.loads(json.dumps(row)))

    def test_rejected_receipt_not_reported_as_success(self):
        row = {'execution': None}
        finish_record(row, results=[{'action_id':'a', 'accepted':False, 'code':'blocked'}],
                      selected_action_id='a', post_observation={}, after_time_ms=1000)
        self.assertEqual(row['execution']['status'], 'rejected')
        self.assertEqual(row['execution']['receipt'], {'accepted':False, 'code':'blocked'})

    def test_missing_receipt_is_explicit_not_invented(self):
        row = {}
        finish_record(row, results=[], selected_action_id='a', post_observation={}, after_time_ms=1000)
        self.assertEqual(row['execution']['status'], 'receipt_unavailable')

    def test_abort_keeps_pending_decisions_without_claiming_execution(self):
        with self.assertRaises(Exception):
            run_match(fixture(), NoOpController(), PluginController('checks.test_match_controllers:error'),
                      self.root/'failed', max_steps=3, failure_policy='abort')
        rows = [json.loads(x) for x in (self.root/'failed/decisions.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(x['execution']['status'] == 'window_not_completed' for x in rows))
        self.assertTrue(all(x['feedback'] is None for x in rows))

    def test_extract_one_record_without_original_replay_or_plan_log(self):
        from scripts.inspect_decisions import main
        _, rows = self.run_game()
        with contextlib.redirect_stdout(io.StringIO()):
            main(['--input', str(self.root/'match/decisions.jsonl'), '--side', 'red',
                  '--start-window','2','--end-window','2','--output', str(self.root/'clip.jsonl')])
        clip = [json.loads(x) for x in (self.root/'clip.jsonl').read_text().splitlines()]
        self.assertEqual(clip, [r for r in rows if r['side']=='red' and r['window']==2])
        validate_record(clip[0])
        self.assertEqual(len(clip[0]['history']), 2)
        with self.assertRaises(SystemExit):
            main(['--input', str(self.root/'clip.jsonl'), '--output', str(self.root/'clip.jsonl')])

    def test_public_geometry_ranges_are_lossless_and_self_contained(self):
        value = {'map_id': 'map', 'map_hash': 'technical', 'valid_hex_ids': [101, 102, 103, 201, 202, 999]}
        compressed = compact_public_map(value)
        self.assertEqual(compressed['valid_hex_ranges'], [[101, 103], [201, 202], [999, 999]])
        self.assertEqual(expand_public_map(compressed), readable(value))

    def test_old_hash_only_record_refused_instead_of_fabricated_migration(self):
        with self.assertRaises(ValueError): validate_record({'action_id':'action-old','side':'blue'})


if __name__ == '__main__': unittest.main()
