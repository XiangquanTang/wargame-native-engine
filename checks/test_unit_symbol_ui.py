"""Exercise actual category resolution and renderer contracts using Map123."""

import shutil
import subprocess
import unittest
from pathlib import Path


class UnitSymbolUITests(unittest.TestCase):
    def test_weight_variants_are_served_as_local_svg_resources(self):
        from wargame_native_engine.viewer.static_resources import (
            load_symbol_static_resource,
        )

        for base in ("tank", "armored_vehicle"):
            variants = []
            for weight in ("medium", "heavy"):
                with self.subTest(base=base, weight=weight):
                    resource = load_symbol_static_resource(
                        f"/static/assets/unit_symbols/base/{base}_{weight}.svg"
                    )
                    self.assertIsNotNone(resource)
                    self.assertEqual(resource.content_type, "image/svg+xml; charset=utf-8")
                    self.assertIn(b'viewBox="0 0 128 128"', resource.payload)
                    variants.append(resource.payload)
            self.assertNotEqual(*variants)

    def test_light_badge_is_served(self):
        from wargame_native_engine.viewer.static_resources import load_symbol_static_resource
        resource = load_symbol_static_resource(
            "/static/assets/unit_symbols/modifiers/armor_light.svg"
        )
        self.assertIsNotNone(resource)
        self.assertIn(b'viewBox="0 0 128 128"', resource.payload)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for UI checks")
    def test_map123_symbols_and_presentation_boundaries(self):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("unit_symbol_ui.cjs"))],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
