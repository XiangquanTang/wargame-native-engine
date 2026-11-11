"""Shared action buttons stay in a compact dock independent of map panning."""
from pathlib import Path
import shutil
import subprocess
import unittest


class UnitRadialLayoutTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for UI checks")
    def test_docked_action_layout(self):
        result = subprocess.run(["node", str(Path(__file__).with_name("unit_radial_layout.cjs"))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_all_control_servers_expose_shared_script(self):
        from wargame_native_engine.viewer import live_server, dual_control_server, realtime_control_server
        for module in (live_server, dual_control_server, realtime_control_server):
            self.assertIn("/static/unit_radial_actions.js", module._STATIC_FILES)


if __name__ == "__main__":
    unittest.main()
