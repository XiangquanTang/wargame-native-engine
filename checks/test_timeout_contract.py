"""Invalid wall-clock budgets must fail before starting or consuming a run."""

import unittest
from threading import TIMEOUT_MAX

from wargame_native_engine.agents._timeouts import validated_timeout
from wargame_native_engine.agents.realtime import RealtimeAgentSession
from wargame_native_engine.agents.realtime_runner import RealtimeLocalAgentRunner


class TimeoutContractTests(unittest.TestCase):
    def test_positive_finite_values_are_normalized(self):
        self.assertEqual(validated_timeout(1), 1.0)
        self.assertEqual(validated_timeout(0.125), 0.125)
        self.assertEqual(validated_timeout(TIMEOUT_MAX), TIMEOUT_MAX)
        self.assertIsNone(validated_timeout(None, allow_none=True))

    def test_invalid_values_rejected_without_numeric_overflow(self):
        invalid = (
            None, True, False, 0, -1, float("nan"), float("inf"),
            -float("inf"), TIMEOUT_MAX * 2, 10 ** 1000, "30", object(),
        )
        for value in invalid:
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                validated_timeout(value)

    def test_invalid_run_budget_does_not_consume_single_use_runner(self):
        # Invalid inputs must return before touching session or filesystem.
        runner = object.__new__(RealtimeLocalAgentRunner)
        runner._ran = False
        for value in (float("nan"), float("inf"), TIMEOUT_MAX * 2):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    runner.run("must-not-be-created", max_ticks=1, timeout_seconds=value)
                self.assertFalse(runner._ran)

    def test_invalid_shutdown_budget_does_not_consume_runner(self):
        runner = object.__new__(RealtimeLocalAgentRunner)
        runner._ran = False
        for value in (True, float("nan"), float("inf"), TIMEOUT_MAX * 2):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    runner.run(
                        "must-not-be-created", max_ticks=1,
                        shutdown_timeout_seconds=value,
                    )
                self.assertFalse(runner._ran)

    def test_invalid_session_stop_budget_fails_before_changing_admission(self):
        # Deliberately uninitialized: the timeout contract runs before locks,
        # events, admission changes or any attempt to join worker threads.
        session = object.__new__(RealtimeAgentSession)
        for value in (True, float("nan"), float("inf"), TIMEOUT_MAX * 2):
            with self.subTest(value=value), self.assertRaises(ValueError):
                session.stop_realtime(timeout_seconds=value)


if __name__ == "__main__":
    unittest.main()
