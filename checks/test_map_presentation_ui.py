"""Map123 classification facts survive the shared live/replay presentation path."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from wargame_native_engine.compat.source_frame.template import (
    source_frame_template_for_scenario,
)
from wargame_native_engine.gateway.models import ControllerView
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.viewer.live_control import (
    controller_view_record,
    native_scenario_map_record,
)
from wargame_native_engine.viewer.map_config import load_native_scenario_map_record
from wargame_native_engine.viewer.replay_view import _frame_record

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples/map123/scenario.json"


class MapPresentationUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenario = load_scenario(SCENARIO)

    def test_objective_types_reach_both_live_and_replay(self):
        template = source_frame_template_for_scenario(self.scenario)
        replay = _frame_record(0, "fixture", template)
        source = template.to_dict()
        source["operators"] = []
        source["passengers"] = []
        source["extra"] = {"native": {"perspective": "RED"}}
        view = ControllerView(
            session_id="map-presentation-test", perspective="RED", step_index=0,
            observation={}, source_frame=source, valid_actions=(),
            terminal=False, winner=None, outcome=None,
        )
        live = controller_view_record(view)["frame"]
        self.assertEqual(live["cities"], replay["cities"])
        self.assertEqual([city["type"] for city in live["cities"]],
                         [0, 0, 0, 1, 1, 1, 1])

    def test_only_map123_town_cells_project_to_settlement(self):
        raw = json.loads(SCENARIO.read_text(encoding="utf-8"))
        expected = {cell["pos"] for cell in raw["map"]["hexes"] if cell["cond"] == 2}
        self.assertEqual(len(expected), 1658)
        live = native_scenario_map_record(self.scenario)
        replay = load_native_scenario_map_record(
            SCENARIO, expected_scenario_id=self.scenario.scenario_id,
            expected_map_id=str(self.scenario.map.map_id),
        )
        self.assertEqual(live["cells"], replay["cells"])
        for record in (live, replay):
            self.assertEqual({int(cell["position_id"]) for cell in record["cells"]
                              if cell["terrain_semantic"] == "settlement"}, expected)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for UI checks")
    def test_shared_map_renderer_shapes_and_towns(self):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("map_presentation_ui.cjs"))],
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
