"""Terrain presentation is explicit, shared, and does not rewrite map facts."""

from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from wargame_native_engine.io.scenario import (
    NativeHex,
    load_scenario,
    native_map_identity_hash,
    scenario_from_dict,
)
from wargame_native_engine.movement.models import TerrainType
from wargame_native_engine.movement.policy import load_source_movement_policy
from wargame_native_engine.viewer.live_control import native_scenario_map_record
from wargame_native_engine.viewer.map_config import (
    ViewerMapConfigError,
    load_native_scenario_map_record,
    load_viewer_map_config,
)
from wargame_native_engine.viewer.terrain_display import terrain_display_semantic


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SCENARIO = ROOT / "examples/source_movement_scenario.json"
SEMANTICS = ("open", "forest", "settlement", "soft_ground", "water", "roadblock")
POLICY_NAMES = ("NORMAL", "FOREST", "SETTLEMENT", "SOFT_GROUND", "LARGE_RIVER", "ROADBLOCK")


class TerrainDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenario = load_scenario(SOURCE_SCENARIO)
        cls.policy = load_source_movement_policy()

    def native_record(self, values, *, roadblocks=()):
        cells = tuple(
            NativeHex(100 + index, 2, value, (-1,) * 6)
            for index, value in enumerate(values)
        )
        scenario = replace(self.scenario, map=replace(
            self.scenario.map, hexes=cells, roadblocks=roadblocks,
        ))
        return scenario, native_scenario_map_record(scenario)

    def basic_record(self, values):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "basic.json"
            path.write_text(json.dumps({
                "coordinate_system": "odd-r",
                "map_data": [[{"cond": value} for value in values]],
            }), encoding="utf-8")
            return load_viewer_map_config(path).to_record()

    def test_all_integer_codes_match_source_policy_and_both_projections(self):
        self.assertEqual(
            {code: terrain.value for code, terrain in self.policy.terrain_codes},
            dict(enumerate(POLICY_NAMES)),
        )
        native = self.native_record(range(6))[1]
        basic = self.basic_record(range(6))
        for code, semantic in enumerate(SEMANTICS):
            with self.subTest(code=code):
                self.assertEqual(self.policy.terrain(code).value, POLICY_NAMES[code])
                self.assertEqual(terrain_display_semantic(code), semantic)
                for record in (native, basic):
                    cell = record["cells"][code]
                    self.assertEqual(cell["terrain_semantic"], semantic)
                    self.assertEqual(cell["terrain_code"], code)
                    self.assertNotIn("terrain_source", cell)

    def test_native_policy_names_and_numeric_strings_preserve_raw_values(self):
        expected = dict(zip(POLICY_NAMES, SEMANTICS))
        expected["SMALL_RIVER"] = "small_river"
        expected["UNKNOWN"] = "unknown"
        self.assertEqual(set(expected), {terrain.value for terrain in TerrainType})
        values = [name for name in expected]
        values += [name.lower() for name in expected]
        values += [str(code) for code in range(6)] + ["00", "-0", "01", "１", "٢"]
        record = self.native_record(values)[1]
        for value, cell in zip(values, record["cells"]):
            with self.subTest(value=value):
                semantic = expected[self.policy.terrain(value).value]
                self.assertEqual(cell["terrain_semantic"], semantic)
                self.assertIsNone(cell["terrain_code"])
                self.assertEqual(cell["terrain_source"], value)
                self.assertEqual(terrain_display_semantic(value), "unknown")

    def test_unknown_inputs_are_not_coerced_or_guessed(self):
        values = [None, True, False, 1.0, -1, 6, 99, "", "rough", "blocked",
                  "open", "water", "forest ", " FOREST", "1.0", "+1", "--1"]
        record = self.native_record(values)[1]
        for value, cell in zip(values, record["cells"]):
            with self.subTest(value=value):
                self.assertEqual(cell["terrain_semantic"], "unknown")
                if type(value) is int or value is None:
                    self.assertNotIn("terrain_source", cell)
                else:
                    self.assertEqual(cell["terrain_source"], value)
                self.assertEqual(cell["terrain_code"], value if type(value) is int else None)
                self.assertNotIn("roadblock", cell)
        for value in ([], {}, object()):
            self.assertEqual(
                terrain_display_semantic(value, allow_native_strings=True), "unknown",
            )

    def test_basic_map_keeps_integer_or_null_boundary(self):
        record = self.basic_record([None, -1, 99])
        self.assertTrue(all(cell["terrain_semantic"] == "unknown"
                            for cell in record["cells"]))
        for value in (True, False, 1.0, "1", "FOREST", "ROADBLOCK"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ViewerMapConfigError, "cond must be an int or null",
            ):
                self.basic_record([value])

    def test_missing_terrain_stays_unknown(self):
        raw = self.scenario.to_dict()
        del raw["map"]["hexes"][0]["cond"]
        native = native_scenario_map_record(scenario_from_dict(raw))["cells"][0]
        self.assertEqual(native["terrain_semantic"], "unknown")
        self.assertEqual(native["terrain_source"], "UNKNOWN")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "basic.json"
            path.write_text(json.dumps({
                "coordinate_system": "odd-r", "map_data": [[{}]],
            }), encoding="utf-8")
            cell = load_viewer_map_config(path).to_record()["cells"][0]
            self.assertEqual(cell["terrain_semantic"], "unknown")
            self.assertIsNone(cell["terrain_code"])
            self.assertNotIn("terrain_source", cell)

    def test_static_roadblock_is_separate_from_underlying_terrain(self):
        scenario, record = self.native_record([1, "FOREST", 5, "rough"], roadblocks=(100, 101))
        self.assertEqual([cell["terrain_semantic"] for cell in record["cells"]],
                         ["forest", "forest", "roadblock", "unknown"])
        self.assertEqual([cell.get("roadblock", False) for cell in record["cells"]],
                         [True, True, False, False])
        self.assertTrue(all(cell["roadblock"] is True for cell in record["cells"]
                            if "roadblock" in cell))
        self.assertEqual([cell.terrain for cell in scenario.map.hexes],
                         [1, "FOREST", 5, "rough"])
        self.assertEqual(scenario.map.roadblocks, (100, 101))

    def test_display_projection_leaves_serialization_identity_and_edges_unchanged(self):
        scenario, _ = self.native_record(["FOREST", "ROADBLOCK"], roadblocks=(100,))
        cells = list(scenario.map.hexes)
        cells[0] = replace(cells[0], roads=(1, 2, 3, 0, 0, 0), rivers=(0, 1, 2, 0, 0, 0))
        scenario = replace(scenario, map=replace(
            scenario.map, hexes=tuple(cells), blocked_positions=((1.0, 1.0),),
        ))
        before = scenario.to_dict()
        scenario_hash = scenario.scenario_hash
        map_hash = native_map_identity_hash(scenario.map)
        live = native_scenario_map_record(scenario)
        replay = load_native_scenario_map_record(
            SOURCE_SCENARIO, loaded_scenario=scenario,
            expected_scenario_id=scenario.scenario_id,
            expected_map_id=scenario.map.map_id,
            expected_scenario_hash=scenario_hash, expected_map_hash=map_hash,
        )
        self.assertEqual(live["cells"], replay["cells"])
        self.assertEqual(live["cells"][0]["roads"], [1, 2, 3, 0, 0, 0])
        self.assertEqual(live["cells"][0]["rivers"], [0, 1, 2, 0, 0, 0])
        # Returned presentation edge arrays are detached from Native data.
        live["cells"][0]["roads"][0] = 99
        live["cells"][0]["terrain_source"] = "SETTLEMENT"
        self.assertEqual(scenario.to_dict(), before)
        self.assertEqual(scenario.scenario_hash, scenario_hash)
        self.assertEqual(native_map_identity_hash(scenario.map), map_hash)

    def test_map123_distribution_and_cell_facts_are_unchanged(self):
        scenario = load_scenario(ROOT / "examples/map123/scenario.json")
        cells = native_scenario_map_record(scenario)["cells"]
        self.assertEqual(len(cells), 10000)
        self.assertEqual(Counter(cell["terrain_code"] for cell in cells),
                         {0: 5522, 1: 2733, 2: 1658, 4: 87})
        self.assertEqual(Counter(cell["terrain_semantic"] for cell in cells),
                         {"open": 5522, "forest": 2733, "settlement": 1658, "water": 87})
        source = {cell.hex_id: cell for cell in scenario.map.hexes}
        for cell in cells:
            original = source[int(cell["position_id"])]
            self.assertNotIn("terrain_source", cell)
            self.assertEqual(cell["elevation"], original.elevation)
            self.assertEqual(cell["roads"], list(original.roads))
            self.assertEqual(cell["rivers"], list(original.rivers))
            self.assertNotIn("roadblock", cell)
            self.assertEqual(cell, {
                "row": original.hex_id // 100,
                "column": original.hex_id % 100,
                "position_id": str(original.hex_id),
                "elevation": original.elevation,
                "terrain_level": (int(original.elevation)
                                  if scenario.map.elevation_kind == "LEVEL"
                                  and original.elevation is not None else None),
                "terrain_code": original.terrain,
                "terrain_semantic": SEMANTICS[original.terrain],
                "roads": list(original.roads), "rivers": list(original.rivers),
            })


if __name__ == "__main__":
    unittest.main()
