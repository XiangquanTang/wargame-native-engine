"""Regression coverage for detached, seat-isolated runtime read models."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from wargame_native_engine.agents.realtime import (
    CommandStatus,
    ManualRealtimeClock,
    RealtimeAgentSession,
    RealtimeAgentSessionError,
)
from wargame_native_engine.agents.seat import SeatDecision, SeatOperation
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.operations import ScenarioMatchFactory
from wargame_native_engine.viewer.realtime_control_server import (
    RealtimeDualControlHTTPServer,
)


class RuntimeStatusTests(unittest.TestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        self.session = RealtimeAgentSession(
            ScenarioMatchFactory(
                bundle=ScenarioBundle.from_path(
                    "examples/public_river_valley_scenario.json"
                ),
                storage_root=Path(self.storage.name),
                red_controller_id="red",
                blue_controller_id="blue",
                decision_interval_ms=1000,
                frame_interval_ms=1000,
            ),
            run_namespace="status-tests",
            tick_interval_ms=1000,
            clock=ManualRealtimeClock(),
        )
        self.addCleanup(self.session.close)
        self.reset = self.session.reset(seed=42)
        self.bindings = (self.reset.red_binding, self.reset.blue_binding)

    def assert_summary_matches_ledger(self):
        for binding in self.bindings:
            summary = self.session.status_snapshot(binding)
            events = [
                event
                for event in self.session.command_events
                if event.seat == binding.seat
            ]
            self.assertEqual(
                summary.pending_command_count,
                sum(event.status is CommandStatus.PENDING for event in events),
            )
            if events:
                latest = events[-1]
                self.assertEqual(summary.last_command.status, latest.status)
                self.assertEqual(summary.last_command.error_code, latest.error_code)
                self.assertEqual(
                    summary.last_command.applied_frame_index, latest.applied_frame_index
                )
            else:
                self.assertIsNone(summary.last_command)
            view, controller = self.session.controller_snapshot(binding)
            self.assertEqual(summary.observation_version, view.observation_version)
            self.assertEqual(summary.frame_index, view.frame_index)
            self.assertEqual(summary.winner, controller.winner)

    def submit(self, binding, version=None):
        return self.session.submit(
            binding,
            observation_version=(
                self.session.latest_view(binding).observation_version
                if version is None
                else version
            ),
            decision=SeatDecision(),
        )

    def test_summary_tracks_all_lifecycle_transitions_and_reset(self):
        self.assert_summary_matches_ledger()
        for binding in self.bindings:
            self.submit(binding)
            self.submit(binding)  # supersede, do not double count
            self.assert_summary_matches_ledger()
        self.submit(self.bindings[0], 999999)  # rejected submission
        self.assert_summary_matches_ledger()
        self.session.advance_tick()
        self.assert_summary_matches_ledger()
        self.submit(self.bindings[1])
        self.session.stop_accepting()
        self.assert_summary_matches_ledger()
        self.reset = self.session.reset(seed=42)
        self.bindings = (self.reset.red_binding, self.reset.blue_binding)
        self.assert_summary_matches_ledger()

    def test_recorded_admission_and_rejected_apply_keep_summary(self):
        red = self.bindings[0]
        version = self.session.latest_view(red).observation_version
        for sequence in (0, 1):
            self.session.submit_recorded(
                red,
                command_sequence=sequence,
                observation_version=version,
                target_tick=0,
                decision=SeatDecision(),
            )
            self.assert_summary_matches_ledger()
        self.session.submit_operation(
            red,
            observation_version=version,
            operation=SeatOperation("MOVE", actor_ids=(999999,), destination_hex=405),
        )
        self.session.advance_tick()
        self.assertEqual(
            self.session.command_events[-1].status, CommandStatus.REJECTED_AT_APPLY
        )
        self.assert_summary_matches_ledger()

    def test_receipt_lookup_and_binding_isolation(self):
        receipt = self.submit(self.bindings[0])
        sequence = receipt.command_sequence
        event = self.session.command_event(self.bindings[0], sequence)
        self.assertIsNotNone(event)
        self.assertIsNot(event, self.session.command_event(self.bindings[0], sequence))
        self.assertIsNone(self.session.command_event(self.bindings[1], sequence))
        self.assertIsNone(self.session.command_event(self.bindings[0], 999999))
        forged = replace(self.bindings[0], seat="blue")
        with self.assertRaises(RealtimeAgentSessionError):
            self.session.status_snapshot(forged)
        with self.assertRaises(RealtimeAgentSessionError):
            self.session.command_event(forged, sequence)

    def test_opt_in_current_version_check_preserves_legacy_contract(self):
        red = self.bindings[0]
        old_version = self.session.latest_view(red).observation_version
        self.session.advance_tick()
        with self.assertRaises(RealtimeAgentSessionError) as error:
            self.session.submit_operation_if_current(
                red,
                observation_version=old_version,
                operation=SeatOperation("WAIT", whole_side_wait=True),
            )
        self.assertEqual(error.exception.code, "observation_version_stale")
        self.assertEqual(len(self.session.command_events), 0)
        self.assertTrue(self.submit(red, old_version).accepted)
        current = self.session.latest_view(red).observation_version
        self.assertTrue(
            self.session.submit_operation_if_current(
                red,
                observation_version=current,
                operation=SeatOperation("WAIT", whole_side_wait=True),
            ).accepted
        )
        self.assert_summary_matches_ledger()

    def test_pending_includes_frozen_inflight_commands(self):
        red = self.bindings[0]
        self.submit(red)
        entered, release = Event(), Event()
        service = self.session._services["red"]
        preview = service.preview_decision
        errors = []

        def paused_preview(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test synchronization timeout")
            return preview(*args, **kwargs)

        def advance():
            try:
                self.session.advance_tick()
            except Exception as error:  # noqa: BLE001 - report worker failures in the test thread
                errors.append(error)

        with patch.object(service, "preview_decision", paused_preview):
            worker = Thread(target=advance)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.session._pending, {})
                self.assertEqual(
                    self.session.status_snapshot(red).pending_command_count, 1
                )
                self.assert_summary_matches_ledger()
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
        self.assertEqual(self.session.status_snapshot(red).pending_command_count, 0)
        self.assert_summary_matches_ledger()

    def test_http_status_shape_preserved_without_large_reads(self):
        server = object.__new__(RealtimeDualControlHTTPServer)
        server.realtime_session = self.session
        server.bindings = dict(zip(("red", "blue"), self.bindings))
        for binding in self.bindings:
            self.submit(binding)
            view, controller = self.session.controller_snapshot(binding)
            self.assertEqual(
                server.status(binding.seat, binding),
                server.status_from_snapshot(binding.seat, view, controller),
            )


if __name__ == "__main__":
    unittest.main()
