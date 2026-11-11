"""Run dependency-free frontend interaction checks when Node.js is available."""

import shutil
import subprocess
import unittest
from pathlib import Path


class AttackSelectionUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for UI checks")
    def test_candidate_highlights_preserve_actor_and_target_selection(self):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("attack_selection_ui.cjs"))],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
