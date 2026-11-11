"""Map123 opts into the new game rule without broadening guided/indirect fire."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENABLED = {"4", "29", "35", "36", "43", "54"}


class Map123MovingFireProfileTests(unittest.TestCase):
    def test_both_variants_and_conversion_agree_on_capability(self):
        supplement = json.loads((ROOT / "config/source_scenario_conversion/scenarios/2201010105.native-supplement.json").read_text())
        expected = supplement["weapon_profiles"]
        for filename in ("scenario.json", "scenario-artillery-ammo2.json"):
            with self.subTest(filename=filename):
                scenario = json.loads((ROOT / "examples/map123" / filename).read_text())
                profiles = scenario["metadata"]["source_compatible"]["weapons"]
                self.assertEqual({key for key, value in profiles.items() if value["can_fire_while_moving"]}, ENABLED)
                self.assertEqual(len(scenario["operators"]), 49)
                for key, value in profiles.items():
                    self.assertEqual(value["can_fire_while_moving"], expected[key]["can_fire_while_moving"])
                    if key in ENABLED:
                        self.assertTrue(value["target_domains"])
                        self.assertIsNotNone(value["ammunition_key"])

    def test_capability_is_explicit_not_global_registry_change(self):
        registry = json.loads((ROOT / "config/source_compat/operator_registry_v1.json").read_text())
        for key in ENABLED:
            self.assertFalse(registry["weapon_profiles"][key]["can_fire_while_moving"])


if __name__ == "__main__":
    unittest.main()
