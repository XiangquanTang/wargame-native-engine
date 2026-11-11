"""Serialization fast paths must preserve canonical bytes and detach data."""

import json
import unittest
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from wargame_native_engine.core.action import ActionType, EngineAction, ValidAction
from wargame_native_engine.gateway.decision_window import DecisionWindow
from wargame_native_engine.io.serialization import (
    canonical_hash,
    canonical_json,
    to_json_value,
)


class SerializationRuntimeTests(unittest.TestCase):
    def test_exact_containers_keep_key_conversion_order_and_values(self):
        value = {3: ("汉字", None, True, 1.5), "nested": [{False: 2}], "3": "last"}
        self.assertEqual(canonical_json(value), '{"3":"last","nested":[{"False":2}]}')
        detached = to_json_value(value)
        detached["nested"][0]["False"] = 9
        self.assertEqual(value["nested"][0][False], 2)

    def test_subclasses_and_special_values_keep_dispatch(self):
        class Number(Enum):
            ONE = 1

        class CustomDict(dict):
            pass

        @dataclass
        class Record:
            field: object

        value = Record(
            CustomDict(
                enum=Number.ONE,
                path=Path("abc"),
                proxy=MappingProxyType({1: [2]}),
                values={3, 1},
            )
        )
        self.assertEqual(
            json.loads(canonical_json(value)),
            {
                "field": {
                    "enum": 1,
                    "path": "abc",
                    "proxy": {"1": [2]},
                    "values": [1, 3],
                }
            },
        )
        with self.assertRaises(ValueError):
            canonical_json(float("nan"))
        with self.assertRaises(TypeError):
            canonical_json(object())

    def test_valid_action_serializes_once_without_changing_hash_or_aliasing(self):
        action = EngineAction("red", 1, ActionType.WAIT, parameters={"nested": [1]})
        valid = ValidAction(action, 99)
        record = valid.to_dict()
        self.assertEqual(
            record,
            {
                "action_id": action.action_id,
                "priority": 99,
                "source": valid.source,
                "action": action.to_dict(),
            },
        )
        self.assertEqual(
            record["action_id"], "action-" + canonical_hash(record["action"])[:24]
        )
        record["action"]["parameters"]["nested"].append(2)
        self.assertEqual(action.parameters, {"nested": [1]})
        action.parameters["nested"].append(3)
        self.assertNotEqual(valid.to_dict()["action_id"], record["action_id"])

    def test_custom_action_id_dispatch_remains_supported(self):
        class CustomAction(EngineAction):
            @property
            def action_id(self):
                return "custom-action"

        action = CustomAction("red", 1, ActionType.WAIT)
        self.assertEqual(
            ValidAction(action, 99).to_dict()["action_id"], "custom-action"
        )

    def test_window_records_are_fully_detached_and_side_only(self):
        red = ValidAction(
            EngineAction("red", 1, ActionType.WAIT, parameters={"nested": [1]}), 99
        )
        blue = ValidAction(EngineAction("blue", 2, ActionType.WAIT), 99)
        window = DecisionWindow.open(
            session_id="test",
            decision_window_index=0,
            opened_at_sim_time_ms=0,
            red_valid_actions=[red],
            blue_valid_actions=[blue],
        )
        first = window.action_records("red")
        first[0]["action"]["parameters"]["nested"].append(7)
        self.assertEqual(
            window.action_records("red")[0]["action"]["parameters"], {"nested": [1]}
        )
        self.assertEqual(window.action_records("blue")[0]["action"]["actor_id"], 2)
        red.action.parameters["nested"].append(8)
        self.assertEqual(
            window.action_records("red")[0]["action"]["parameters"], {"nested": [1]}
        )


if __name__ == "__main__":
    unittest.main()
