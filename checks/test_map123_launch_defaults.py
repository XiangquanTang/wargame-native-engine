"""The standard Map123 launch must not silently select the ammo-modified demo."""
import json
import unittest
from pathlib import Path

from scripts.run_map123 import build_parser


class Map123LaunchDefaultsTests(unittest.TestCase):
    def test_launcher_defaults_to_original_and_keeps_demo_explicit(self):
        self.assertEqual(build_parser().parse_args([]).scenario, "original")
        self.assertEqual(
            build_parser().parse_args(["--scenario", "artillery-ammo2"]).scenario,
            "artillery-ammo2",
        )
        self.assertEqual(build_parser().parse_args([]).host, "127.0.0.1")

    def test_catalog_defaults_to_original_49_unit_scenario(self):
        root = Path(__file__).resolve().parents[1]
        catalog = json.loads((root / "examples/maps/catalog.json").read_text())
        entry = next(item for item in catalog["maps"] if item["id"] == "map123")
        self.assertEqual(entry["default_variant"], "original")
        original = next(item for item in entry["scenarios"] if item["variant"] == "original")
        self.assertEqual(original["initial_units"], 49)
        self.assertEqual(original["path"], "examples/map123/scenario.json")
