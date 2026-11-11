"""Selected-unit region display uses current side-bound backend cells."""
from pathlib import Path
import shutil
import subprocess
import unittest


class TacticalOverlayUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for UI checks")
    def test_overlay_cells_selection_freshness_and_controls(self):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("tactical_overlay_ui.cjs"))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
