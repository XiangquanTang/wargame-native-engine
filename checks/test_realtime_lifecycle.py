"""Deterministic race tests for episode construction and closure."""

import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from wargame_native_engine.agents.realtime import (
    ManualRealtimeClock,
    RealtimeAgentSession,
    RealtimeAgentSessionError,
    RealtimeLifecycleState,
)
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.operations import (
    ProductOperationService,
    ScenarioMatchFactory,
)
from wargame_native_engine.operations.facade import MatchHandle

ROOT = Path(__file__).resolve().parents[1]


class RealtimeLifecycleTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.factory = ScenarioMatchFactory(
            bundle=ScenarioBundle.from_path(
                ROOT / "examples/source_movement_scenario.json"
            ),
            storage_root=Path(storage.name),
            red_controller_id="red",
            blue_controller_id="blue",
            decision_interval_ms=1000,
            frame_interval_ms=1000,
        )

    def make_session(self, factory=None):
        session = RealtimeAgentSession(
            factory or self.factory,
            run_namespace="lifecycle-tests",
            tick_interval_ms=1000,
            clock=ManualRealtimeClock(),
        )
        self.addCleanup(session.close)
        return session

    def start_blocked_reset(self):
        entered, release = Event(), Event()
        constructed, outcome = [], []

        def factory(seed, sequence):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test factory barrier was not released")
            match = self.factory(seed, sequence)
            constructed.append(match)
            return match

        session = self.make_session(factory)

        def reset():
            try:
                outcome.append(session.reset(seed=42))
            except BaseException as error:  # noqa: BLE001 - capture worker outcome
                outcome.append(error)

        worker = Thread(target=reset, daemon=True)
        worker.start()

        def finish():
            release.set()
            worker.join(5)

        self.addCleanup(finish)
        self.assertTrue(entered.wait(5))
        return session, release, worker, constructed, outcome

    def test_close_wins_over_in_flight_factory_and_disposes_match(self):
        session, release, worker, constructed, outcome = self.start_blocked_reset()
        closed = []
        original_close = MatchHandle.close

        def close_match(match):
            closed.append(match.session_id)
            original_close(match)

        with patch.object(MatchHandle, "close", close_match):
            # This must return without waiting for the blocked factory.
            session.close()
            self.assertEqual(session.lifecycle, RealtimeLifecycleState.CLOSED)
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(constructed), 1)
        self.assertEqual(closed, [constructed[0].session_id])
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], RealtimeAgentSessionError)
        self.assertEqual(outcome[0].code, "realtime_session_closed")
        self.assertEqual(session.lifecycle, RealtimeLifecycleState.CLOSED)
        self.assertIsNone(session._match)
        with self.assertRaises(RealtimeAgentSessionError):
            session.reset(seed=43)

    def test_concurrent_reset_is_rejected_without_starting_second_factory(self):
        session, release, worker, constructed, outcome = self.start_blocked_reset()
        with self.assertRaises(RealtimeAgentSessionError) as raised:
            session.reset(seed=99)
        self.assertEqual(raised.exception.code, "realtime_reset_in_progress")
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(constructed), 1)
        self.assertEqual(len(outcome), 1)
        self.assertNotIsInstance(outcome[0], BaseException)
        self.assertEqual(session.lifecycle, RealtimeLifecycleState.ACCEPTING)
        self.assertEqual(session.episode_id, outcome[0].episode_id)
        # The guard is released, not a permanent prohibition on future resets.
        second = session.reset(seed=99)
        self.assertNotEqual(second.episode_id, outcome[0].episode_id)

    def test_failed_factory_invalidates_old_bindings_and_allows_retry(self):
        attempts = 0

        def factory(seed, sequence):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise RuntimeError("expected factory failure")
            return self.factory(seed, sequence)

        session = self.make_session(factory)
        old = session.reset(seed=42)
        with self.assertRaisesRegex(RuntimeError, "expected factory failure"):
            session.reset(seed=42)
        with self.assertRaises(RealtimeAgentSessionError) as raised:
            session.latest_view(old.red_binding)
        self.assertEqual(raised.exception.code, "realtime_binding_invalid")
        current = session.reset(seed=42)
        self.assertNotEqual(current.episode_id, old.episode_id)
        self.assertEqual(
            session.latest_view(current.red_binding).observation_version,
            current.red.observation_version,
        )

    def test_partial_service_construction_closes_already_created_service(self):
        session = self.make_session()
        created = []

        def service(controller):
            if created:
                raise RuntimeError("expected service failure")
            instance = ProductOperationService(controller)
            created.append(instance)
            return instance

        with patch(
            "wargame_native_engine.agents.realtime.ProductOperationService",
            side_effect=service,
        ), self.assertRaisesRegex(RuntimeError, "expected service failure"):
            session.reset(seed=42)
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0]._closed)
        self.assertIsNone(session._match)
        session.reset(seed=42)

    def test_failed_initial_view_cannot_leave_accepting_partial_episode(self):
        session = self.make_session()
        created = []

        def service(controller):
            instance = ProductOperationService(controller)
            created.append(instance)
            return instance

        with patch.object(
            session,
            "_issue_views_locked",
            side_effect=RuntimeError("expected initial view failure"),
        ), patch(
            "wargame_native_engine.agents.realtime.ProductOperationService",
            side_effect=service,
        ), self.assertRaisesRegex(RuntimeError, "expected initial view failure"):
            session.reset(seed=42)
        self.assertEqual(session.lifecycle, RealtimeLifecycleState.FAILED)
        self.assertIsNone(session._match)
        self.assertIsNone(session._tick_history)
        self.assertFalse(session._bindings)
        self.assertEqual(len(created), 4)
        self.assertTrue(all(item._closed for item in created))
        with self.assertRaises(RealtimeAgentSessionError) as raised:
            _ = session.episode_id
        self.assertEqual(raised.exception.code, "realtime_not_reset")
        session.reset(seed=43)
        self.assertEqual(session.lifecycle, RealtimeLifecycleState.ACCEPTING)


if __name__ == "__main__":
    unittest.main()
