"""Regression: native authorization metadata is not a tactical candidate feature."""
from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.matches.runtime import MatchRuntime
from wargame_native_engine.matches.runner import context_for
from wargame_native_engine.matches.decision_records import (
    make_record, validate_record, load_record, semantic_action, technical_field_paths)

ROOT = Path(__file__).resolve().parents[1]

class DecisionAuthorityTests(unittest.TestCase):
    def setup_record(self):
        bundle = ScenarioBundle.from_scenario(load_scenario(ROOT/'examples/maps/scenarios/boarding.json'))
        runtime = MatchRuntime(bundle, max_steps=2)
        self.addCleanup(runtime.close)
        runtime.reset()
        context = context_for(runtime.seat('red'), 42)
        native = next(x for x in context.binding.action_space.actions if x['action']['action_type']=='BOARD')
        row = make_record(context, context.binding, match_id='native-board', controller='rules',
                          action_id=native['action_id'], status='ok', elapsed=0.)
        return runtime, context, native, row

    def test_native_board_candidate_preserves_execution_and_game_parameters(self):
        runtime, context, native, row = self.setup_record()
        before = deepcopy(native)
        self.assertIn('authority_hash', native['action']['parameters'])
        validate_record(row)
        selected = row['selection']['action']
        self.assertNotIn('authority_hash', selected['parameters'])
        expected = deepcopy(native['action']); del expected['parameters']['authority_hash']
        self.assertEqual(selected, expected)
        self.assertEqual(native, before)
        # Execute the original binding payload, never the semantic export.
        runtime.step(context.binding.action_space.action_ids.index(native['action_id']), None)
        result = next(x for x in runtime.last_execution['red'] if x['action_id']==native['action_id'])
        self.assertTrue(result['accepted'], result)

    def test_jev_request_uses_semantic_candidates_and_returns_native_id(self):
        from checks.test_strategy_jev import qwen_response, jev_response
        from wargame_native_engine.matches.strategy_jev import LocalStrategyJev
        _, context, native, row = self.setup_record()
        plugin=LocalStrategyJev({}); plugin.reset(context); self.addCleanup(plugin.close)
        calls=[]
        def fake(url, request):
            calls.append(deepcopy(request))
            if url.endswith('/plan'): return qwen_response(request)
            reply=jev_response(request)
            result=reply['states'][0]['answers']['action']
            key=row['selection']['id']
            result.update(choice=key,value=key,probabilities={k:float(k==key) for k in result['probabilities']})
            return reply
        plugin._post=fake
        self.assertEqual(plugin.decide(context),native['action_id'])
        criteria=calls[-1]['states'][0]['questions']['action']['criteria']
        self.assertEqual(json.loads(criteria[row['selection']['id']]),row['selection']['action'])
        self.assertNotIn('authority_hash',json.dumps(calls))
        self.assertIn('authority_hash',native['action']['parameters'])

    def test_all_public_scenario_catalogs_validate_including_fire_and_board(self):
        count=0
        for path in sorted((ROOT/'examples/maps/scenarios').glob('*.json')):
            runtime=MatchRuntime(ScenarioBundle.from_scenario(load_scenario(path)),max_steps=1)
            try:
                runtime.reset()
                for side in ('red','blue'):
                    context=context_for(runtime.seat(side),42)
                    row=make_record(context,context.binding,match_id=path.stem,controller='rules',action_id=None,status='ok',elapsed=0.)
                    validate_record(row); count+=1
            finally: runtime.close()
        self.assertGreaterEqual(count,28)

    def test_initial_full_record_reads_without_rewriting_source(self):
        _, _, native, row = self.setup_record()
        legacy = deepcopy(row)
        legacy['selection']['action'] = deepcopy(native['action'])
        for item in legacy['policy_input']['legal_candidates']:
            if item['id'] == legacy['selection']['id']:
                item['action'] = deepcopy(native['action'])
        original = deepcopy(legacy)
        with self.assertRaisesRegex(ValueError, 'authority_hash'):
            validate_record(legacy)
        self.assertEqual(load_record(legacy), row)
        self.assertEqual(legacy, original)
        from scripts.inspect_decisions import main
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'old.jsonl'; output=Path(tmp)/'clip.jsonl'
            source.write_text(json.dumps(legacy)+'\n'); before=source.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                main(['--input',str(source),'--output',str(output)])
            self.assertEqual(source.read_bytes(),before)
            self.assertEqual(json.loads(output.read_text()),row)

    def test_unknown_links_wrong_location_and_hash_only_logs_stay_rejected(self):
        _, _, _, row = self.setup_record()
        for location in ('top','observation','candidate'):
            bad=deepcopy(row)
            if location=='top': bad['authority_hash']='value-not-to-print'
            elif location=='observation':bad['policy_input']['observation']['authority_hash']='value-not-to-print'
            else:bad['policy_input']['legal_candidates'][0]['action']['parameters']['unknown_hash']='value-not-to-print'
            with self.assertRaises(ValueError) as caught:load_record(bad)
            self.assertNotIn('value-not-to-print',str(caught.exception))
        with self.assertRaises(ValueError):load_record({'action_id':'old','side':'red'})

    def test_allowlist_is_action_specific_and_keeps_profile_weapon_path(self):
        from wargame_native_engine.rules.indirect_fire import load_legacy_indirect_fire_replay_identity
        fire={'side':'red','action_type':'FIRE','actor_id':1,'parameters':dict(load_legacy_indirect_fire_replay_identity(),weapon_id=2,profile_id='p',path=[101,102])}
        before=deepcopy(fire); projected=semantic_action(fire)
        self.assertEqual(fire,before)
        self.assertEqual(technical_field_paths(projected),[])
        for key in ('weapon_id','profile_id','path','indirect_fire_authority_version'):
            self.assertEqual(projected['parameters'][key],fire['parameters'][key])
        unrelated=deepcopy(fire);unrelated['action_type']='MOVE'
        self.assertTrue(technical_field_paths(semantic_action(unrelated)))

if __name__=='__main__':unittest.main()
