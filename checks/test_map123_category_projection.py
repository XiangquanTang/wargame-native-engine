"""Preserve observed Map123 categories without disclosing private enemy state.

The records come from the unchanged original Map123 dataset. Contacts here are
explicit fixture inputs to the observer projector, not claims that the initial
Map123 sensor state discovers these units. No runtime cache or large benchmark
is required by this test.
"""

import json
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from wargame_native_engine.core.observation import (
    SideObservation,
    side_safe_enemy_observation,
)
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import Stage
from wargame_native_engine.observations.perspective import (
    PerspectiveContext,
    PerspectiveProjector,
)
from wargame_native_engine.viewer.live_control import _viewer_unit
from wargame_native_engine.viewer.operator_registry import load_operator_registry

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CATEGORIES = {
    (2, 0): "重型坦克",
    (2, 3): "炮兵",
    (3, 5): "无人机",
    (2, 1): "重型战车",
    (1, 2): "步兵",
}


class Map123CategoryProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scenario = json.loads(
            (ROOT / "examples/map123/scenario.json").read_text(encoding="utf-8")
        )
        cls.states = tuple(OperatorState.from_dict(o) for o in scenario["operators"])
        representatives = {}
        for operator in cls.states:
            if operator.side == "blue":
                representatives.setdefault(operator.subtype, operator)
        cls.contacts = tuple(
            side_safe_enemy_observation(o) for o in representatives.values()
        )
        cls.registry = load_operator_registry()

    def project(self, contacts=None, green=None):
        if contacts is None:
            contacts = self.contacts
        if green is None:
            green = {
                "time": {"cur_step": 0},
                "operators": [
                    deepcopy(o.raw_attributes["source_frame"]) for o in self.states
                ],
            }
        red = SideObservation("red", 0, Stage.COMBAT, (), contacts, (), ())
        blue = SideObservation("blue", 0, Stage.COMBAT, (), (), (), ())
        context = PerspectiveContext(red, blue, "map123-category-test", {})
        return PerspectiveProjector().project(green, context)

    def test_all_five_source_categories_survive_observer_to_viewer(self):
        projection = self.project()
        units = [
            _viewer_unit(source, "operator", "RED", None)
            for source in projection.red["operators"]
        ]
        resolved = {}
        for unit in units:
            descriptor = self.registry.resolve_viewer_unit(unit)
            self.assertEqual(descriptor.status, "MAPPED")
            resolved[(unit["type_code"], unit["sub_type_code"])] = (
                descriptor.display_name
            )
        self.assertEqual(resolved, EXPECTED_CATEGORIES)
        self.assertEqual(len(self.states), 49)
        visible_ids = {u["operator_id"] for u in units}
        self.assertEqual(visible_ids, {c.operator_id for c in self.contacts})
        self.assertEqual(len({o.operator_id for o in self.states} - visible_ids), 44)
        self.assertEqual(projection.blue["operators"], [])

    def test_all_49_model_names_survive_both_side_contacts(self):
        for observer_side in ('red', 'blue'):
            detected = tuple(side_safe_enemy_observation(o) for o in self.states
                             if o.side != observer_side)
            sides = {side: SideObservation(side, 0, Stage.COMBAT, (),
                     detected if side == observer_side else (), (), ())
                     for side in ('red', 'blue')}
            projection = PerspectiveProjector().project({}, PerspectiveContext(
                sides['red'], sides['blue'], 'all-models', {}))
            records = projection.red if observer_side == 'red' else projection.blue
            actual = {u['obj_id']: u for u in records['operators']}
            self.assertEqual(set(actual), {o.operator_id for o in self.states if o.side != observer_side})
            for operator in self.states:
                if operator.side != observer_side:
                    self.assertEqual(actual[operator.operator_id]['name'], operator.raw_attributes['source_name'])
                    self.assertIsNone(actual[operator.operator_id]['blood'])
                    self.assertEqual(actual[operator.operator_id]['remain_bullet_nums'], {})

    def test_visible_identity_does_not_disclose_private_fields(self):
        for source in self.project().red["operators"]:
            for name in ("blood", "max_blood", "weapon_cool_time", "owner"):
                self.assertIsNone(source[name], name)
            self.assertEqual(source["armor"], -1)
            for name in ("carry_weapon_ids", "move_path", "passenger_ids"):
                self.assertEqual(source[name], [], name)
            self.assertEqual(source["remain_bullet_nums"], {})
            unit = _viewer_unit(source, "operator", "RED", None)
            self.assertIsNone(unit["health_ratio"])
            self.assertIsNone(unit["movement_target_hex"])

    def test_green_private_changes_cannot_backfill_observed_contact(self):
        green = {
            "time": {"cur_step": 0},
            "operators": [
                deepcopy(o.raw_attributes["source_frame"]) for o in self.states
            ],
        }
        baseline = self.project(green=green)
        for source in green["operators"]:
            source.update({
                "name": "PRIVATE_NAME_SENTINEL",
                "type": 999,
                "sub_type": 999,
                "blood": 999,
                "armor": 999,
                "carry_weapon_ids": [999],
                "remain_bullet_nums": {"999": 999},
                "move_path": [9999],
            })
        changed = self.project(green=green)
        self.assertEqual(changed.red, baseline.red)
        self.assertEqual(changed.blue, baseline.blue)

    def test_unknown_subtype_never_inherits_hidden_known_identity(self):
        tank = next(c for c in self.contacts if c.source_subtype_code == 0)
        for subtype in ("source-2-999", None):
            with self.subTest(subtype=subtype):
                operator = next(
                    o for o in self.states if o.operator_id == tank.operator_id
                )
                contact = side_safe_enemy_observation(
                    replace(operator, subtype=subtype)
                )
                source = self.project(contacts=(contact,)).red["operators"][0]
                unit = _viewer_unit(source, "operator", "RED", None)
                # Even tempting names/icon hints must not manufacture a class.
                unit.update(name="重型坦克", icon_key="tank")
                descriptor = self.registry.resolve_viewer_unit(unit)
                self.assertEqual(descriptor.status, "UNKNOWN")
                self.assertEqual(descriptor.symbol.base, "unknown")
                self.assertEqual(descriptor.symbol.modifiers, ())
                self.assertIsNone(contact.canonical_identity)
                self.assertNotEqual(
                    (unit["type_code"], unit["sub_type_code"]), (2, 0)
                )


if __name__ == "__main__":
    unittest.main()
