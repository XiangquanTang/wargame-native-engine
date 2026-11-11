"""Opt-in snapshot copies preserve deepcopy isolation and reference topology."""

import unittest
from copy import deepcopy
from dataclasses import dataclass, field

from wargame_native_engine.io.detached import detached_copy, detached_record


@detached_record
@dataclass(frozen=True, slots=True)
class Record:
    payload: object
    marker: str = field(default="constructed", init=False)


class DetachedCopyTests(unittest.TestCase):
    def test_nested_mutable_record_is_detached_without_constructor(self):
        value = Record({"b": [1], "a": (2, 3)})
        copied = detached_copy(value)
        self.assertEqual(copied, deepcopy(value))
        self.assertIsNot(copied, value)
        self.assertEqual(list(copied.payload), ["b", "a"])
        copied.payload["b"].append(7)
        self.assertEqual(value.payload["b"], [1])
        self.assertEqual(copied.marker, "constructed")

    def test_aliases_and_record_cycles_are_preserved(self):
        shared = []
        record = Record(shared)
        shared.extend([record, shared])
        copied = detached_copy([record, record, shared])
        self.assertIs(copied[0], copied[1])
        self.assertIs(copied[0].payload, copied[2])
        self.assertIs(copied[2][0], copied[0])
        self.assertIs(copied[2][1], copied[2])
        self.assertIsNot(copied[2], shared)

    def test_tuple_list_cycle_matches_deepcopy(self):
        items = []
        value = (items,)
        items.append(value)
        cloned = detached_copy(value)
        expected = deepcopy(value)
        self.assertIs(cloned[0][0], cloned)
        self.assertIs(expected[0][0], expected)
        self.assertIsNot(cloned, value)
        atomic = (1, "two", (3.0,))
        self.assertIs(detached_copy(atomic), atomic)

    def test_unknown_and_subclass_custom_protocols_are_preserved(self):
        class Custom:
            def __deepcopy__(self, memo):
                return {"custom_copy": True}

        class RecordSubclass(Record):
            def __deepcopy__(self, memo):
                return {"subclass_copy": True}

        class ListSubclass(list):
            def __deepcopy__(self, memo):
                return ["list_subclass"]

        value = [Custom(), RecordSubclass([]), ListSubclass([1])]
        self.assertEqual(detached_copy(value), deepcopy(value))

    def test_mixed_registered_and_unknown_records_preserve_shared_children(self):
        @dataclass
        class Unregistered:
            child: object

        child = [1]
        values = [Record(child), Unregistered(child), child]
        result = detached_copy(values)
        self.assertIs(result[0].payload, result[1].child)
        self.assertIs(result[0].payload, result[2])
        self.assertIsNot(result[2], child)

    def test_unsupported_record_registration_is_rejected(self):
        @dataclass
        class Mutable:
            value: object

        @dataclass(frozen=True, slots=True)
        class CustomState:
            value: object

            def __getstate__(self):
                return {"custom": self.value}

        for cls in (Mutable, CustomState):
            with self.subTest(cls=cls), self.assertRaises(TypeError):
                detached_record(cls)


if __name__ == "__main__":
    unittest.main()
