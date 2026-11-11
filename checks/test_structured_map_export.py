"""CLI and precision checks for the deterministic static-map export."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wargame_native_engine.cli.structure_map import main
from wargame_native_engine.io.scenario import NativeHex, NativeMap
from wargame_native_engine.io.structured_map import (
    CELL_COLUMNS, StructuredMapError, load_structured_map, structure_map,
)


class StructuredMapExportTests(unittest.TestCase):
    def fixture(self):
        return NativeMap(
            "example", 100, 100,
            hexes=(NativeHex(503, 0.3, "FOREST", (-1,) * 6),),
            hex_size_m=10, elevation_kind="METERS", elevation_unit_m=0.1,
        )

    def row(self, result):
        return dict(zip(CELL_COLUMNS, result["cells"][0]))

    def test_decimal_exact_grade_does_not_round_off_grid_value(self):
        value = self.fixture()
        self.assertEqual(self.row(structure_map(value))["elevation_level"], 3)
        changed = replace(value, hexes=(replace(value.hexes[0], elevation=0.31),))
        row = self.row(structure_map(changed))
        self.assertIsNone(row["elevation_level"])
        self.assertEqual(row["elevation_raw"], 0.31)
        self.assertEqual(row["elevation_m"], 0.31)

    def test_cli_new_file_stdout_identity_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "map.json"
            output = Path(directory) / "structured.json"
            source.write_text(json.dumps(self.fixture().to_dict()), encoding="utf-8")
            args = [str(source), "--input-format", "native-map"]
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(args), 0)
            self.assertEqual(main(args + ["--output", str(output)]), 0)
            self.assertEqual(stdout.getvalue(), output.read_text(encoding="utf-8"))
            old = output.read_bytes()
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(args + ["--output", str(output)])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(output.read_bytes(), old)

    def test_source_and_static_hashes_have_distinct_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenario.json"
            raw = {"map": self.fixture().to_dict(), "operators": ["PRIVATE"]}
            path.write_text(json.dumps(raw), encoding="utf-8")
            first = load_structured_map(path, input_format="native-scenario")
            raw["operators"] = ["CHANGED PRIVATE"]
            path.write_text(json.dumps(raw), encoding="utf-8")
            second = load_structured_map(path, input_format="native-scenario")
            self.assertEqual(first["map_hash"], second["map_hash"])
            self.assertNotEqual(first["source"]["sha256"], second["source"]["sha256"])
            self.assertNotIn("PRIVATE", json.dumps(second))
            self.assertEqual(first["map_hash"], structure_map(self.fixture())["map_hash"])

    def test_basic_missing_features_and_changed_snapshot(self):
        from wargame_native_engine.viewer.map_config import load_viewer_map_config
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "basic.json"
            path.write_text(json.dumps({
                "coordinate_system": "odd-r", "map_data": [[{}]],
            }), encoding="utf-8")
            result = load_structured_map(path, input_format="basic")
            row = self.row(result)
            self.assertTrue(all(row[key] is None for key in (
                "neighbors", "roads", "rivers", "terrain_code", "elevation_raw",
            )))
            viewer = load_viewer_map_config(path)
            with patch("wargame_native_engine.io.structured_map.load_viewer_map_config",
                       return_value=replace(viewer, source_sha256="changed")):
                with self.assertRaisesRegex(StructuredMapError, "changed during loading"):
                    load_structured_map(path, input_format="basic")

    def test_unsafe_json_or_continuous_obstacles_reject(self):
        with self.assertRaisesRegex(StructuredMapError, "blocked_positions"):
            structure_map(replace(self.fixture(), blocked_positions=((1, 2),)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for text in ('{"x":NaN}', '{"x":1e400}', '{"x":1,"x":2}'):
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(StructuredMapError):
                    load_structured_map(path, input_format="native-map")


if __name__ == "__main__":
    unittest.main()
