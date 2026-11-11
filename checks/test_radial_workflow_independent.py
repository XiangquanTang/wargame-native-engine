"""Independent checks for all operation families and interrupted confirmations."""
import shutil
import subprocess
import unittest
from pathlib import Path


class RadialWorkflowIndependentTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for UI checks")
    def test_all_operations_and_interruption_guards(self):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("radial_workflow_independent.cjs"))],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for UI checks")
    def test_legacy_live_target_confirmation_workflows(self):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("live_radial_ui.cjs"))],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_radial_asset_served_by_all_operational_handlers(self):
        from contextlib import ExitStack
        from http.server import ThreadingHTTPServer
        from threading import Thread
        from urllib.request import urlopen
        from wargame_native_engine.viewer.live_server import LiveControlRequestHandler
        from wargame_native_engine.viewer.dual_control_server import DualSideControlRequestHandler
        from wargame_native_engine.viewer.realtime_control_server import RealtimeDualControlRequestHandler

        root = Path(__file__).resolve().parents[1]
        expected = (root / "src/wargame_native_engine/viewer/static/unit_radial_actions.js").read_bytes()
        for handler in (LiveControlRequestHandler, DualSideControlRequestHandler,
                        RealtimeDualControlRequestHandler):
            with self.subTest(handler=handler.__name__), ExitStack() as cleanup:
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                cleanup.callback(server.server_close)
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                cleanup.callback(thread.join, 5)
                cleanup.callback(server.shutdown)
                with urlopen(f"http://127.0.0.1:{server.server_port}/static/unit_radial_actions.js", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), expected)
                    self.assertIn("javascript", response.headers["Content-Type"])

    def test_replay_page_keeps_inspector_and_omits_command_ring(self):
        root = Path(__file__).resolve().parents[1]
        page = (root / "src/wargame_native_engine/viewer/static/index.html").read_text()
        self.assertIn("<aside", page)
        self.assertNotIn("unit_radial_actions.js", page)
