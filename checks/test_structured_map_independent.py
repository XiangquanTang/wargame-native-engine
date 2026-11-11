"""Independent adversarial and full-data checks for the static map boundary."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import sys
from unittest.mock import patch
import unittest

from wargame_native_engine.io.scenario import NativeHex, NativeMap, load_scenario
from wargame_native_engine.io.structured_map import load_structured_map, structure_map
from wargame_native_engine.agents.map_tool_calling import (
    MapToolAdapter, MapToolError, function_schemas,
)

ROOT = Path(__file__).resolve().parents[1]
MAP123 = ROOT / "examples/map123/scenario.json"
SMALL = ROOT / "examples/source_movement_scenario.json"


def records(export):
    columns = export["cell_columns"]
    return [dict(zip(columns, row)) for row in export["cells"]]


def fixture_map(*, elevation_kind="LEVEL", elevation_unit_m=10, elevation=2.5,
                terrain="FOREST", roadblocks=(307,)):
    return NativeMap(
        map_id="independent-sparse-map", width=1000, height=1000,
        hex_size_m=200, elevation_kind=elevation_kind,
        elevation_unit_m=elevation_unit_m, roadblocks=roadblocks,
        hexes=(
            NativeHex(408, None, "UNRECOGNIZED", (-1,) * 6,
                      roads=(0, 4, 0, 0, 0, 0), rivers=(0, 0, 2, 0, 0, 0)),
            NativeHex(307, elevation, terrain, (-1,) * 6,
                      roads=(0, 0, 0, 1, 0, 0), rivers=(0,) * 6),
        ),
    )


class IndependentStaticMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = json.loads(MAP123.read_text())
        cls.export = load_structured_map(MAP123, input_format="native-scenario")

    def test_map123_every_cell_matches_source_facts(self):
        self.assertEqual(self.export["cell_count"], 10000)
        self.assertEqual(len(self.export["cells"]), 10000)
        expected = sorted(self.original["map"]["hexes"], key=lambda cell: cell["pos"])
        for source, actual in zip(expected, records(self.export)):
            self.assertEqual(actual["x"], source["pos"] % 100)
            self.assertEqual(actual["y"], source["pos"] // 100)
            self.assertEqual(int(actual["position_id"]), source["pos"])
            self.assertEqual(actual["elevation_raw"], source["elev"])
            self.assertEqual(actual["elevation_m"], source["elev"])
            self.assertEqual(actual["elevation_level"], source["elev"] / 10)
            self.assertEqual(actual["terrain_code"], source["cond"])
            for field in ("neighbors", "roads", "rivers"):
                self.assertEqual(actual[field], source[field])
                self.assertEqual(len(actual[field]), 6)
        self.assertEqual(self.export["coordinates"]["direction_order"],
                         ["E", "NE", "NW", "W", "SW", "SE"])
        self.assertEqual({row["elevation_level"] for row in records(self.export)},
                         set(range(8)))
        self.assertEqual(self.export["elevation"]["level_count"], 8)

    def test_static_output_never_contains_scenario_state(self):
        text = json.dumps(self.export, ensure_ascii=False)
        for key in ("operators", "deployment", "objectives", "terminal_rules",
                    "controlling_side", "weapon_ids", "initial_unit_count"):
            self.assertNotIn(f'"{key}"', text)
        changed = copy.deepcopy(self.original)
        # These deliberately malformed fields must not be inspected by a
        # map-only reader or become part of static-map identity.
        for field in ("operators", "deployment", "objectives", "terminal_rules"):
            changed[field] = {"secret_marker": "NOT_STATIC_MAP"}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "changed.json"
            source.write_text(json.dumps(changed))
            exported = load_structured_map(source, input_format="native-scenario")
        self.assertEqual(exported["map_hash"], self.export["map_hash"])
        self.assertNotEqual(exported["source"]["sha256"], self.export["source"]["sha256"])
        self.assertNotIn("NOT_STATIC_MAP", json.dumps(exported))

    def test_source_file_hash_matches_exact_original_bytes(self):
        self.assertEqual(self.export["source"]["sha256"],
                         hashlib.sha256(MAP123.read_bytes()).hexdigest())
        self.assertRegex(self.export["map_hash"], r"^[0-9a-f]{64}$")

    def test_nonzero_sparse_origin_and_overlay_remain_independent(self):
        exported = structure_map(fixture_map())
        self.assertEqual(exported["coordinates"]["origin"], [7, 3])
        self.assertEqual(exported["bounds"],
                         {"min_x": 7, "max_x": 8, "min_y": 3, "max_y": 4})
        self.assertEqual(exported["dimensions"], {"rows": 2, "columns": 2})
        self.assertEqual(exported["cell_count"], 2)
        first, second = records(exported)
        self.assertEqual((first["x"], first["y"]), (7, 3))
        self.assertTrue(first["roadblock"])
        self.assertEqual(first["terrain_raw"], "FOREST")
        self.assertEqual(first["terrain_name"], "FOREST")
        self.assertFalse(second["roadblock"])
        self.assertEqual(second["terrain_raw"], "UNRECOGNIZED")
        self.assertIsNone(second["terrain_name"])
        self.assertEqual(second["roads"], [0, 4, 0, 0, 0, 0])
        self.assertEqual(second["rivers"], [0, 0, 2, 0, 0, 0])

    def test_nonintegral_or_unknown_elevation_never_rounds(self):
        for kind, step, raw, metres, level in (
            ("LEVEL", 10, 2.5, 25, None),
            ("LEVEL", None, 2, None, 2),
            ("METERS", 10, 25, 25, None),
            ("METERS", None, 25, 25, None),
            (None, 10, 25, None, None),
        ):
            with self.subTest(kind=kind, step=step, raw=raw):
                row = records(structure_map(fixture_map(
                    elevation_kind=kind, elevation_unit_m=step, elevation=raw)))[0]
                self.assertEqual(row["elevation_raw"], raw)
                self.assertEqual(row["elevation_m"], metres)
                self.assertEqual(row["elevation_level"], level)

    def test_basic_unknown_river_code_is_not_lost_or_guessed(self):
        path = ROOT / "examples/public_river_valley_map.json"
        original = json.loads(path.read_text())
        expected = sorted((cell for row in original["map_data"] for cell in row if cell),
                          key=lambda cell: cell["pos"])
        actual = records(load_structured_map(path, input_format="basic"))
        self.assertEqual(len(actual), 432)
        self.assertTrue(any(2 in cell["rivers"] for cell in expected))
        for source, row in zip(expected, actual):
            self.assertEqual(row["rivers"], source["rivers"])
            self.assertEqual(row["roads"], source["roads"])
            self.assertEqual(row["elevation_raw"], source["elev"])
            self.assertIsNone(row["elevation_m"])
            self.assertIsNone(row["elevation_level"])

    def test_missing_basic_facts_are_null_not_fabricated_zeroes(self):
        source = {"coordinate_system": "odd-r", "map_data": [[{"cond": 99}]]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "basic.json"
            path.write_text(json.dumps(source))
            row = records(load_structured_map(path, input_format="basic"))[0]
        self.assertEqual(row["terrain_raw"], 99)
        self.assertIsNone(row["terrain_name"])
        for field in ("elevation_raw", "elevation_level", "elevation_m",
                      "neighbors", "roads", "rivers"):
            self.assertIsNone(row[field])

    def test_invalid_json_numbers_and_duplicate_fields_are_rejected(self):
        texts = (
            '{"coordinate_system":"odd-r","map_data":[[{"elev":NaN}]]}',
            '{"coordinate_system":"odd-r","map_data":[[{"elev":Infinity}]]}',
            '{"coordinate_system":"odd-r","map_data":[[{"cond":0,"cond":1}]]}',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for text in texts:
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(ValueError):
                    load_structured_map(path, input_format="basic")

    def test_cli_static_document_matches_reassembled_map_pages(self):
        command = [sys.executable, "-m", "wargame_native_engine.cli.structure_map",
                   str(MAP123), "--input-format", "native-scenario"]
        result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True,
                                text=True)
        from_cli = json.loads(result.stdout)
        native = load_scenario(MAP123).map
        adapter = MapToolAdapter({native.map_id: native})
        combined = None
        offset = 0
        while True:
            page = adapter.dispatch("structured_map", {
                "map_id": native.map_id, "offset": offset, "limit": 256,
            })
            pagination = page.pop("pagination")
            if combined is None:
                combined = page
            else:
                combined["cells"].extend(page["cells"])
            if not pagination["has_more"]:
                break
            offset = pagination["next_offset"]
        # File provenance is deliberately distinct from host in-memory source.
        combined.pop("source")
        from_cli.pop("source")
        self.assertEqual(combined, from_cli)

    def test_continuous_blocked_positions_cannot_be_silently_discarded(self):
        original = json.loads(SMALL.read_text())["map"]
        original["blocked_positions"] = [[1, 2]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continuous-obstacles.json"
            path.write_text(json.dumps(original))
            with self.assertRaises(ValueError):
                load_structured_map(path, input_format="native-map")

    def test_basic_source_change_between_reads_is_detected(self):
        from wargame_native_engine.io import structured_map as module
        loader = module.load_viewer_map_config
        first = {"coordinate_system": "odd-r", "map_data": [[{"cond": 0}]]}
        second = {"coordinate_system": "odd-r", "map_data": [[{"cond": 1}]]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changing.json"
            path.write_text(json.dumps(first))
            def changed_source(*args, **kwargs):
                path.write_text(json.dumps(second))
                return loader(*args, **kwargs)
            with patch.object(module, "load_viewer_map_config", side_effect=changed_source):
                with self.assertRaises(ValueError):
                    load_structured_map(path, input_format="basic")

    def test_native_source_does_not_coerce_malformed_exact_fields(self):
        original = json.loads(SMALL.read_text())["map"]
        mutations = (
            ("map", "hex_size_m", 200.9),
            ("map", "hex_size_m", True),
            ("map", "roadblocks", [101.1]),
            ("cell", "pos", 101.9),
            ("cell", "pos", True),
            ("cell", "cond", True),
            ("cell", "elev", True),
            ("cell", "neighbors", [102.5, -1, -1, -1, -1, -1]),
            ("cell", "roads", [True, 0, 0, 0, 0, 0]),
            ("cell", "rivers", [0, 0, 0, 0, 0, -1]),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.native.json"
            for target, field, invalid in mutations:
                changed = copy.deepcopy(original)
                selected = changed if target == "map" else changed["hexes"][0]
                selected[field] = invalid
                path.write_text(json.dumps(changed))
                with self.subTest(target=target, field=field, invalid=invalid):
                    with self.assertRaises(ValueError):
                        load_structured_map(path, input_format="native-map")


class IndependentMapToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.native_map = load_scenario(MAP123).map

    def setUp(self):
        self.adapter = MapToolAdapter({self.native_map.map_id: self.native_map})

    def test_schemas_are_closed_detached_and_read_only(self):
        schema = function_schemas()
        self.assertEqual(len(schema), 1)
        self.assertEqual(schema[0]["name"], "structured_map")
        parameters = schema[0]["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(set(parameters["properties"]), {"map_id", "offset", "limit"})
        self.assertEqual(set(parameters["required"]), {"map_id", "offset", "limit"})
        parameters["properties"]["map_id"].clear()
        self.assertTrue(function_schemas()[0]["parameters"]["properties"]["map_id"])

    def test_paginated_reassembly_matches_full_structured_export(self):
        expected = structure_map(self.native_map)
        result_cells = []
        offset = 0
        while True:
            page = self.adapter.dispatch("structured_map", {
                "map_id": self.native_map.map_id, "offset": offset, "limit": 173,
            })
            pagination = page.pop("pagination")
            result_cells.extend(page.pop("cells"))
            self.assertEqual(page, {key: value for key, value in expected.items()
                                    if key != "cells"})
            self.assertEqual(pagination["offset"], offset)
            if not pagination["has_more"]:
                self.assertIsNone(pagination["next_offset"])
                break
            offset = pagination["next_offset"]
        self.assertEqual(result_cells, expected["cells"])
        terminal = self.adapter.dispatch("structured_map", {
            "map_id": self.native_map.map_id, "offset": 10000, "limit": 173,
        })
        self.assertEqual(terminal["cells"], [])
        self.assertFalse(terminal["pagination"]["has_more"])

    def test_model_cannot_select_file_paths_or_arbitrary_attributes(self):
        base = {"map_id": self.native_map.map_id, "offset": 0, "limit": 1}
        for key in ("path", "source_path", "output", "__class__", "coordinate_system_override"):
            with self.subTest(key=key), self.assertRaises(MapToolError):
                self.adapter.dispatch("structured_map", {**base, key: "../../secret"})
        for name in ("../../etc/passwd", "__dict__", "load_structured_map"):
            with self.subTest(name=name), self.assertRaises(MapToolError):
                self.adapter.dispatch(name, base)
        for unknown in ("../../etc/passwd", "/etc/passwd", "file:///etc/passwd"):
            with self.subTest(unknown=unknown), self.assertRaises(MapToolError):
                self.adapter.dispatch("structured_map", {**base, "map_id": unknown})

    def test_invalid_offsets_limits_and_types_reject_before_reading(self):
        base = {"map_id": self.native_map.map_id, "offset": 0, "limit": 1}
        for key, value in (("offset", -1), ("offset", 0.5), ("offset", True),
                           ("offset", 10001), ("limit", 0), ("limit", 257),
                           ("limit", False), ("limit", 2.5), ("limit", "1"),
                           ("map_id", 123), ("map_id", None)):
            with self.subTest(key=key, value=value), self.assertRaises(MapToolError):
                self.adapter.dispatch("structured_map", {**base, key: value})
        for arguments in (None, [], "{}", {"map_id": self.native_map.map_id}):
            with self.subTest(arguments=arguments), self.assertRaises(MapToolError):
                self.adapter.dispatch("structured_map", arguments)

    def test_returned_page_mutation_never_mutates_snapshot(self):
        args = {"map_id": self.native_map.map_id, "offset": 0, "limit": 2}
        first = self.adapter.dispatch("structured_map", args)
        expected = copy.deepcopy(first)
        first["cells"][0][0] = -100
        first["cell_columns"].clear()
        first["coordinates"].clear()
        self.assertEqual(self.adapter.dispatch("structured_map", args), expected)
