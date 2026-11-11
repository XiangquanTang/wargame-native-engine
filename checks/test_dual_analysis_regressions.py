"""Independent edge checks for the explicit Source/Native analysis boundary."""
from copy import deepcopy
import math
import json
from pathlib import Path
import unittest

from wargame_native_engine.analysis.analyzer import EightFactorAnalyzer, GreenFrameAnalysisInput
from wargame_native_engine.analysis.native_analyzer import NativeEightFactorAnalyzer
from wargame_native_engine.analysis.native_adapter import adapt_native_unit
from wargame_native_engine.analysis.semantic_identity import ANALYZER_MODEL_HASH


def _unit():
    return {
        'obj_id': 0, 'color': 0, 'value': 10, 'blood': 100, 'max_blood': 100,
        'carry_weapon_ids': [4, 29], 'remain_bullet_nums': {'initial': 900},
        'weapon_cool_time': 0, 'weapon_unfold_state': 1, 'move_state': 0,
        'stop': 0, 'can_to_move': True, 'on_board': 0, 'see_enemy_bop_ids': [],
        'keep': 0, 'tire': 0, 'lose_control': False, 'close_combat': 0,
        'extra': {'native': {
            'movement_locked': True,
            'weapon_inventory': {
                'operator_id': 0, 'ammunition_pools': {'0': 3},
                'weapons': [
                    {'weapon_id': 4, 'ammunition_key': '0', 'resource_policy': 'AMMUNITION_KEY'},
                    {'weapon_id': 29, 'ammunition_key': '0', 'resource_policy': 'AMMUNITION_KEY'},
                ],
            },
            'analysis_facts': {
                'schema_version': 'native-analysis-facts/1.0',
                'movement_locked': True,
                'boarding': {'on_board': False},
                'fatigue': {'native_level': 0, 'applicability': 'NOT_APPLICABLE', 'level': None},
                'suppression': {'active': False, 'remaining_ms': 0},
                'close_combat': False,
                'lose_control': {'status': 'UNKNOWN', 'reason': 'NOT_MODELED'},
            },
        }},
    }


def _analyze(unit, native=True):
    analyzer = NativeEightFactorAnalyzer() if native else EightFactorAnalyzer()
    return analyzer.analyze_green_frame(GreenFrameAnalysisInput(
        frame_index=0, frame={'operators': [unit], 'passengers': []},
        frame_hash='original-frame', scenario_hash='original-scenario',
    ))


def _factor(result, name):
    return next(f for f in result.unit_analyses[0].factor_values if f.factor_name == name)


class DualAnalysisRegressionTests(unittest.TestCase):
    def test_current_shared_pool_is_counted_once_not_initial_stock(self):
        result = _analyze(_unit())
        self.assertAlmostEqual(_factor(result, 'ammo').value, .55 + math.log(4) / 5)
        self.assertEqual(_factor(result, 'mobility').value, .85)
        self.assertEqual(result.provenance.frame_hash, 'original-frame')
        self.assertEqual(result.provenance.scenario_hash, 'original-scenario')

    def test_ambiguous_or_invalid_ammo_never_becomes_known(self):
        for change in ('missing_pool', 'self_munition', 'wrong_owner', 'extra_stock', 'bool_stock', 'missing_weapon'):
            with self.subTest(change=change):
                unit = _unit()
                inventory = unit['extra']['native']['weapon_inventory']
                if change == 'missing_pool':
                    inventory['ammunition_pools'] = {}
                elif change == 'self_munition':
                    inventory['weapons'][0]['resource_policy'] = 'SELF_AS_MUNITION'
                elif change == 'wrong_owner':
                    inventory['operator_id'] = 1
                elif change == 'extra_stock':
                    inventory['ammunition_pools']['unknown'] = 1
                elif change == 'bool_stock':
                    inventory['ammunition_pools']['0'] = False
                else:
                    inventory['weapons'].pop()
                factor = _factor(_analyze(unit), 'ammo')
                self.assertEqual(factor.status.value, 'UNKNOWN')
                self.assertIsNone(factor.value)

    def test_lock_requires_exact_boolean_and_never_falls_back_from_bad_schema(self):
        for lock, expected in ((True, .85), (False, 1.0), (0, None), (1, None), (None, None)):
            with self.subTest(lock=lock):
                unit = _unit()
                unit['extra']['native']['analysis_facts']['movement_locked'] = lock
                self.assertEqual(_factor(_analyze(unit), 'mobility').value, expected)
        unit = _unit()
        unit['extra']['native']['analysis_facts']['schema_version'] = 'unknown-future-version'
        self.assertIsNone(_factor(_analyze(unit), 'mobility').value)
        del unit['extra']['native']['analysis_facts']
        del unit['extra']['native']['movement_locked']
        self.assertIsNone(_factor(_analyze(unit), 'mobility').value)

    def test_suppression_maps_loss_control_once_and_ignores_stale_source_state(self):
        unit = _unit()
        self.assertEqual(_factor(_analyze(unit, native=False), 'status').value, 1.0)
        unit['keep'] = True
        unit['lose_control'] = True
        self.assertEqual(_factor(_analyze(unit), 'status').value, 1.0)
        facts = unit['extra']['native']['analysis_facts']
        facts['suppression'] = {'active': True, 'remaining_ms': 1000}
        # The two historical Source flags must not double-penalize one Native fact.
        result = _analyze(unit)
        self.assertEqual(_factor(result, 'status').value, .2)
        self.assertNotEqual(_factor(result, 'status').value, .65 * .2)
        self.assertNotIn('unit.keep', _factor(result, 'status').source_paths)
        self.assertNotIn('unit.lose_control', _factor(result, 'status').source_paths)
        facts['fatigue']['native_level'] = 2
        facts['close_combat'] = True
        self.assertAlmostEqual(_factor(_analyze(unit), 'status').value, .75 * .2 * 1.05)
        facts['fatigue']['native_level'] = 0
        facts['close_combat'] = False
        facts['suppression'] = {'active': False, 'remaining_ms': 0}
        self.assertEqual(_factor(_analyze(unit), 'status').value, 1.0)

    def test_missing_or_contradictory_current_suppression_is_unknown(self):
        unit = _unit()
        facts = unit['extra']['native']['analysis_facts']
        facts['suppression'] = {'active': True, 'remaining_ms': 0}
        self.assertIsNone(_factor(_analyze(unit), 'status').value)
        del unit['extra']['native']['analysis_facts']
        # Source keep/lose_control are present but cannot prove Native current state.
        self.assertIsNone(_factor(_analyze(unit), 'status').value)
        self.assertIsNone(_analyze(unit).unit_analyses[0].effective_power)

    def test_invalid_native_codes_fail_closed_and_known_dead_power_is_zero(self):
        for key, factor_name in (('move_state', 'mobility'), ('weapon_unfold_state', 'readiness')):
            unit = _unit()
            unit[key] = 99
            self.assertIsNone(_factor(_analyze(unit), factor_name).value)
        unit = _unit()
        unit['blood'] = 0
        result = _analyze(unit)
        self.assertEqual(result.unit_analyses[0].effective_power, 0.0)
        self.assertEqual(result.advantage.normalized_advantage, 0.0)

    def test_source_model_and_false_zero_convention_remain_unchanged(self):
        self.assertEqual(ANALYZER_MODEL_HASH,
            '77e0113c896c813033528d43bc6c132e296cb91eacf9a153d436cb9fe20bc577')
        unit = _unit()
        unit['can_to_move'] = False
        source = _analyze(unit, native=False)
        self.assertEqual(_factor(source, 'mobility').value, .85)
        unit['can_to_move'] = 0
        self.assertEqual(_factor(_analyze(unit, native=False), 'mobility').value, 1.0)
        self.assertNotEqual(source.provenance.model_hash, _analyze(unit).provenance.model_hash)

    def test_real_fire_exports_suppression_and_expiry_without_double_penalty(self):
        from wargame_native_engine.backends.native import NativeEngineBackend
        from wargame_native_engine.compat.source_frame.contract import ANALYSIS_SOURCE_CONTRACT
        from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
        from wargame_native_engine.core.action import EngineAction, ActionType
        from wargame_native_engine.io.scenario import scenario_from_dict
        from wargame_native_engine.session.game_session import GameSession
        from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment

        # Adjacent, unobstructed synthetic units; seed 3 produces suppression,
        # not a hit-point loss. No runtime or projected facts are injected.
        path = Path(__file__).resolve().parents[1] / 'examples/source_movement_scenario.json'
        record = json.loads(path.read_text())
        record['operators'][1].update(x=2, current_hex=102)
        for cell in record['map']['hexes']:
            cell.update(elev=0, cond=0)
        scenario = scenario_from_dict(record)
        env = SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(scenario)),
            template=source_frame_template_for_scenario(scenario),
            source_contract=ANALYSIS_SOURCE_CONTRACT,
        )

        def assert_status(frame, active, remaining_ms, expected):
            target = next(unit for unit in frame['operators'] if unit['obj_id'] == 2)
            self.assertEqual(target['blood'], 3)
            self.assertEqual(target['extra']['native']['analysis_facts']['suppression'],
                             {'active': active, 'remaining_ms': remaining_ms})
            self.assertEqual(_factor(_analyze(target), 'status').value, expected)

        assert_status(env.setup(seed=3), False, 0, 1.0)
        frame, _ = env.step([EngineAction('red', 1, ActionType.SHOOT,
                                         target_id=2, parameters={'weapon_id': 36})],
                            delta_time_ms=1)
        self.assertTrue(env.last_action_results[0]['accepted'])
        assert_status(frame, True, 149999, .2)
        frame, _ = env.step([], delta_time_ms=149998)
        assert_status(frame, True, 1, .2)
        frame, _ = env.step([], delta_time_ms=1)
        assert_status(frame, False, 0, 1.0)

    def test_zero_id_and_detached_inputs(self):
        unit = _unit()
        original = deepcopy(unit)
        result = _analyze(unit)
        self.assertEqual(result.unit_analyses[0].operator_id, 0)
        adapted = adapt_native_unit(unit)
        adapted.unit['carry_weapon_ids'].append(999)
        ammo = next(e for e in adapted.evidence if e.name == 'ammo')
        ammo.raw_value['inputs']['unit.extra.native.weapon_inventory.ammunition_pools']['0'] = 100
        self.assertEqual(unit, original)


if __name__ == '__main__':
    unittest.main()
