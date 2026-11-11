"""Legend units and steps are projected facts, never palette-derived values."""

from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.viewer.live_control import native_scenario_map_record
from wargame_native_engine.viewer.map_config import (
    coordinate_fallback_record,
    load_native_scenario_map_record,
    load_viewer_map_config,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples/map123/scenario.json"


class ElevationLegendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenario = load_scenario(SCENARIO)

    def test_native_live_and_replay_preserve_map123_units_and_step(self):
        before = self.scenario.to_dict()
        live = native_scenario_map_record(self.scenario)
        replay = load_native_scenario_map_record(
            SCENARIO, expected_scenario_id=self.scenario.scenario_id,
            expected_map_id=str(self.scenario.map.map_id),
        )
        for record in (live, replay):
            self.assertEqual(record["elevation_kind"], "METERS")
            self.assertEqual(record["elevation_unit_m"], 10)
            self.assertEqual(record["elevation"], {
                "mode": "episode", "minimum": 0, "maximum": 70, "levels": 7,
            })
        self.assertEqual(self.scenario.to_dict(), before)

    def test_native_levels_and_unknown_metadata_remain_unconverted(self):
        for kind, unit in (("LEVEL", 5), ("LEVEL", None), (None, None)):
            scenario = replace(self.scenario, map=replace(
                self.scenario.map, elevation_kind=kind, elevation_unit_m=unit,
                hexes=tuple(replace(cell, elevation=index + 2)
                            for index, cell in enumerate(self.scenario.map.hexes[:2])),
            ))
            record = native_scenario_map_record(scenario)
            self.assertEqual(record["elevation_kind"], kind)
            self.assertEqual(record["elevation_unit_m"], unit)
            self.assertEqual(record["elevation"]["minimum"], 2)
            self.assertEqual(record["elevation"]["maximum"], 3)
            self.assertEqual([cell["elevation"] for cell in record["cells"]], [2, 3])

    def basic_record(self, metadata):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "map.json"
            source.write_text(json.dumps({
                "coordinate_system": "odd-r",
                "map_data": [[{"elev": 0}, {"elev": 70}]], **metadata,
            }))
            return load_viewer_map_config(source).to_record()

    def test_basic_map_projects_only_declared_units_and_steps(self):
        for metadata, kind, unit in (
            ({"ele_grade": 10}, "METERS", 10),
            ({"elevation_kind": "LEVEL", "elevation_unit_m": 5}, "LEVEL", 5),
            ({"elevation_kind": "METERS"}, "METERS", None),
            ({"elevation_unit_m": 10}, None, 10),
            ({}, None, None),
            ({"ele_grade": 0}, None, None),
            ({"ele_grade": -10}, None, None),
            ({"ele_grade": "10"}, None, None),
            ({"ele_grade": True}, None, None),
        ):
            with self.subTest(metadata=metadata):
                record = self.basic_record(metadata)
                self.assertEqual(record["elevation_kind"], kind)
                self.assertEqual(record["elevation_unit_m"], unit)
                self.assertEqual(record["elevation"]["minimum"], 0)
                self.assertEqual(record["elevation"]["maximum"], 70)

    def test_missing_elevation_and_coordinate_fallback_stay_unknown(self):
        scenario = replace(self.scenario, map=replace(
            self.scenario.map,
            hexes=(replace(self.scenario.map.hexes[0], elevation=None),),
        ))
        for record in (native_scenario_map_record(scenario), coordinate_fallback_record([])):
            self.assertEqual(record["elevation"]["mode"], "unavailable")
            self.assertIsNone(record["elevation"]["minimum"])
            self.assertIsNone(record["elevation"]["maximum"])
        fallback = coordinate_fallback_record([])
        self.assertIsNone(fallback["elevation_kind"])
        self.assertIsNone(fallback["elevation_unit_m"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for UI checks")
    def test_shared_legend_ui(self):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("elevation_legend_ui.cjs"))],
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
