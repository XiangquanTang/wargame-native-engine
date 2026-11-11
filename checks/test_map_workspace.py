"""Multi-map intake keeps source data and failures isolated."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.run_maps import main
from wargame_native_engine.data.map_workspace import check_workspace, initialize_workspace
from wargame_native_engine.io.scenario import NativeHex, NativeMap


class MapWorkspaceTests(unittest.TestCase):
    def native(self, map_id="tiny"):
        return NativeMap(map_id, 100, 100, hexes=(NativeHex(0, 0, 0, (-1,) * 6),),
                         hex_size_m=100, elevation_kind="METERS", elevation_unit_m=10).to_dict()

    def test_initialize_idempotent_preserves_inputs_and_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sibling = root / "map123"
            sibling.mkdir()
            (sibling / "keep").write_text("old")
            workspace = root / "maps"
            first = initialize_workspace(workspace)
            (workspace / "inbox" / "keep").write_text("input")
            self.assertEqual(initialize_workspace(workspace), first)
            self.assertEqual((sibling / "keep").read_text(), "old")
            self.assertEqual((workspace / "inbox" / "keep").read_text(), "input")

    def test_mixed_inputs_continue_and_new_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_workspace(root)
            good = root / "inbox" / "native-map.json"
            good.write_text(json.dumps(self.native()))
            bad = root / "inbox" / "basic.json"
            bad.write_text('{"a": 1, "a": 2}')
            before = good.read_bytes()
            report = check_workspace(root)
            self.assertEqual((report["total"], report["passed"], report["failed"]), (2, 1, 1))
            self.assertEqual(good.read_bytes(), before)
            passing = next(e for e in report["maps"] if e["status"] == "passed")
            self.assertFalse(passing["scenario_ready"])
            self.assertTrue(Path(passing["structured_map"]).is_file())
            self.assertNotEqual(check_workspace(root)["report"], report["report"])

    def test_zip_nested_folders_preserve_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "input.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("one/native-map.json", json.dumps(self.native("one")))
                output.writestr("two/native-map.json", json.dumps(self.native("two")))
            raw = archive.read_bytes()
            report = check_workspace(root / "maps", source=archive)
            self.assertEqual(report["passed"], 2)
            self.assertEqual(archive.read_bytes(), raw)
            self.assertTrue(all("input.zip!" in entry["source"] for entry in report["maps"]))

    def test_unsafe_zip_rejected_before_any_map_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "unsafe.zip"
            for name in ("../escape", "/absolute", "C:/windows", "folder\\escape"):
                with self.subTest(name=name):
                    with zipfile.ZipFile(archive, "w") as output:
                        output.writestr("native-map.json", json.dumps(self.native()))
                        output.writestr(name, "bad")
                    report = check_workspace(root / "maps", source=archive)
                    self.assertEqual(report["failed"], 1)
                    self.assertEqual(report["passed"], 0)
            self.assertFalse((root / "escape").exists())

    def test_cli_empty_inbox_fails_and_mode_conflict(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init", "--workspace", directory]), 0)
            self.assertEqual(main(["check", "--workspace", directory]), 2)
            with self.assertRaisesRegex(ValueError, "cannot be combined"):
                check_workspace(Path(directory), terrain_only=True, require_runtime=True)

    def test_workspace_symlink_is_rejected_without_external_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "maps"
            initialize_workspace(workspace)
            external = root / "outside"
            external.mkdir()
            (workspace / "reports").rmdir()
            (workspace / "reports").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                initialize_workspace(workspace)
            self.assertEqual(list(external.iterdir()), [])

    def test_source_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "map.json"
            source.write_text(json.dumps(self.native()))
            alias = root / "alias.json"
            alias.symlink_to(source)
            with self.assertRaisesRegex(ValueError, "symlink"):
                check_workspace(root / "maps", source=alias)

    def test_native_map_runtime_required_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "map.json"
            source.write_text(json.dumps(self.native()))
            report = check_workspace(root / "maps", source=source, require_runtime=True)
            self.assertEqual(report["failed"], 1)
            self.assertIn("required", report["maps"][0]["error"])
            self.assertTrue(report["maps"][0]["terrain_ready"])
            self.assertTrue(Path(report["maps"][0]["preview"]).is_file())
            self.assertTrue(Path(report["index"]).is_file())


if __name__ == "__main__":
    unittest.main()
