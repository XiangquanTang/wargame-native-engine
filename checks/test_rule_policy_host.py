"""Host ownership, deterministic batching, and failure publication contracts."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from http import HTTPStatus
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import signal
import tempfile
from threading import Event, Lock, Thread
from time import monotonic
import unittest
from unittest.mock import patch

from scripts import run_maps, run_realtime_dual_control as launcher
from scripts.rule_policy_host import _deferred_sigint, _StopRequest, run_policy_loop
from wargame_native_engine.agents.realtime import (
    CommandStatus, ManualRealtimeClock, RealtimeAgentSession,
)
from wargame_native_engine.agents.seat import ScriptedSeatAgent, SeatDecision, SeatOperation
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.operations import ProductOperationService, ScenarioMatchFactory
from wargame_native_engine.viewer.realtime_control_server import (
    RealtimeDualControlHTTPServer, RealtimeDualControlRequestHandler,
)

ROOT = Path(__file__).resolve().parents[1]


class RulePolicyHostTests(unittest.TestCase):
    def make_session(self, *, second_red=False, tick_interval_ms=1000):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        record = json.loads((ROOT / "examples/source_movement_scenario.json").read_text())
        if second_red:
            extra = deepcopy(record["operators"][0])
            extra["operator_id"] = 3
            record["operators"].append(extra)
            record["deployment"]["positions"]["red"]["3"] = [1, 1]
        bundle = ScenarioBundle.from_scenario(scenario_from_dict(record))
        session = RealtimeAgentSession(
            ScenarioMatchFactory(
                bundle=bundle, storage_root=Path(storage.name),
                red_controller_id="red", blue_controller_id="blue",
                decision_interval_ms=tick_interval_ms, frame_interval_ms=tick_interval_ms,
                replay_mode="compact",
            ), run_namespace="host-test", tick_interval_ms=tick_interval_ms,
            clock=ManualRealtimeClock(),
        )
        self.addCleanup(session.close)
        reset = session.reset(seed=42)
        return session, {"red": reset.red_binding, "blue": reset.blue_binding}

    def test_both_sides_same_tick_one_bundle_and_empty_decisions_do_not_clobber(self):
        session, bindings = self.make_session(second_red=True)
        policies = {
            "red": ScriptedSeatAgent("red-policy", [SeatDecision(tuple(
                SeatOperation("WEAPON_LOCK", actor_ids=(actor,), weapon_choices=((actor, 36),))
                for actor in (1, 3)
            ))]),
            "blue": ScriptedSeatAgent("blue-policy", [SeatDecision.single(
                SeatOperation("WEAPON_LOCK", actor_ids=(2,), weapon_choices=((2, 29),))
            )]),
        }
        reason, report = run_policy_loop(session, bindings, policies, max_ticks=3, paced=False)
        self.assertEqual(reason.value, "runner_limit")
        self.assertEqual(session.tick_count, 3)
        self.assertEqual(len(session.command_events), 2)
        self.assertEqual({event.target_tick for event in session.command_events}, {0})
        self.assertEqual({event.status for event in session.command_events}, {CommandStatus.APPLIED})
        self.assertEqual(report["policies"]["red"]["operations_in_applied_commands"], {"WEAPON_LOCK": 2})
        self.assertEqual(report["policies"]["blue"]["operations_in_applied_commands"], {"WEAPON_LOCK": 1})

    def test_current_side_inventory_and_visible_hexes_are_detached(self):
        session, bindings = self.make_session()
        captured = {}

        class CaptureAgent(ScriptedSeatAgent):
            def act(self, *, context):
                captured[context.side] = context
                return SeatDecision()

        run_policy_loop(session, bindings, {
            side: CaptureAgent(side, []) for side in bindings
        }, max_ticks=1, paced=False)
        for side, context in captured.items():
            summary = context.operation_capabilities.operation_summary
            own = {unit["operator_id"] for unit in context.observation["friendly_operators"]}
            visible = {unit["operator_id"] for unit in context.observation["visible_enemies"]}
            self.assertEqual({unit["operator_id"] for unit in summary["policy_own_weapon_inventory"]}, own)
            self.assertLessEqual(set(map(int, summary["policy_visible_enemy_hexes"])), visible)
            self.assertTrue(summary["policy_weapon_catalog"]["weapons"])
            summary["policy_own_weapon_inventory"].clear()
            self.assertTrue(session.controller_snapshot(bindings[side])[1].own_weapon_inventory)

    def test_apply_rejection_stops_once_and_launcher_saves_partial_replay(self):
        bad = SeatDecision.single(SeatOperation("MOVE", actor_ids=(999,), destination_hex=102))
        policies = {"red": ScriptedSeatAgent("bad-red", [bad, bad])}
        with tempfile.TemporaryDirectory() as directory, \
                patch("scripts.rule_policy_host.create_simple_policies", return_value=policies), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            output = Path(directory) / "output"
            code = launcher.main([
                "--scenario-bundle", str(ROOT / "examples/source_movement_scenario.json"),
                "--output-dir", str(output), "--run-namespace", "bad-policy",
                "--red-policy", "simple-capture", "--blue-policy", "simple-capture",
                "--max-ticks", "5", "--fast",
            ])
            self.assertEqual(code, 2)
            self.assertTrue((output / "replay.zip").is_file())
            report = json.loads((output / "policy-summary.json").read_text())
            self.assertEqual(report["tick_count"], 1)
            self.assertEqual(report["stop_reason"], "failure")
            self.assertEqual(report["non_applied_command_count"], 1)
            self.assertEqual(report["command_status_counts"], {"REJECTED_AT_APPLY": 1})
            self.assertTrue(report["non_applied_commands_first_100"][0]["error_code"])

    def test_fast_validation_precedes_map_preparation(self):
        for options in (["--fast"], ["--max-ticks", "0"],
                        ["--fast", "--max-ticks", "2", "--red-policy", "simple-capture"]):
            with self.subTest(options=options), \
                    patch("wargame_native_engine.data.scenario_workspace.prepare_source_game") as prepare, \
                    redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    run_maps.main(["start", "--input", "map.zip", "--scenario-input", "scenario.json", *options])
                prepare.assert_not_called()

    @unittest.skipUnless(hasattr(signal, "raise_signal"), "requires local SIGINT delivery")
    def test_sigint_during_authoritative_tick_finishes_tick_and_saves(self):
        original = ProductOperationService.preview_decision
        interrupted = []
        previous = signal.getsignal(signal.SIGINT)
        restored_handler = lambda _signum, _frame: None

        def interrupt_first(service, *args, **kwargs):
            if not interrupted:
                interrupted.append(True)
                signal.raise_signal(signal.SIGINT)
            return original(service, *args, **kwargs)

        signal.signal(signal.SIGINT, restored_handler)
        try:
            with tempfile.TemporaryDirectory() as directory, \
                    patch.object(ProductOperationService, "preview_decision", interrupt_first), \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                root = Path(directory)
                output = root / "output"
                code = launcher.main([
                    "--scenario-bundle", str(ROOT / "examples/source_movement_scenario.json"),
                    "--output-dir", str(output), "--run-namespace", "midtick-stop",
                    "--red-policy", "simple-capture", "--blue-policy", "simple-capture",
                    "--max-ticks", "2", "--fast",
                ])
                self.assertEqual(code, 0)
                self.assertTrue(interrupted)
                self.assertIs(signal.getsignal(signal.SIGINT), restored_handler)
                self.assertTrue((output / "replay.zip").is_file())
                self.assertFalse(list(root.glob(".output.incomplete-*")))
                report = json.loads((output / "policy-summary.json").read_text())
                self.assertEqual(report["tick_count"], 1)
                self.assertEqual(report["stop_reason"], "user_stop")
                self.assertEqual(report["command_status_counts"], {"APPLIED": 2})
        finally:
            signal.signal(signal.SIGINT, previous)

    def test_sigint_handler_restored_when_host_raises(self):
        session, bindings = self.make_session()
        previous = signal.getsignal(signal.SIGINT)

        def failure():
            raise RuntimeError("host-failed-for-test")

        with self.assertRaisesRegex(RuntimeError, "host-failed-for-test"):
            run_policy_loop(session, bindings, {}, max_ticks=1, paced=False, check_server=failure)
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    @unittest.skipUnless(os.name == "posix", "requires process SIGINT during a blocking wait")
    def test_paced_sigint_wakes_long_wait_without_starting_tick(self):
        session, bindings = self.make_session(tick_interval_ms=60_000)
        waiting = Event()

        class TrackedStopRequest(_StopRequest):
            def wait(self, timeout=None):
                waiting.set()
                return super().wait(timeout)

        def interrupt_wait():
            if waiting.wait(5):
                os.kill(os.getpid(), signal.SIGINT)

        thread = Thread(target=interrupt_wait)
        previous = signal.getsignal(signal.SIGINT)
        started = monotonic()
        thread.start()
        try:
            with patch("scripts.rule_policy_host._StopRequest", TrackedStopRequest):
                reason, _ = run_policy_loop(session, bindings, {}, max_ticks=1, paced=True)
        finally:
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(reason.value, "user_stop")
        self.assertEqual(session.tick_count, 0)
        self.assertLess(monotonic() - started, 5)
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    @unittest.skipUnless(hasattr(signal, "raise_signal"), "requires local SIGINT delivery")
    def test_signal_handler_is_lock_free_inside_locked_region(self):
        previous = signal.getsignal(signal.SIGINT)
        lock = Lock()
        with _deferred_sigint() as stop:
            # The handler must only assign its flag: no Event.set() (whose
            # internal Condition could be the interrupted locked region).
            with lock, patch.object(Event, "set", side_effect=AssertionError("signal handler must not acquire an event lock")):
                signal.raise_signal(signal.SIGINT)
                self.assertTrue(stop.is_set())
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_non_main_thread_does_not_install_signal_handler(self):
        session, bindings = self.make_session()
        results = []

        def worker():
            try:
                results.append(run_policy_loop(session, bindings, {}, max_ticks=1, paced=False))
            except BaseException as error:
                results.append(error)

        with patch("scripts.rule_policy_host.signal.signal", side_effect=AssertionError("no worker signal handler")):
            thread = Thread(target=worker)
            thread.start()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertNotIsInstance(results[0], BaseException)
        self.assertEqual(results[0][0].value, "runner_limit")

    def test_unavailable_signal_registration_keeps_embedded_run_usable(self):
        session, bindings = self.make_session()
        with patch("scripts.rule_policy_host.signal.signal", side_effect=ValueError("signals unavailable")):
            reason, _ = run_policy_loop(session, bindings, {}, max_ticks=1, paced=False)
        self.assertEqual(reason.value, "runner_limit")

    def test_policy_seat_http_is_read_only_but_human_seat_remains_enabled(self):
        session, bindings = self.make_session()
        server = object.__new__(RealtimeDualControlHTTPServer)
        server.realtime_session = session
        server.bindings = bindings
        server.tokens = {"red-token": "red", "blue-token": "blue"}
        server.seat_policies = {"red": "simple-capture", "blue": "human"}
        self.assertFalse(server.status("red", bindings["red"])["can_operate"])
        self.assertTrue(server.status("blue", bindings["blue"])["can_operate"])
        server.require_human_control("blue")
        for path, data in (
            ("preview", {"request_id": "preview", "observation_version": 0,
                         "actor_ids": [1], "operation_type": "WEAPON_LOCK"}),
            ("execute", {"request_id": "execute", "preview_token": "invented"}),
        ):
            body = json.dumps({"token": "red-token", **data}).encode()
            handler = object.__new__(RealtimeDualControlRequestHandler)
            handler.server = server
            handler.path = "/api/product/" + path
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile = BytesIO(body)
            responses = []
            handler._send_json = lambda status, record: responses.append((status, record))
            handler.do_POST()
            self.assertEqual(responses[0][0], HTTPStatus.FORBIDDEN)
            self.assertEqual(responses[0][1]["code"], "seat_policy_controlled")
        self.assertEqual(session.command_events, ())


if __name__ == "__main__":
    unittest.main()
