"""Firing table inputs are exact grades, never raw meters or rounded values."""
from dataclasses import replace
import unittest
import contextlib
import io
import json
import tempfile
from pathlib import Path

from test_fire_effect_query import fixture
import test_fire_effect_query as query_tests
from test_direct_fire_range import TABLES, shoot
from wargame_native_engine.adjudication import AdjudicationTableSet, DirectFireAdjudicator, FixedDiceRoller
from wargame_native_engine.adjudication.replay import direct_fire_request_from_dict, direct_fire_request_to_dict
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.visibility import HexCell, HexMapProfile


class FireElevationUnitsTests(unittest.TestCase):
    def engine(self, kind='METERS', difference=10, unit=10):
        value = fixture()
        value['map'].update(elevation_kind=kind, elevation_unit_m=unit)
        value['map']['hexes'][2]['elev'] = difference
        # Keep the unmodified 200 m hex geometry; shooter101 -> target103 is 2 hexes.
        target = value['operators'][1]
        target.update(current_hex=103, x=3, y=1)
        value['deployment']['positions']['blue']['2'] = [3, 1]
        engine = NativeEngine(scenario_from_dict(value))
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        return engine

    def request(self, engine):
        return engine._direct_fire_request(shoot(), engine._world.operators[1], engine._world.operators[2])

    def test_meter_and_level_maps_have_same_request_query_and_execution(self):
        meter, level = self.engine(), self.engine('LEVEL', 1)
        requests = [self.request(e) for e in (meter, level)]
        self.assertEqual([r.distance for r in requests], [2, 2])
        self.assertEqual([r.elevation_difference for r in requests], [1, 1])
        tables = AdjudicationTableSet(TABLES)
        self.assertEqual(tables.elevation_modifier(1, 2).value, -2)
        for request in requests:
            staged = DirectFireAdjudicator(tables, FixedDiceRoller([6] * 10)).stage(request)
            self.assertIsNone(staged.error)
            event = next(e for e in staged.events if e.event_type == EventType.ATTACK_LEVEL_MODIFIED)
            self.assertEqual(event.data['elevation_modifier'], -2)
            restored = direct_fire_request_from_dict(direct_fire_request_to_dict(request))
            self.assertEqual(restored.elevation_difference, 1)
        queries = [query_tests.FireEffectQueryTests.query(self, e) for e in (meter, level)]
        self.assertEqual([q['status'] for q in queries], ['RESOLVED', 'RESOLVED'])
        self.assertEqual(queries[0]['damage_distribution'], queries[1]['damage_distribution'])
        results = [e.step([shoot()], delta_time_ms=1) for e in (meter, level)]
        self.assertTrue(all(r.decisions[0].accepted for r in results), [r.decisions for r in results])
        self.assertEqual(meter._world.operators[2].blood, level._world.operators[2].blood)
        self.assertEqual(meter._source_map.cell(103).elevation, 10)  # Raw map preserved.

    projection = query_tests.FireEffectQueryTests.projection

    def test_guided_request_and_query_normalize_shooter_target_not_guider_height(self):
        from wargame_native_engine.adjudication.fire_effect_query import FireEffectQuery
        value = query_tests.guided_fixture()
        value['map'].update(elevation_kind='METERS', elevation_unit_m=10)
        value['map']['hexes'][-1]['elev'] = 10
        engine = NativeEngine(scenario_from_dict(value))
        world = engine.reset(seed=42)
        self.addCleanup(engine.close)
        capability = engine.get_guided_fire_capabilities('red')[0]
        guided = engine.build_guided_fire_request(capability)
        request = engine._guided_direct_fire_request(guided, world)
        self.assertEqual(request.elevation_difference, 1)
        self.assertEqual(request.distance, 5)
        query = FireEffectQuery(1, 73, target_id=2, guider_id=3, fire_mode='GUIDED',
            target_profile=dict(query_tests.PROFILE, domain='VEHICLE', armor_class='MEDIUM'))
        result = query_tests.FireEffectQueryTests.query(self, engine, query)
        self.assertEqual(result['status'], 'RESOLVED', result)
        staged = DirectFireAdjudicator(AdjudicationTableSet(TABLES), FixedDiceRoller([6] * 10)).stage(request)
        self.assertIsNone(staged.error)
        event = next(e for e in staged.events if e.event_type == EventType.ATTACK_LEVEL_MODIFIED)
        self.assertEqual(event.data['elevation_modifier'], -1)

    def test_standalone_cli_serializes_grade_and_replays_exactly(self):
        from wargame_native_engine.cli.resolve_direct_fire import main
        from wargame_native_engine.adjudication.replay import replay_direct_fire
        request = self.request(self.engine())
        operators = [request.attacker.to_dict(), request.target.to_dict()]
        for operator in operators:
            operator['raw_attributes']['hex_id'] = operator['current_hex']
        scenario = {
            'schema_version': 'native-direct-fire-scenario/1.0', 'scenario_id': 'meter-grade-cli',
            'operators': operators,
            'weapons': [{'weapon_id': 36, 'weapon_class': request.weapon.weapon_class,
                         'maximum_range': 10, 'target_domains': ['PERSONNEL', 'VEHICLE']}],
            'map': {'elevation_kind': 'METERS', 'elevation_unit_m': 10,
                    'cells': [{'hex_id': 101, 'elevation': 0},
                              {'hex_id': 102, 'elevation': 0},
                              {'hex_id': 103, 'elevation': 10}]},
        }
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'scenario.json'
            output = Path(folder) / 'replay'
            source.write_text(json.dumps(scenario))
            with contextlib.redirect_stdout(io.StringIO()) as report:
                code = main(['--scenario', str(source), '--attacker', '1', '--target', '2',
                             '--weapon', '36', '--table-set', str(TABLES), '--output', str(output)])
            self.assertEqual(code, 0, report.getvalue())
            self.assertEqual(json.loads((output / 'request.json').read_text())['elevation_difference'], 1)
            verified = replay_direct_fire(output, tables=AdjudicationTableSet(TABLES))
            self.assertTrue(verified.matches)
            scenario['weapons'][0]['maximum_range'] = 1
            scenario['map']['cells'][-1]['elevation'] = 15
            source.write_text(json.dumps(scenario))
            overshoot = Path(folder) / 'overshoot'
            with contextlib.redirect_stdout(io.StringIO()) as report:
                code = main(['--scenario', str(source), '--attacker', '1', '--target', '2',
                             '--weapon', '36', '--table-set', str(TABLES), '--output', str(overshoot)])
            self.assertEqual(code, 0, report.getvalue())
            self.assertIsNone(json.loads((overshoot / 'request.json').read_text())['elevation_difference'])
            self.assertTrue(replay_direct_fire(overshoot, tables=AdjudicationTableSet(TABLES)).matches)

    def test_zero_downhill_and_other_declared_scales(self):
        for difference, unit, expected in [(0, 10, 0), (-10, 10, -1), (20, 20, 1), (0.2, 0.1, 2)]:
            with self.subTest(difference=difference, unit=unit):
                profile = HexMapProfile({101: HexCell(101, 0), 103: HexCell(103, difference)},
                                        elevation_kind='METERS', elevation_unit_m=unit)
                self.assertEqual(profile.elevation_level_difference(101, 103), expected)
        tables = AdjudicationTableSet(TABLES)
        self.assertEqual(tables.elevation_modifier(-1, 2).value, 0)
        self.assertEqual(tables.elevation_modifier(0, 2).value, 0)

    def test_fractional_unknown_units_and_true_missing_cells_are_not_guessed(self):
        cells = {101: HexCell(101, 0), 103: HexCell(103, 15)}
        for kind, unit in [('METERS', 10), ('METERS', None), ('METERS', 0), (None, 10)]:
            with self.subTest(kind=kind, unit=unit), self.assertRaises(ValueError):
                HexMapProfile(cells, elevation_kind=kind, elevation_unit_m=unit).elevation_level_difference(101, 103)
        tables = AdjudicationTableSet(TABLES)
        for difference, distance in [(9, 2), (1, 13), (1, 0)]:
            with self.subTest(difference=difference, distance=distance), self.assertRaisesRegex(ValueError, 'outside table'):
                tables.elevation_modifier(difference, distance)
        request = self.request(self.engine())
        for invalid in (1.5, True, '1', 1.0):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, 'integer elevation-level'):
                    replace(request, elevation_difference=invalid)
                record = direct_fire_request_to_dict(request)
                record['elevation_difference'] = invalid
                with self.assertRaisesRegex(ValueError, 'integer elevation-level'):
                    direct_fire_request_from_dict(record)
                with self.assertRaisesRegex(ValueError, 'integer elevation-level'):
                    tables.elevation_modifier(invalid, 2)

    def test_fractional_ordinary_overshoot_spends_shot_without_elevation_table(self):
        value = fixture()
        value['map'].update(elevation_kind='METERS', elevation_unit_m=10)
        value['map']['hexes'][-1]['elev'] = 15
        value['metadata']['source_compatible']['weapons']['36']['maximum_range'] = 4
        engine = NativeEngine(scenario_from_dict(value))
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        request = self.request(engine)
        self.assertIsNone(request.elevation_difference)
        self.assertIsNone(direct_fire_request_from_dict(direct_fire_request_to_dict(request)).elevation_difference)
        result = query_tests.FireEffectQueryTests.query(self, engine)
        self.assertEqual(result['status'], 'RESOLVED')
        self.assertEqual(result['damage_probability'], 0)
        before = engine.snapshot()
        step = engine.step([shoot()], delta_time_ms=1)
        self.assertTrue(step.decisions[0].accepted, step.decisions)
        self.assertEqual(engine.snapshot()['rng'], before['rng'])
        self.assertEqual(engine._world.operators[1].raw_attributes['ammunition']['0'], 19)
        self.assertGreater(engine._world.operators[1].weapon_cooldown_remaining_ms, 0)
        self.assertEqual(engine._world.operators[2].blood, 3)
        with self.assertRaisesRegex(ValueError, 'integer elevation-level'):
            replace(request, distance=2)

    def test_fractional_query_is_structured_unresolved_without_mutation(self):
        engine = self.engine(difference=15)
        before = engine.snapshot()
        result = query_tests.FireEffectQueryTests.query(self, engine)
        self.assertEqual(result['status'], 'UNRESOLVED')
        self.assertEqual(result['reason_code'], 'ELEVATION_LEVEL_UNRESOLVED')
        self.assertEqual(engine.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
