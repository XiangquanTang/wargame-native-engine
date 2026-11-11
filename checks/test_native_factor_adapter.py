"""Native inputs reuse numeric rules without impersonating Source observations."""
from copy import deepcopy
import json
import math
from pathlib import Path
import unittest

from wargame_native_engine.analysis.analyzer import EightFactorAnalyzer, GreenFrameAnalysisInput
from wargame_native_engine.analysis.native_adapter import adapt_native_unit, NATIVE_FACTS_SCHEMA
from wargame_native_engine.analysis.native_analyzer import NativeEightFactorAnalyzer
from wargame_native_engine.analysis.serialization import frame_analysis_to_record, frame_analysis_from_record

FIXTURE = Path(__file__).parent / 'fixtures/native_factor_audit_samples.json'


class NativeFactorAdapterTests(unittest.TestCase):
    def setUp(self):
        self.samples = json.loads(FIXTURE.read_text())['samples']
        self.unit = deepcopy(self.samples[-1]['unit'])

    def analyze(self, unit=None):
        return NativeEightFactorAnalyzer().analyze_green_frame(GreenFrameAnalysisInput(
            704, {'operators': [self.unit if unit is None else unit], 'passengers': []},
            frame_hash='raw-frame-hash', scenario_hash='scenario-hash'))

    def factor(self, name, unit=None):
        return next(f for f in self.analyze(unit).unit_analyses[0].factor_values if f.factor_name == name)

    def test_actual_samples_deduplicate_current_pools_not_initial_or_per_weapon(self):
        for sample, total in zip(self.samples, (30, 44, 43)):
            with self.subTest(frame=sample['frame_index']):
                adapted = adapt_native_unit(sample['unit'])
                self.assertEqual(sum(adapted.unit['remain_bullet_nums'].values()), total)
                self.assertAlmostEqual(self.factor('ammo', sample['unit']).value,
                                       min(1.25, .55 + math.log1p(total) / 5))

    def test_zero_ammo_is_observed_zero_stock_not_missing(self):
        inv = self.unit['extra']['native']['weapon_inventory']
        inv['ammunition_pools'] = {k: 0 for k in inv['ammunition_pools']}
        self.assertEqual(self.factor('ammo').value, .3)
        self.assertEqual(self.factor('ammo').status.value, 'OBSERVED')

    def test_incomplete_or_ambiguous_inventory_fails_closed(self):
        for change in ('missing_pool', 'unknown_binding', 'self_munition', 'wrong_id',
                       'unbound_nonzero', 'int_key_collision', 'bool_stock', 'missing_weapon'):
            unit = deepcopy(self.unit)
            inv = unit['extra']['native']['weapon_inventory']
            if change == 'missing_pool': del inv['ammunition_pools']['100']
            if change == 'unknown_binding': inv['weapons'][0]['resource_policy'] = 'UNKNOWN'
            if change == 'self_munition': inv['weapons'][0]['resource_policy'] = 'SELF_AS_MUNITION'
            if change == 'wrong_id': inv['operator_id'] = 76
            if change == 'unbound_nonzero': inv['ammunition_pools']['102'] = 1
            if change == 'int_key_collision': inv['ammunition_pools'][0] = 40
            if change == 'bool_stock': inv['ammunition_pools']['0'] = False
            if change == 'missing_weapon': inv['weapons'].pop()
            with self.subTest(change=change):
                self.assertIsNone(self.factor('ammo', unit).value)

    def test_mobility_exact_bool_and_missing_historical_flag(self):
        native = self.unit['extra']['native']
        native['movement_locked'] = True
        expected = .95 * .85
        self.assertAlmostEqual(self.factor('mobility').value, expected)
        self.assertIs(adapt_native_unit(self.unit).unit['can_to_move'], False)
        for invalid in (0, 1, 'false', None):
            native['movement_locked'] = invalid
            self.assertIsNone(self.factor('mobility').value)
        del native['movement_locked']
        self.assertIsNone(self.factor('mobility').value)

    def test_boarding_is_separate_from_transition_lock(self):
        self.unit['extra']['native']['movement_locked'] = False
        self.unit['on_board'] = True
        self.assertAlmostEqual(self.factor('mobility').value, .95 * .9)

    def facts(self):
        facts = {'schema_version': NATIVE_FACTS_SCHEMA, 'movement_locked': False,
                 'boarding': {'on_board': False},
                 'fatigue': {'level': None, 'native_level': 0, 'applicability': 'NOT_APPLICABLE'},
                 'suppression': {'active': False, 'remaining_ms': 0},
                 'close_combat': False}
        self.unit['extra']['native']['analysis_facts'] = facts
        return facts

    def test_new_facts_ignore_stale_template_and_suppression_applies_once(self):
        facts = self.facts()
        self.unit.update(keep=1, tire=2, lose_control=True, can_to_move=False)
        adapted = adapt_native_unit(self.unit)
        self.assertIs(adapted.unit['keep'], False)
        self.assertIs(adapted.unit['lose_control'], False)
        self.assertEqual(adapted.unit['tire'], 0)
        self.assertEqual(self.factor('status').value, 1.0)
        facts['suppression'] = {'active': True, 'remaining_ms': 150000}
        self.assertEqual(self.factor('status').value, .2)
        self.assertNotEqual(self.factor('status').value, .13)
        facts['suppression'] = {'active': True, 'remaining_ms': 1}
        self.assertEqual(self.factor('status').value, .2)
        facts['suppression'] = {'active': False, 'remaining_ms': 0}
        self.assertEqual(self.factor('status').value, 1.0)
        facts['fatigue']['native_level'] = 2
        facts['close_combat'] = True
        self.assertAlmostEqual(self.factor('status').value, .75 * 1.05)
        ev = adapt_native_unit(self.unit).evidence[-1]
        self.assertNotIn('unit.keep', ev.source_paths)
        self.assertNotIn('unit.lose_control', ev.source_paths)
        self.assertEqual(ev.raw_value['policy_constants'], {'keep': False})
        self.assertIn('native-status:', self.factor('status').formula_id)
        del facts['suppression']
        self.assertIsNone(self.factor('status').value)
        self.assertIsNone(self.analyze().unit_analyses[0].effective_power)

    def test_complete_native_facts_produce_valid_power_and_dead_zero(self):
        self.facts()
        result = self.analyze()
        unit = result.unit_analyses[0]
        self.assertTrue(unit.valid)
        self.assertEqual(unit.observed_factor_count, 8)
        self.assertGreater(unit.effective_power, 0)
        self.assertEqual(frame_analysis_from_record(frame_analysis_to_record(result))[0], result)
        self.unit['blood'] = 0
        self.assertEqual(self.analyze().unit_analyses[0].effective_power, 0)

    def test_invalid_or_inconsistent_facts_do_not_borrow_legacy_fields(self):
        facts = self.facts()
        facts['movement_locked'] = 0
        self.assertIsNone(self.factor('mobility').value)
        facts['suppression']['active'] = True
        self.assertNotIn('lose_control', adapt_native_unit(self.unit).unit)
        facts['fatigue']['native_level'] = False
        self.assertNotIn('tire', adapt_native_unit(self.unit).unit)
        facts['schema_version'] = 'unsupported'
        self.assertIsNone(self.factor('mobility').value)

    def test_distinct_identity_original_hash_detached_provenance_roundtrip(self):
        before = deepcopy(self.unit)
        result = self.analyze()
        self.assertEqual(self.unit, before)
        self.assertNotEqual(result.provenance.model_hash, EightFactorAnalyzer().model_hash)
        self.assertEqual(result.provenance.frame_hash, 'raw-frame-hash')
        ev = next(x for x in result.unit_analyses[0].factor_evidence if x.name == 'ammo')
        self.assertEqual(ev.raw_value['kind'], 'DERIVED_NATIVE')
        self.assertNotIn('unit.remain_bullet_nums', ev.source_paths)
        self.assertIn('unit.extra.native.weapon_inventory.ammunition_pools', ev.source_paths)
        self.assertEqual(frame_analysis_from_record(frame_analysis_to_record(result))[0], result)
        ev.raw_value['inputs']['unit.extra.native.weapon_inventory.ammunition_pools']['0'] = -1
        self.assertEqual(self.unit, before)

    def test_fixed_identity_and_unknown_status_consistency(self):
        with self.assertRaises(TypeError):
            NativeEightFactorAnalyzer(model_id=EightFactorAnalyzer().model_id)
        for sample in self.samples:
            result = self.analyze(sample['unit'])
            for unit in result.unit_analyses:
                for ev, factor in zip(unit.factor_evidence, unit.factor_values):
                    self.assertEqual(ev.status, factor.status)
                    if ev.status.value == 'UNKNOWN':
                        self.assertIsNone(factor.value)
                        self.assertIsNone(unit.effective_power)
                        self.assertFalse(unit.valid)

    def test_compact_evidence_contains_only_used_inventory_fields(self):
        inv = self.unit['extra']['native']['weapon_inventory']
        inv['unused_large_profile'] = 'not evidence'
        inv['weapons'][0]['unrelated_payload'] = 'not evidence'
        adapted = adapt_native_unit(self.unit)
        for ev in adapted.evidence:
            if ev.name not in {'ammo', 'mobility', 'status'}:
                self.assertNotIsInstance(ev.raw_value, dict)
            else:
                self.assertNotIn('policy_hash', ev.raw_value)
        ammo = next(ev for ev in adapted.evidence if ev.name == 'ammo')
        bindings = ammo.raw_value['inputs']['unit.extra.native.weapon_inventory.weapons']
        self.assertEqual(set(bindings[0]), {'weapon_id', 'resource_policy', 'ammunition_key'})
        self.assertNotIn('unused_large_profile', json.dumps(ammo.raw_value))
        self.assertNotIn('unrelated_payload', json.dumps(ammo.raw_value))

    def test_duplicate_ids_and_invalid_ids_rejected(self):
        for frame in ({'operators': [self.unit], 'passengers': [self.unit]},
                      {'operators': [{**self.unit, 'obj_id': True}]}):
            with self.assertRaises((TypeError, ValueError)):
                NativeEightFactorAnalyzer().analyze_green_frame(GreenFrameAnalysisInput(0, frame))

    def test_missing_native_inputs_never_read_template_as_current(self):
        self.unit['extra'] = {}
        self.unit.update(remain_bullet_nums={'0': 999}, can_to_move=True, keep=0, tire=0, lose_control=0)
        for name in ('ammo', 'mobility', 'status'):
            self.assertIsNone(self.factor(name).value)


if __name__ == '__main__':
    unittest.main()
