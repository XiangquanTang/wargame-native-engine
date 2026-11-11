"""Browser-independent chart math and replay request scheduling regressions."""
from pathlib import Path
import shutil
import subprocess
import unittest


class ReplayTimelineUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for viewer JavaScript checks")
    def test_chart_domains_gaps_and_async_loading(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(["node", str(root / "checks/replay_timeline_ui.cjs")], cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
