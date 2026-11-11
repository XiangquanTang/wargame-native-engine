from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.run_maps import main
from wargame_native_engine.data.scenario_workspace import _one_raw_map, prepare_source_game


class SourceGameStartTests(unittest.TestCase):
    def test_start_prepare_only_and_managed_bindings(self):
        report = {"scenario_path": "/new/scenario.json", "runtime": {"path": "/new/runtime"},
                  "output_dir": "/new/run", "run_namespace": "fresh-run",
                  "map_id": "221", "scenario_id": "fixture", "preparation_path": "/new/preparation.json"}
        with patch("wargame_native_engine.data.scenario_workspace.prepare_source_game", return_value=report) as prepare, \
             patch("scripts.run_realtime_dual_control.main", return_value=0) as launch, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main(["start", "--input", "map.zip", "--scenario-input", "scenario.json", "--prepare-only"]), 0)
            launch.assert_not_called()
            self.assertEqual(main(["start", "--input", "map.zip", "--scenario-input", "scenario.json", "--port", "8100"]), 0)
            self.assertEqual(launch.call_args.args[0], ["--scenario-bundle", "/new/scenario.json", "--map-runtime-data", "/new/runtime",
                "--output-dir", "/new/run", "--run-namespace", "fresh-run", "--port", "8100"])
            self.assertEqual(prepare.call_count, 2)

    def test_start_rejects_overrides_before_preparation(self):
        for option in ("--scenario-bundle=x", "--map-runtime-data=x", "--run-namespace=x", "--output-dir=x", "--overwrite",
                       "--scenario-b=x", "--map-r=x", "--run-n=x", "--output-d=x", "--overw", "--port=0", "--port=garbage"):
            with self.subTest(option=option), patch("wargame_native_engine.data.scenario_workspace.prepare_source_game") as prepare, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(["start", "--input", "map.zip", "--scenario-input", "scenario.json", option])
                prepare.assert_not_called()

    def test_exactly_one_map_and_safe_archive_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "maps.zip"
            for members in (("a/basic.json", "b/basic.json"), ("../basic.json",)):
                with zipfile.ZipFile(path, "w") as archive:
                    for name in members:
                        archive.writestr(name, "{}")
                with self.assertRaises(ValueError), _one_raw_map(path):
                    pass
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("map/basic.json", "{}")
            original = path.read_bytes()
            with _one_raw_map(path) as basic:
                self.assertEqual(json.loads(basic.read_text()), {})
            self.assertFalse(basic.exists())
            self.assertEqual(path.read_bytes(), original)

    def test_source_json_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "source.json"
            target.write_text("{}")
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink"):
                prepare_source_game(root / "map.zip", link, root / "workspace")
            self.assertFalse((root / "workspace").exists())


if __name__ == "__main__":
    unittest.main()
