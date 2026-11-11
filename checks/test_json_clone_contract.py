"""The fast clone is observationally equivalent to the canonical JSON round trip."""

import json
import math
import random
import unittest
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from wargame_native_engine.io.serialization import canonical_json, json_clone


def reference_clone(value):
    return json.loads(canonical_json(value))


class JsonCloneContractTests(unittest.TestCase):
    def assert_round_trip(self, value):
        expected = reference_clone(value)
        actual = json_clone(value)
        self.assertEqual(actual, expected)
        # Compare insertion order, normalized scalar types and signed zero too.
        self.assertEqual(
            json.dumps(actual, ensure_ascii=True),
            json.dumps(expected, ensure_ascii=True),
        )

    def test_native_trees_keep_order_types_and_numeric_precision(self):
        for value in (
            {"z": [1, True, None], "a": ("汉字", "\ud800", -0.0)},
            [math.nextafter(1.0, 2.0), 5e-324, 1.7976931348623157e308],
            {"é": 1, "aa": 2, "a": 3, "😀": 4},
            [2**2048, -(2**2048), 2**4096],
        ):
            with self.subTest(value=type(value).__name__):
                self.assert_round_trip(value)

    def test_special_values_and_subclasses_retain_reference_dispatch(self):
        class Number(Enum):
            ONE = 1

        class String(str):
            pass

        class NumberValue(int):
            pass

        class CustomDict(dict):
            pass

        @dataclass
        class Record:
            value: object

        for value in (
            {1: "integer", "1": "string", False: (1, 2)},
            {String("b"): NumberValue(4), "a": Number.ONE},
            CustomDict(z=1, a=[2]),
            {"nested": Record(MappingProxyType({"z": Path("test")}))},
            {"set": {3, 1, 2}, "frozen": frozenset({"a", "b"})},
        ):
            self.assert_round_trip(value)

    def test_mutable_aliases_are_detached_independently(self):
        shared = {"nested": [1]}
        original = {"b": shared, "a": shared}
        cloned = json_clone(original)
        cloned["a"]["nested"].append(2)
        self.assertEqual(original["a"]["nested"], [1])
        self.assertEqual(cloned["b"]["nested"], [1])

    def test_invalid_values_keep_exception_type(self):
        cyclic = []
        cyclic.append(cyclic)
        for value in (float("nan"), float("inf"), -float("inf"), object(), cyclic,
            {"a": float("nan"), "z": object()},
            {"z": object(), "a": float("nan")},):
            try:
                reference_clone(value)
            except (ValueError, TypeError, RecursionError) as error:
                with self.subTest(kind=type(error).__name__), self.assertRaises(type(error)):
                    json_clone(value)
            else:
                self.fail("reference unexpectedly accepted invalid input")

    def test_seeded_nested_trees_match_reference(self):
        rng = random.Random(417)

        def tree(depth):
            if depth == 0 or rng.random() < 0.45:
                return rng.choice([None, True, False, -0.0, 2.5, -7, "α", ""])
            if rng.random() < 0.5:
                return [tree(depth - 1) for _ in range(rng.randrange(5))]
            return {
                str(rng.randrange(20)): tree(depth - 1)
                for _ in range(rng.randrange(5))
            }

        for _ in range(500):
            self.assert_round_trip(tree(5))


if __name__ == "__main__":
    unittest.main()
