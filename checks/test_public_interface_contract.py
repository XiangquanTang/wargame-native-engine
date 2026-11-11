"""Keep the canonical integration example and exported tool boundary executable."""

import ast
import contextlib
import io
from pathlib import Path
import re
import unittest

from wargame_native_engine.agents.tool_calling import function_schemas
from wargame_native_engine.operations.product import PRODUCT_OPERATION_TYPES

ROOT = Path(__file__).resolve().parents[1]


class PublicInterfaceContractTests(unittest.TestCase):
    def test_documented_host_example_runs_through_command_result(self):
        document = ROOT / "docs/game_tool_calling.md"
        source = re.search(r"```python\n(.*?)\n```", document.read_text(), re.S)
        self.assertIsNotNone(source, "canonical host example is missing")
        # Keep this independent of the test runner's working directory without
        # changing process-wide cwd (other integration checks may run in parallel).
        tree = ast.parse(source.group(1), filename=str(document))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "examples/source_movement_scenario.json":
                node.value = str(ROOT / node.value)
        namespace = {"__name__": "__interface_example__"}
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(tree, str(document), "exec"), namespace)
        self.assertTrue(namespace["receipt"]["accepted"])
        self.assertEqual(namespace["feedback"]["command"]["status"], "APPLIED")

    def test_exported_tools_have_closed_argument_shapes(self):
        schemas = function_schemas()
        self.assertEqual(
            [schema["name"] for schema in schemas],
            ["game_observation", "game_capabilities", "preview_game_operation",
             "submit_game_operation", "game_command_result",
             "game_region_visibility", "game_weapon_catalog", "game_fire_effect",
             "game_visibility_range", "game_attack_range"],
        )
        for schema in schemas:
            with self.subTest(tool=schema["name"]):
                parameters = schema["parameters"]
                self.assertFalse(parameters["additionalProperties"])
                self.assertLessEqual(set(parameters["required"]), set(parameters["properties"]))
                if schema in schemas[:5]:
                    self.assertEqual(set(parameters["required"]), set(parameters["properties"]))
        operation = schemas[2]["parameters"]["properties"]["operation"]
        self.assertFalse(operation["additionalProperties"])
        self.assertEqual(set(operation["required"]), {"operation_type", "actor_ids"})
        self.assertNotIn("confirm_partial", operation["properties"])

    def test_operation_enum_uses_domain_authority_and_is_deeply_detached(self):
        schemas = function_schemas()
        operation = schemas[2]["parameters"]["properties"]["operation"]
        kinds = operation["properties"]["operation_type"]["enum"]
        self.assertEqual(kinds, list(PRODUCT_OPERATION_TYPES))
        kinds.clear()
        operation["properties"]["actor_ids"]["items"]["minimum"] = -1
        fresh = function_schemas()
        fresh_operation = fresh[2]["parameters"]["properties"]["operation"]
        self.assertEqual(fresh_operation["properties"]["operation_type"]["enum"], list(PRODUCT_OPERATION_TYPES))
        self.assertEqual(fresh_operation["properties"]["actor_ids"]["items"]["minimum"], 0)
        self.assertEqual(fresh[1]["parameters"]["properties"]["actor_ids"]["items"]["minimum"], 0)


if __name__ == "__main__":
    unittest.main()
