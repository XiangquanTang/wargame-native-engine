"""Presentation names never grant combat support or rewrite source evidence."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from wargame_native_engine.weapon_names import WEAPON_DISPLAY_NAMES, weapon_display_name
from wargame_native_engine.shareddata.operator_registry import (
    default_operator_registry_path, load_operator_registry,
)
from wargame_native_engine.data.source_scenario_profiles import build_source_scenario_profile


class WeaponDisplayNameTests(unittest.TestCase):
    def test_exact_user_names(self):
        expected = {
            71:'便携导弹',5:'便携导弹（对地）',29:'步兵轻武器',69:'车载导弹',
            43:'车载轻武器',36:'大号直瞄炮',35:'火箭筒',56:'速射炮',
            4:'速射炮（对地）',84:'炮射导弹',89:'轻型炮',54:'小号直瞄炮',
            75:'小型导弹',76:'巡飞导弹',37:'中号直瞄炮',83:'中型导弹（标准）',
            74:'中型导弹（便携）',88:'中型炮',73:'重型导弹',72:'重型炮',
            1:'防空高炮',2:'便携防空导弹',3:'车载防空导弹',
        }
        self.assertEqual(dict(WEAPON_DISPLAY_NAMES), expected)
        for weapon_id, name in expected.items():
            self.assertEqual(weapon_display_name(weapon_id, 'old name'), name)
        with self.assertRaises(TypeError):
            WEAPON_DISPLAY_NAMES[5] = 'changed'

    def test_engine_catalog_all_names_without_fabricated_profiles(self):
        from pathlib import Path
        from wargame_native_engine.core.engine import NativeEngine
        from wargame_native_engine.io.scenario import load_scenario
        root = Path(__file__).resolve().parents[1]
        engine = NativeEngine(load_scenario(root / 'examples/source_movement_scenario.json'))
        self.addCleanup(engine.close)
        catalog = engine.get_weapon_catalog()
        entries = {item['weapon_id']: item for item in catalog['weapons']}
        for weapon_id, name in WEAPON_DISPLAY_NAMES.items():
            self.assertEqual(entries[weapon_id]['name'], name)
        self.assertIsNone(entries[71]['runtime_profile'])
        self.assertEqual(entries[71]['fire_modes'], [])

    def test_unknown_and_invalid_identity(self):
        self.assertEqual(weapon_display_name(999, '用户自定'), '用户自定')
        for fallback in (None, '', ' ', 'UNKNOWN'):
            self.assertEqual(weapon_display_name(999, fallback), '武器 999')
        for value in (True, '5', 5.0, None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                weapon_display_name(value)

    def test_registry_keeps_every_non_name_field(self):
        path = default_operator_registry_path()
        before = path.read_bytes()
        raw = json.loads(before)
        registry = load_operator_registry(path)
        self.assertEqual(set(registry.weapon_profiles), {int(k) for k in raw['weapon_profiles']})
        for key, original in raw['weapon_profiles'].items():
            result = registry.weapon_profiles[int(key)].to_record()
            expected = dict(original)
            expected['name'] = weapon_display_name(int(key), original['name'])
            self.assertEqual(result, expected)
        self.assertEqual(path.read_bytes(), before)

    def test_profile_labels_are_metadata_only(self):
        source = {'scenario_id':'name-test', 'time':{'cur_step':0,'tick':1,'max_time':1800},
            'operators':[{'obj_id':1,'color':0,'type':1,'sub_type':2,'name':'步兵小队',
                'carry_weapon_ids':[29,35,5], 'remain_bullet_nums':{'0':10,'100':4},'cur_hex':101}]}
        original = deepcopy(source)
        profile = build_source_scenario_profile(source, '9601')
        with patch('wargame_native_engine.data.source_scenario_profiles.weapon_display_name',
                   return_value='presentation-only'):
            renamed = build_source_scenario_profile(source, '9601')
        for weapon_id, value in profile['weapon_profiles'].items():
            self.assertEqual(value.pop('display_name'), weapon_display_name(int(weapon_id)))
            self.assertEqual(renamed['weapon_profiles'][weapon_id].pop('display_name'), 'presentation-only')
        self.assertEqual(profile, renamed)
        self.assertEqual(source, original)


if __name__ == '__main__':
    unittest.main()
