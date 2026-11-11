"""Tactical overlays retain LOS units and frozen side-safe projection semantics."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from test_fire_effect_query import fixture
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.region_visibility import RegionVisibilityQuery, RegionRectangle, VisibilityTargetProfile
from wargame_native_engine.io.scenario import scenario_from_dict

ROOT = Path(__file__).resolve().parents[1]


class TacticalOverlayUnitsTests(unittest.TestCase):
    def engine(self, value=None):
        engine = NativeEngine(scenario_from_dict(value or fixture()))
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        return engine

    def projection(self, engine):
        observation = engine.observe('red').to_dict()
        visible = {r['operator_id'] for r in observation['visible_enemies']}
        source = {'operators': [
            {'obj_id': u.operator_id, 'color': 0 if u.side == 'red' else 1, 'cur_hex': u.current_hex,
             **({'carry_weapon_ids': list(u.weapon_ids)} if u.side == 'red' else {})}
            for u in engine._world.operators.values() if u.side == 'red' or u.operator_id in visible
        ]}
        return observation, source, [a.to_dict() for a in engine.valid_actions('red')]

    def test_source_color_is_required_and_owner_metadata_is_not_authority(self):
        engine = self.engine()
        observation, source, actions = self.projection(engine)
        query = lambda frame: engine.query_unit_tactical_overlay_from_projection(
            'red', 1, observation, frame, actions).to_record()
        expected = query(source)
        for owner in (None, 22, 'blue', 'red', {'group': 7}):
            frame = deepcopy(source)
            frame['operators'][0]['owner'] = owner
            self.assertEqual(query(frame), expected)
        for color in (None, True, False, 0.0, '0', 1, -1, 2):
            frame = deepcopy(source)
            frame['operators'][0].update(color=color, owner='red')
            with self.subTest(color=color), self.assertRaisesRegex(ValueError, 'controlled unit evidence'):
                query(frame)
        frame = deepcopy(source)
        frame['operators'][0].pop('color')
        frame['operators'][0]['owner'] = 'red'
        with self.assertRaisesRegex(ValueError, 'controlled unit evidence'):
            query(frame)

    def test_projection_identity_remains_exact_unique_and_side_bound(self):
        engine = self.engine()
        observation, source, actions = self.projection(engine)
        for field, key, container in [('obj_id', 'operators', source),
                                      ('operator_id', 'friendly_operators', observation)]:
            for identifier in (True, 1.0):
                forged = deepcopy(container)
                forged[key][0][field] = identifier
                with self.subTest(field=field, identifier=identifier), self.assertRaisesRegex(ValueError, 'exact int'):
                    engine.query_unit_tactical_overlay_from_projection('red', 1,
                        forged if container is observation else observation,
                        forged if container is source else source, actions)
            forged = deepcopy(container)
            forged[key].append(deepcopy(forged[key][0]))
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                engine.query_unit_tactical_overlay_from_projection('red', 1,
                    forged if container is observation else observation,
                    forged if container is source else source, actions)
        forged = deepcopy(observation)
        forged['friendly_operators'][0]['side'] = 'blue'
        with self.assertRaisesRegex(ValueError, 'not controlled'):
            engine.query_unit_tactical_overlay_from_projection('red', 1, forged, source, actions)

    def test_missing_own_source_and_forged_visible_target_cannot_supply_evidence(self):
        engine = self.engine()
        observation, source, actions = self.projection(engine)
        missing = deepcopy(source)
        missing['operators'] = [value for value in missing['operators'] if value['obj_id'] != 1]
        with self.assertRaisesRegex(ValueError, 'controlled unit evidence'):
            engine.query_unit_tactical_overlay_from_projection('red', 1, observation, missing, actions)
        targets = [value['action']['target_id'] for value in actions
                   if value['action']['action_type'] == 'SHOOT' and value['action'].get('target_id') is not None]
        self.assertTrue(targets)
        for color in (0, True, 1.0, '1', None):
            forged = deepcopy(source)
            for value in forged['operators']:
                if value['obj_id'] in targets:
                    value['color'] = color
            with self.subTest(color=color), self.assertRaisesRegex(ValueError, 'absent from visible'):
                engine.query_unit_tactical_overlay_from_projection('red', 1, observation, forged, actions)

    def test_meter_level_overlay_parity_and_raw_map_untouched(self):
        outputs = []
        for kind, height in [('METERS', 10), ('LEVEL', 1)]:
            value = fixture()
            value['map'].update(elevation_kind=kind, elevation_unit_m=10)
            value['map']['hexes'][-1]['elev'] = height
            engine = self.engine(value)
            before = engine.snapshot()
            outputs.append(engine.query_unit_tactical_overlay('red', 1).to_record())
            self.assertEqual(before, engine.snapshot())
            self.assertEqual(engine._source_map.cell(106).elevation, height)
        self.assertEqual(outputs[0], outputs[1])
        self.assertIn(106, outputs[0]['visibility_field']['hex_ids'])

    def test_frozen_projection_excludes_occupancy_and_ignores_target_free_shoot(self):
        engine = self.engine()
        observation, source, actions = self.projection(engine)
        expected = engine.query_unit_tactical_overlay_from_projection('red', 1, observation, source, actions).to_record()
        actions.append({'action': {'side': 'red', 'actor_id': 1, 'action_type': 'SHOOT',
                                  'target_id': None, 'parameters': {'weapon_id': 36, 'target_hex': 105}}})
        engine._world = None  # Frozen path must not inspect live enemies or current world.
        actual = engine.query_unit_tactical_overlay_from_projection('red', 1, observation, source, actions).to_record()
        self.assertEqual(expected, actual)
        with self.assertRaisesRegex(ValueError, 'not controlled'):
            engine.query_unit_tactical_overlay_from_projection('red', 2, observation, source, actions)

    def test_drone_visibility_matches_authoritative_ground_region_query(self):
        value = json.loads((ROOT / 'examples/maps/scenarios/drone.json').read_text())
        engine = self.engine(value)
        observation, source, actions = self.projection(engine)
        before = engine.snapshot()
        overlay = engine.query_unit_tactical_overlay_from_projection('red', 1, observation, source, actions).to_record()
        for target_class in ('PERSONNEL', 'VEHICLE'):
            query = RegionVisibilityQuery(RegionRectangle(1, 1, len(value["map"]["hexes"]), 1),
                VisibilityTargetProfile(target_class, False, 0))
            region = engine.query_region_visibility_from_projection('red', 1, observation, source, query).to_dict()
            self.assertEqual(overlay['visibility_field']['by_target_domain'][target_class],
                             [row[0] for row in region['cells'] if row[1]])
        self.assertEqual(before, engine.snapshot())
        self.assertEqual(overlay['weapon_envelope']['weapons'], [])

    def test_frozen_air_identity_overrides_stale_template_subtype(self):
        value = json.loads((ROOT / 'examples/maps/scenarios/drone.json').read_text())
        engine = self.engine(value)
        engine._world.operators[1].subtype = 'source-3-0'
        engine._world.operators[1].altitude = 200
        observation, source, actions = self.projection(engine)
        engine._world = None
        overlay = engine.query_unit_tactical_overlay_from_projection('red', 1, observation, source, actions).to_record()
        for target_class in ('PERSONNEL', 'VEHICLE'):
            query = RegionVisibilityQuery(RegionRectangle(1, 1, len(value['map']['hexes']), 1),
                VisibilityTargetProfile(target_class, False, 0))
            region = engine.query_region_visibility_from_projection('red', 1, observation, source, query).to_dict()
            self.assertEqual(overlay['visibility_field']['by_target_domain'][target_class],
                             [row[0] for row in region['cells'] if row[1]])

    def test_missing_authoritative_los_is_an_error_not_an_empty_mask(self):
        engine = self.engine()
        observation, source, actions = self.projection(engine)
        engine.map_runtime_data = SimpleNamespace(visibility=SimpleNamespace(
            line_of_sight=Mock(side_effect=ValueError('missing declared LOS channel'))))
        with self.assertRaisesRegex(ValueError, 'missing declared LOS channel'):
            engine.query_unit_tactical_overlay_from_projection('red', 1, observation, source, actions)
        engine.map_runtime_data = None


if __name__ == '__main__':
    unittest.main()
