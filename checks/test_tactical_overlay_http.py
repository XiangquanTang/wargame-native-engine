"""Real HTTP route logic keeps overlays paired with their frozen seat view.

No listener is needed: the request body parser and POST dispatcher run against
a real realtime session, and only the final HTTP write is captured.
"""

from dataclasses import replace
from http import HTTPStatus
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wargame_native_engine.agents.realtime import (
    ManualRealtimeClock,
    RealtimeAgentSession,
    RealtimeAgentSessionError,
)
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.operations import ScenarioMatchFactory
from wargame_native_engine.viewer.human_capabilities import source_weapon_profiles
from wargame_native_engine.viewer.realtime_control_server import (
    RealtimeDualControlHTTPServer,
    RealtimeDualControlRequestHandler,
    _confirmed_contacts,
)


ROOT = Path(__file__).resolve().parents[1]


class TacticalOverlayHTTPTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        bundle = ScenarioBundle.from_path(
            ROOT / "examples/source_movement_scenario.json"
        )
        self.session = RealtimeAgentSession(
            ScenarioMatchFactory(
                bundle=bundle,
                storage_root=Path(storage.name),
                red_controller_id="red",
                blue_controller_id="blue",
                decision_interval_ms=1000,
                frame_interval_ms=1000,
            ),
            run_namespace="overlay-http-tests",
            tick_interval_ms=1000,
            clock=ManualRealtimeClock(),
        )
        self.addCleanup(self.session.close)
        reset = self.session.reset(seed=42)
        self.red = reset.red_binding
        self.blue = reset.blue_binding
        self.version = self.session.latest_view(self.red).observation_version
        self.server = object.__new__(RealtimeDualControlHTTPServer)
        self.server.realtime_session = self.session
        self.server.bindings = {"red": self.red, "blue": self.blue}
        self.server.tokens = {"red-token": "red", "blue-token": "blue"}
        self.server.map_data = bundle.render_map_record()
        self.server.weapon_profiles = source_weapon_profiles(bundle.scenario.metadata)

    def get_bootstrap(self, query=""):
        handler = object.__new__(RealtimeDualControlRequestHandler)
        handler.server = self.server
        handler.path = "/api/product/bootstrap?token=red-token" + query
        responses = []
        handler._send_json = lambda status, record: responses.append((status, record))
        handler.do_GET()
        self.assertEqual(len(responses), 1)
        return responses[0]

    def post(self, **changes):
        body = {
            "token": "red-token",
            "observation_version": self.version,
            "unit_id": 1,
            **changes,
        }
        payload = json.dumps(body).encode()
        handler = object.__new__(RealtimeDualControlRequestHandler)
        handler.server = self.server
        handler.path = "/api/product/tactical-overlay"
        handler.headers = {"Content-Length": str(len(payload))}
        handler.rfile = BytesIO(payload)
        responses = []
        handler._send_json = lambda status, record: responses.append((status, record))
        handler.do_POST()
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_successful_read_has_cells_and_does_not_advance_or_submit(self):
        before = self.session.status_snapshot(self.red)
        status, record = self.post()
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(record["visibility_field"]["hex_ids"])
        self.assertTrue(record["weapon_envelope"]["weapons"])
        self.assertEqual(record["observation_version"], self.version)
        self.assertEqual(record["frame_index"], before.frame_index)
        self.assertEqual(self.session.status_snapshot(self.red), before)
        self.assertEqual(self.session.command_events, ())

    def test_tick_after_query_cannot_discard_cells_or_mix_contact_metadata(self):
        original = self.session.tactical_overlay_snapshot
        expected_view, expected_controller, expected_overlay = original(
            self.red, observation_version=self.version, unit_id=1,
        )

        def publish_after_snapshot(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            self.session.advance_tick()
            return snapshot

        with patch.object(
            self.session, "tactical_overlay_snapshot", publish_after_snapshot,
        ), patch.object(
            self.session, "controller_snapshot",
            side_effect=AssertionError("must not combine another live snapshot"),
        ):
            status, record = self.post()
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(record["observation_version"], expected_view.observation_version)
        self.assertEqual(record["frame_index"], expected_view.frame_index)
        self.assertEqual(record["visibility_field"], expected_overlay.to_record()["visibility_field"])
        self.assertEqual(record["confirmed_contacts"], _confirmed_contacts(
            "red", 1, expected_controller,
        ))
        self.assertGreater(self.session.latest_view(self.red).observation_version, self.version)

    def test_stale_or_other_seat_versions_are_rejected_before_query(self):
        blue_version = self.session.latest_view(self.blue).observation_version
        status, record = self.post(observation_version=blue_version)
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(record["code"], "observation_version_stale")
        self.session.advance_tick()
        status, record = self.post()
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(record["code"], "observation_version_stale")

    def test_other_seat_actor_or_invalid_token_cannot_read_an_overlay(self):
        status, record = self.post(unit_id=2)
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(record["code"], "actor_not_owned")
        status, record = self.post(token="invalid")
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(record["code"], "invalid_token")

    def test_forged_binding_is_rejected(self):
        with self.assertRaises(RealtimeAgentSessionError):
            self.session.tactical_overlay_snapshot(
                replace(self.red, seat="blue"),
                observation_version=self.version,
                unit_id=1,
            )

    def test_snapshot_detaches_projection_and_reuses_existing_overlay_cache(self):
        manager = self.session._match.gateway.manager
        with patch.object(
            manager, "query_unit_tactical_overlay_from_projection",
            wraps=manager.query_unit_tactical_overlay_from_projection,
        ) as query:
            seat_view, controller_view, overlay = self.session.tactical_overlay_snapshot(
                self.red, observation_version=self.version, unit_id=1,
            )
            seat_view.observation["friendly_operators"].clear()
            controller_view.observation["friendly_operators"].clear()
            controller_view.source_frame["operators"].clear()
            fresh_seat, fresh_controller, fresh_overlay = self.session.tactical_overlay_snapshot(
                self.red, observation_version=self.version, unit_id=1,
            )
            legacy_overlay = self.session.tactical_overlay(
                self.red, observation_version=self.version, unit_id=1,
            )
        self.assertTrue(fresh_seat.observation["friendly_operators"])
        self.assertTrue(fresh_controller.observation["friendly_operators"])
        self.assertTrue(fresh_controller.source_frame["operators"])
        self.assertEqual(query.call_count, 1)
        self.assertEqual(overlay, fresh_overlay)
        self.assertEqual(overlay, legacy_overlay)

    def test_bootstrap_without_overlay_retains_the_existing_shape(self):
        status, record = self.get_bootstrap("&include_map=0")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertNotIn("tactical_overlay", record)
        self.assertNotIn("capabilities", record)
        self.assertIsNone(record["scene"]["map"])

    def test_combined_bootstrap_is_coherent_across_tick_during_response(self):
        before = self.session.status_snapshot(self.red)
        original = self.session.current_tactical_overlay_snapshot

        def publish_after_snapshot(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            self.session.advance_tick()
            return snapshot

        with patch.object(
            self.session, "current_tactical_overlay_snapshot", publish_after_snapshot,
        ), patch.object(
            self.session, "controller_snapshot",
            side_effect=AssertionError("combined read cannot take a later scene"),
        ), patch.object(
            self.session, "operation_capabilities_snapshot",
            side_effect=AssertionError("combined read cannot take later capabilities"),
        ):
            status, record = self.get_bootstrap("&overlay_unit_id=1&include_map=0")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(record["observation_version"], before.observation_version)
        self.assertEqual(record["scene"]["frame"]["frame_index"], before.frame_index)
        self.assertIsNone(record["scene"]["map"])
        for field in ("tactical_overlay", "capabilities", "product_status"):
            self.assertEqual(record[field]["observation_version"], before.observation_version)
            self.assertEqual(record[field]["frame_index"], before.frame_index)
        self.assertTrue(record["tactical_overlay"]["visibility_field"]["hex_ids"])
        self.assertTrue(record["capabilities"]["direct_fire"]["units"])
        self.assertGreater(self.session.latest_view(self.red).observation_version, before.observation_version)

    def test_combined_bootstrap_recovers_after_strict_query_version_expires(self):
        self.session.advance_tick()
        status, _ = self.post()
        self.assertEqual(status, HTTPStatus.CONFLICT)
        before = self.session.status_snapshot(self.red)
        status, record = self.get_bootstrap("&overlay_unit_id=1&include_map=1")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(record["observation_version"], before.observation_version)
        self.assertEqual(record["tactical_overlay"]["observation_version"], before.observation_version)
        self.assertIsNotNone(record["scene"]["map"])
        self.assertEqual(self.session.status_snapshot(self.red), before)
        self.assertEqual(self.session.command_events, ())

    def test_combined_bootstrap_validates_actor_and_exact_query_parameter(self):
        for query in ("", "-1", "1.0", "true", "+1", "1&overlay_unit_id=1"):
            with self.subTest(query=query):
                status, _ = self.get_bootstrap("&overlay_unit_id=" + query)
                self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        for actor in (2, 999999):
            with self.subTest(actor=actor):
                status, record = self.get_bootstrap(f"&overlay_unit_id={actor}")
                self.assertEqual(status, HTTPStatus.CONFLICT)
                self.assertEqual(record["code"], "actor_not_owned")


if __name__ == "__main__":
    unittest.main()
