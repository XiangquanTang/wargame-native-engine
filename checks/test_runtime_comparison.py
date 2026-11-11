"""Performance reports must not silently compare different rules or outcomes."""

from copy import deepcopy
import unittest

from benchmarks.compare_runtime_results import compare, FACT_HASHES, IDENTICAL_INPUTS


class RuntimeComparisonTests(unittest.TestCase):
    def fixture(self):
        value = {key: 'same' for key in (*IDENTICAL_INPUTS, *FACT_HASHES)}
        value.update(source_tree_sha256='control', tick={
            'count': 100, 'p50_ms': 10, 'p95_ms': 20,
        })
        return value

    def test_same_inputs_and_facts_allow_different_source_and_latency(self):
        control = self.fixture()
        candidate = deepcopy(control)
        candidate['source_tree_sha256'] = 'optimized'
        candidate['tick']['p50_ms'] = 8
        summary = compare(control, candidate)
        self.assertAlmostEqual(summary['tick']['p50_ms']['reduction_percent'], 20)
        self.assertEqual(summary['candidate_source_sha256'], 'optimized')

    def test_every_input_and_fact_mismatch_is_rejected(self):
        for key in (*IDENTICAL_INPUTS, *FACT_HASHES):
            with self.subTest(field=key):
                control = self.fixture()
                candidate = deepcopy(control)
                candidate[key] = 'different'
                with self.assertRaisesRegex(ValueError, key):
                    compare(control, candidate)

    def test_different_tick_counts_are_rejected(self):
        control = self.fixture()
        candidate = deepcopy(control)
        candidate['tick']['count'] = 99
        with self.assertRaisesRegex(ValueError, 'tick count'):
            compare(control, candidate)


if __name__ == '__main__':
    unittest.main()
