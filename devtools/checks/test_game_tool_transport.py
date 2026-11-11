"""JSON host boundary tests, without a provider SDK or external service."""

import json
import unittest
from unittest.mock import patch

import test_game_tools

from wargame_native_engine.agents.tool_calling import GameToolAdapter
from wargame_native_engine.agents.tool_transport import (
    TOOL_TRANSPORT_VERSION,
    GameToolTransport,
)
from wargame_native_engine.version import ADJUDICATION_SEMANTICS_VERSION


class GameToolTransportTests(unittest.TestCase):
    def setUp(self):
        # Reuse the tiny fixture without inheriting and rerunning its test suite.
        self.fixture = test_game_tools.GameToolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.transport = GameToolTransport(self.fixture.red)

    def test_describe_is_detached_versioned_and_schema_fingerprinted(self):
        metadata = self.transport.describe()
        self.assertEqual(metadata["transport_version"], TOOL_TRANSPORT_VERSION)
        self.assertEqual(
            metadata["adjudication_semantics_version"], ADJUDICATION_SEMANTICS_VERSION
        )
        self.assertEqual(metadata["freshness_policy"], "strict_current_observation")
        self.assertEqual(len(metadata["schema_sha256"]), 64)
        metadata["functions"][0]["name"] = "bad"
        self.assertEqual(
            self.transport.describe()["functions"][0]["name"], "game_observation"
        )
        self.assertEqual(
            metadata["schema_sha256"], self.transport.describe()["schema_sha256"]
        )

    def test_success_matches_original_dispatch(self):
        reply = self.transport.invoke("game_observation", "{}")
        self.assertTrue(reply["ok"])
        self.assertEqual(
            reply["result"], self.fixture.red.dispatch("game_observation", {})
        )
        json.dumps(reply, allow_nan=False)

    def test_bad_json_never_reaches_adapter(self):
        for source in (
            "{",
            "null",
            "[]",
            "true",
            "1",
            None,
            '{"actor_ids":[],"actor_ids":[1]}',
            '{"value": {"x": 1, "x": 2}}',
            '{"value":NaN}',
            '{"value":Infinity}',
            '{"value":1e400}',
            '{"value":"\\ud800"}',
            '{"\\ud800":0}',
            "{" + '"value":' + "[" * 2000 + "0" + "]" * 2000 + "}",
        ):
            with (
                self.subTest(source=str(source)[:80]),
                patch.object(self.fixture.red, "dispatch") as call,
            ):
                result = self.transport.invoke("game_observation", source)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "invalid_json")
                call.assert_not_called()

    def test_size_limit_is_utf8_bytes_and_checked_before_dispatch(self):
        transport = GameToolTransport(self.fixture.red, max_argument_bytes=12)
        for source in ('{"x":"中文"}', " " * 13):
            with (
                self.subTest(source=source),
                patch.object(self.fixture.red, "dispatch") as call,
            ):
                reply = transport.invoke("game_observation", source)
                self.assertEqual(reply["error"]["code"], "arguments_too_large")
                call.assert_not_called()

    def test_gateway_errors_are_structured_not_success(self):
        observation = self.fixture.red.dispatch("game_observation", {})
        reply = self.transport.invoke(
            "preview_game_operation",
            json.dumps(
                {
                    "observation_version": observation["observation_version"],
                    "request_id": "missing-destination",
                    "operation": {"operation_type": "MOVE", "actor_ids": [1]},
                }
            ),
        )
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "destination_required")
        self.assertEqual(reply["error"]["recovery"], "review_request")

    def test_racing_tick_returns_refresh_guidance(self):
        token = self.fixture.preview()
        submit = self.fixture.session.submit_operation_if_current

        def advance(*args, **kwargs):
            self.fixture.session.advance_tick()
            return submit(*args, **kwargs)

        with patch.object(self.fixture.session, "submit_operation_if_current", advance):
            reply = self.transport.invoke(
                "submit_game_operation",
                json.dumps(
                    {
                        "preview_token": token,
                        "request_id": "race",
                        "confirm_partial": False,
                    }
                ),
            )
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "observation_version_stale")
        self.assertEqual(reply["error"]["recovery"], "refresh_observation")
        self.assertFalse(self.fixture.session.command_events)

    def test_unknown_exceptions_are_not_disguised_as_domain_errors(self):
        with (
            patch.object(
                self.fixture.red,
                "dispatch",
                side_effect=RuntimeError("programmer error"),
            ),
            self.assertRaisesRegex(RuntimeError, "programmer error"),
        ):
            self.transport.invoke("game_observation", "{}")

    def test_response_loss_does_not_duplicate_submission(self):
        adapter = GameToolAdapter(self.fixture.session, self.fixture.reset.red_binding)
        transport = GameToolTransport(adapter)
        token = self.fixture.preview(adapter)
        # Simulate a host limit changing after preview, before returning receipt.
        adapter._max_response_bytes = 10
        args = json.dumps(
            {"preview_token": token, "request_id": "same", "confirm_partial": False}
        )
        first = transport.invoke("submit_game_operation", args)
        second = transport.invoke("submit_game_operation", args)
        self.assertEqual(first["error"]["code"], "response_too_large")
        self.assertEqual(first, second)
        self.assertEqual(len(self.fixture.session.command_events), 1)
        adapter._max_response_bytes = 512 * 1024
        self.assertTrue(transport.invoke("submit_game_operation", args)["ok"])
        self.assertEqual(len(self.fixture.session.command_events), 1)


if __name__ == "__main__":
    unittest.main()
