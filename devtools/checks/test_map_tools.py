"""Standalone static-map function boundary and canonical page reconstruction."""

import ast
import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from wargame_native_engine.agents.map_tool_calling import (
    MapToolAdapter, MapToolError, function_schemas,
)
from wargame_native_engine.io.scenario import NativeMap, load_scenario
from wargame_native_engine.io.structured_map import structure_map


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples/source_movement_scenario.json"


class MapToolTests(unittest.TestCase):
    def setUp(self):
        self.native_map = load_scenario(SOURCE).map
        self.map_id = self.native_map.map_id
        self.adapter = MapToolAdapter({self.map_id: self.native_map})
        self.args = {"map_id": self.map_id, "offset": 0, "limit": 2}

    def test_pages_reconstruct_identical_direct_export(self):
        expected = structure_map(self.native_map)
        collected = []
        offset = 0
        while True:
            page = self.adapter.dispatch("structured_map", {**self.args, "offset": offset})
            self.assertEqual(
                {key: value for key, value in page.items() if key not in {"cells", "pagination"}},
                {key: value for key, value in expected.items() if key != "cells"},
            )
            pagination = page["pagination"]
            self.assertEqual(pagination["offset"], offset)
            self.assertEqual(pagination["returned"], len(page["cells"]))
            self.assertEqual(pagination["total"], expected["cell_count"])
            self.assertLessEqual(len(page["cells"]), 2)
            collected.extend(page["cells"])
            if not pagination["has_more"]:
                self.assertIsNone(pagination["next_offset"])
                break
            self.assertEqual(pagination["next_offset"], offset + len(page["cells"]))
            offset = pagination["next_offset"]
        self.assertEqual(collected, expected["cells"])

    def test_last_and_empty_terminal_pages_are_explicit(self):
        total = len(self.native_map.hexes)
        page = self.adapter.dispatch("structured_map", {**self.args, "offset": total - 1})
        self.assertEqual(len(page["cells"]), 1)
        self.assertFalse(page["pagination"]["has_more"])
        empty = self.adapter.dispatch("structured_map", {**self.args, "offset": total})
        self.assertEqual(empty["cells"], [])
        self.assertIsNone(empty["pagination"]["next_offset"])
        with self.assertRaises(MapToolError) as error:
            self.adapter.dispatch("structured_map", {**self.args, "offset": total + 1})
        self.assertEqual(error.exception.code, "invalid_offset")

    def test_conversion_is_lazy_cached_and_results_are_detached(self):
        registry = {self.map_id: self.native_map}
        with patch("wargame_native_engine.agents.map_tool_calling.structure_map", wraps=structure_map) as convert:
            adapter = MapToolAdapter(registry)
            convert.assert_not_called()
            registry.clear()
            first = adapter.dispatch("structured_map", self.args)
            baseline = json.loads(json.dumps(first))
            first["cells"][0][-1][0] = 999
            first["cell_columns"].clear()
            first["pagination"]["next_offset"] = 999
            self.assertEqual(adapter.dispatch("structured_map", self.args), baseline)
            convert.assert_called_once()

    def test_registry_snapshots_nested_mutable_host_values(self):
        first = replace(self.native_map.hexes[0], roads=list(self.native_map.hexes[0].roads))
        native_map = replace(self.native_map, hexes=[first, *self.native_map.hexes[1:]])
        expected = structure_map(native_map)
        adapter = MapToolAdapter({self.map_id: native_map})
        first.roads[0] = 99
        native_map.hexes.pop()
        page = adapter.dispatch("structured_map", {**self.args, "limit": 256})
        self.assertEqual(page["cells"], expected["cells"])
        self.assertEqual(page["map_hash"], expected["map_hash"])

    def test_invalid_registered_maps_report_a_tool_error(self):
        adapter = MapToolAdapter({"empty": NativeMap("empty", 10, 10)})
        with self.assertRaises(MapToolError) as error:
            adapter.dispatch("structured_map", {**self.args, "map_id": "empty"})
        self.assertEqual(error.exception.code, "invalid_map")

    def test_input_shapes_types_and_limits_are_strict(self):
        invalid = [
            None, [], {}, {"map_id": self.map_id},
            {**self.args, "path": "/etc/passwd"},
            {**self.args, "map_id": ""}, {**self.args, "map_id": 123},
            {**self.args, "map_id": "m" * 129},
        ]
        invalid.extend({**self.args, "offset": value} for value in [-1, True, 1.0, "0", None])
        invalid.extend({**self.args, "limit": value} for value in [0, -1, True, 1.0, "2", 257, None])
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(MapToolError) as error:
                self.adapter.dispatch("structured_map", arguments)
            self.assertEqual(error.exception.code, "invalid_arguments")

    def test_registry_is_the_only_source_authority(self):
        for name, arguments, code in [
            ("read_file", self.args, "unknown_tool"),
            (None, self.args, "unknown_tool"),
            ("structured_map", {**self.args, "map_id": "../../etc/passwd"}, "unknown_map"),
            ("structured_map", {**self.args, "map_id": "another-map"}, "unknown_map"),
        ]:
            with self.subTest(name=name, arguments=arguments), self.assertRaises(MapToolError) as error:
                self.adapter.dispatch(name, arguments)
            self.assertEqual(error.exception.code, code)
        for maps in [None, [], {self.map_id: str(SOURCE)}]:
            with self.subTest(maps=maps), self.assertRaises(TypeError):
                MapToolAdapter(maps)
        with self.assertRaises(ValueError):
            MapToolAdapter({"alias": self.native_map})

    def test_only_static_map_facts_are_exposed(self):
        response = self.adapter.dispatch("structured_map", self.args)
        forbidden = {"operators", "deployment", "objectives", "scenario_id", "scenario_hash", "tick_index", "future_frames"}

        def inspect(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden & value.keys())
                for item in value.values():
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)

        inspect(response)
        self.assertNotIn(str(SOURCE), json.dumps(response))

    def test_response_byte_limit_fails_without_silent_truncation(self):
        adapter = MapToolAdapter({self.map_id: self.native_map}, max_response_bytes=32)
        with self.assertRaises(MapToolError) as error:
            adapter.dispatch("structured_map", self.args)
        self.assertEqual(error.exception.code, "response_too_large")
        self.assertEqual(len(self.adapter.dispatch("structured_map", self.args)["cells"]), 2)
        for limit in [0, -1, True, 1.5]:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                MapToolAdapter({}, max_response_bytes=limit)

    def test_function_schema_is_closed_and_deeply_detached(self):
        schema = function_schemas()
        self.assertEqual([entry["name"] for entry in schema], ["structured_map"])
        parameters = schema[0]["parameters"]
        self.assertEqual(set(parameters["required"]), {"map_id", "offset", "limit"})
        self.assertEqual(set(parameters["properties"]), set(parameters["required"]))
        self.assertFalse(parameters["additionalProperties"])
        parameters["properties"]["limit"]["maximum"] = 999
        self.assertEqual(function_schemas()[0]["parameters"]["properties"]["limit"]["maximum"], 256)

    def test_documented_host_example_executes(self):
        document = ROOT / "docs/map_structuring.md"
        match = re.search(r"```python\n(.*?)\n```", document.read_text(), re.S)
        self.assertIsNotNone(match)
        tree = ast.parse(match.group(1), filename=str(document))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "examples/source_movement_scenario.json":
                node.value = str(SOURCE)
        namespace = {"__name__": "__map_function_example__"}
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(tree, str(document), "exec"), namespace)
        self.assertEqual(namespace["received"], namespace["direct"]["cells"])


if __name__ == "__main__":
    unittest.main()
