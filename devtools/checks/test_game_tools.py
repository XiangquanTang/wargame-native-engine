"""Integration tests use the public six-cell movement fixture; no model needed."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from wargame_native_engine.agents.realtime import (
    ManualRealtimeClock,
    RealtimeAgentSession,
    RealtimeAgentSessionError,
    RealtimeSeatBinding,
)
from wargame_native_engine.agents.tool_calling import (
    GameToolAdapter,
    GameToolError,
    function_schemas,
)
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.operations import ScenarioMatchFactory

ROOT = Path(__file__).resolve().parents[2]


class GameToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = ManualRealtimeClock()
        self.session = RealtimeAgentSession(
            ScenarioMatchFactory(
                bundle=ScenarioBundle.from_path(
                    ROOT / "examples/source_movement_scenario.json"
                ),
                storage_root=Path(self.tmp.name),
                red_controller_id="tools-red",
                blue_controller_id="tools-blue",
                decision_interval_ms=1000,
                frame_interval_ms=1000,
            ),
            run_namespace="game-tools-test",
            tick_interval_ms=1000,
            clock=self.clock,
        )
        self.reset = self.session.reset(seed=42)
        self.addCleanup(self.session.close)
        self.red = GameToolAdapter(self.session, self.reset.red_binding)
        self.blue = GameToolAdapter(self.session, self.reset.blue_binding)

    def preview(self, adapter=None, operation=None, request="preview-1"):
        adapter = adapter or self.red
        obs = adapter.dispatch("game_observation", {})
        return adapter.dispatch(
            "preview_game_operation",
            {
                "observation_version": obs["observation_version"],
                "request_id": request,
                "operation": operation
                or {"operation_type": "WAIT", "actor_ids": [], "whole_side_wait": True},
            },
        )["preview"]["preview_token"]

    def submit(self, token, adapter=None, request="submit-1"):
        return (adapter or self.red).dispatch(
            "submit_game_operation",
            {
                "preview_token": token,
                "request_id": request,
                "confirm_partial": False,
            },
        )

    def assert_code(self, code, func, *args, **kwargs):
        with self.assertRaises(GameToolError) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)

    def test_observation_detached_and_side_bound(self):
        expected = self.session.latest_view(self.reset.red_binding)
        actual = self.red.dispatch("game_observation", {})
        self.assertEqual(actual["observation"], expected.to_record()["observation"])
        self.assertEqual(actual["side"], "red")
        self.assertNotEqual(
            actual["observation_version"],
            self.blue.dispatch("game_observation", {})["observation_version"],
        )
        actual["observation"].clear()
        self.assertTrue(self.red.dispatch("game_observation", {})["observation"])
        self.assert_code(
            "invalid_arguments", self.red.dispatch, "game_observation", {"side": "blue"}
        )

    def test_invalid_binding_rejected(self):
        with self.assertRaises(RealtimeAgentSessionError):
            GameToolAdapter(
                self.session,
                RealtimeSeatBinding(self.reset.red_binding.binding_id, "blue"),
            )

    def test_capabilities_owned_and_versioned(self):
        result = self.red.dispatch("game_capabilities", {"actor_ids": [1]})
        self.assertEqual(result["controllable_actor_ids"], [1])
        self.assertIn("observation_version", result)
        self.assert_code(
            "actor_not_owned",
            self.red.dispatch,
            "game_capabilities",
            {"actor_ids": [2]},
        )

    def test_wait_receipt_then_application_preserves_logs(self):
        token = self.preview()
        receipt = self.submit(token)
        self.assertTrue(receipt["accepted"])
        self.assertEqual(receipt["status"], "PENDING")
        result = self.red.dispatch(
            "game_command_result", {"command_sequence": receipt["command_sequence"]}
        )
        self.assertEqual(result["command"]["status"], "PENDING")
        self.session.advance_tick()
        result = self.red.dispatch(
            "game_command_result", {"command_sequence": receipt["command_sequence"]}
        )
        self.assertEqual(result["command"]["status"], "APPLIED")
        self.assertEqual(
            result["command"]["observation_version"], receipt["observation_version"]
        )
        self.assertEqual(result["command"]["applied_frame_index"], 1)
        self.assertTrue(result["executions"])
        self.assertEqual(len(self.session.command_events), 1)

    def test_request_retries_never_resubmit_even_after_tick(self):
        token = self.preview()
        first = self.submit(token)
        self.session.advance_tick()
        self.assertEqual(first, self.submit(token))
        self.assertEqual(len(self.session.command_events), 1)
        self.assert_code(
            "preview_already_submitted", self.submit, token, request="submit-2"
        )
        self.assert_code("request_id_conflict", self.submit, "other-token")

    def test_other_seat_cannot_submit_preview_or_read_receipt(self):
        token = self.preview()
        self.assert_code("preview_not_owned", self.submit, token, adapter=self.blue)
        receipt = self.submit(token)
        self.assert_code(
            "command_not_owned",
            self.blue.dispatch,
            "game_command_result",
            {"command_sequence": receipt["command_sequence"]},
        )

    def test_stale_preview_and_submit_rejected(self):
        version = self.red.dispatch("game_observation", {})["observation_version"]
        token = self.preview()
        self.session.advance_tick()
        self.assert_code("stale_observation", self.submit, token)
        self.assert_code(
            "stale_observation",
            self.red.dispatch,
            "preview_game_operation",
            {
                "observation_version": version,
                "request_id": "stale",
                "operation": {
                    "operation_type": "WAIT",
                    "actor_ids": [],
                    "whole_side_wait": True,
                },
            },
        )
        self.assertEqual(len(self.session.command_events), 0)

    def test_tick_between_precheck_and_submission_rejected_atomically(self):
        token = self.preview()
        submit = self.session.submit_operation_if_current

        def advance_before_admission(*args, **kwargs):
            self.session.advance_tick()
            return submit(*args, **kwargs)

        with (
            patch.object(
                self.session, "submit_operation_if_current", advance_before_admission
            ),
            self.assertRaises(RealtimeAgentSessionError) as ctx,
        ):
            self.submit(token)
        self.assertEqual(ctx.exception.code, "observation_version_stale")
        self.assertEqual(len(self.session.command_events), 0)

    def test_reset_between_precheck_and_submission_cannot_target_new_episode(self):
        token = self.preview()
        old_episode = self.session.episode_id
        submit = self.session.submit_operation_if_current

        def reset_before_admission(*args, **kwargs):
            self.session.reset(seed=43)
            return submit(*args, **kwargs)

        with (
            patch.object(
                self.session, "submit_operation_if_current", reset_before_admission
            ),
            self.assertRaises(RealtimeAgentSessionError) as ctx,
        ):
            self.submit(token)
        self.assertNotEqual(self.session.episode_id, old_episode)
        self.assertEqual(ctx.exception.code, "realtime_binding_invalid")
        self.assertEqual(len(self.session.command_events), 0)

    def test_tick_during_preview_rejected_without_issuing_token(self):
        preview = self.session.preview_operation

        def advance_after_preview(*args, **kwargs):
            result = preview(*args, **kwargs)
            self.session.advance_tick()
            return result

        with patch.object(self.session, "preview_operation", advance_after_preview):
            self.assert_code("stale_observation", self.preview)
        self.assertEqual(len(self.session.command_events), 0)

    def test_changed_episode_requires_new_adapter(self):
        self.session.reset(seed=43)
        self.assert_code("episode_changed", self.red.dispatch, "game_observation", {})

    def test_strict_arguments_and_unknown_tools(self):
        self.assert_code("unknown_tool", self.red.dispatch, "eval", {})
        self.assert_code(
            "invalid_arguments",
            self.red.dispatch,
            "game_capabilities",
            {"actor_ids": [True]},
        )
        self.assert_code(
            "invalid_arguments",
            self.red.dispatch,
            "game_capabilities",
            {"actor_ids": [1, 1]},
        )
        self.assert_code(
            "invalid_arguments",
            self.red.dispatch,
            "game_command_result",
            {"command_sequence": True},
        )
        self.assert_code(
            "actor_not_owned",
            self.preview,
            operation={"operation_type": "WAIT", "actor_ids": [2]},
        )

    def test_capacity_fail_closed_and_retry_preserved(self):
        adapter = GameToolAdapter(self.session, self.reset.red_binding, max_requests=1)
        token = self.preview(adapter)
        receipt = self.submit(token, adapter)
        self.assertEqual(receipt, self.submit(token, adapter))
        self.assert_code("request_capacity", self.preview, adapter, request="second")

    def test_identical_preview_retry_at_capacity_is_idempotent(self):
        adapter = GameToolAdapter(self.session, self.reset.red_binding, max_requests=1)
        first = self.preview(adapter)
        self.assertEqual(first, self.preview(adapter))
        self.assert_code(
            "request_id_conflict", self.preview, adapter,
            operation={"operation_type": "MOVE", "actor_ids": [1], "destination_hex": 102},
        )
        self.session.advance_tick()
        self.assert_code("request_id_conflict", self.preview, adapter)

    def test_failed_preview_delivery_reserves_budget_and_allows_identical_retry(self):
        adapter = GameToolAdapter(
            self.session, self.reset.red_binding, max_requests=1, max_response_bytes=10,
        )
        version = self.session.latest_view(self.reset.red_binding).observation_version
        arguments = {
            "observation_version": version,
            "request_id": "bounded-preview",
            "operation": {"operation_type": "WAIT", "actor_ids": [], "whole_side_wait": True},
        }
        for _ in range(2):
            self.assert_code(
                "response_too_large", adapter.dispatch, "preview_game_operation", arguments,
            )
        self.assert_code(
            "request_capacity", adapter.dispatch, "preview_game_operation",
            {**arguments, "request_id": "another-preview"},
        )
        self.assert_code(
            "request_id_conflict", adapter.dispatch, "preview_game_operation",
            {**arguments, "operation": {"operation_type": "MOVE", "actor_ids": [1], "destination_hex": 102}},
        )
        service = self.session._advisory_services["red"]
        self.assertEqual(len(service._preview_requests), 1)
        self.assertFalse(adapter._previews)
        adapter._max_response_bytes = 512 * 1024
        recovered = adapter.dispatch("preview_game_operation", arguments)
        self.assertTrue(recovered["preview"]["preview_token"])
        self.assertEqual(len(service._preview_requests), 1)
        self.assertEqual(len(adapter._previews), 1)

    def test_preview_race_still_reserves_capacity(self):
        adapter = GameToolAdapter(self.session, self.reset.red_binding, max_requests=1)
        preview = self.session.preview_operation

        def advance_after_preview(*args, **kwargs):
            result = preview(*args, **kwargs)
            self.session.advance_tick()
            return result

        with patch.object(self.session, "preview_operation", advance_after_preview):
            self.assert_code("stale_observation", self.preview, adapter)
        self.assert_code("request_capacity", self.preview, adapter, request="new-version")
        self.assertFalse(adapter._previews)
        self.assertEqual(len(adapter._preview_requests), 1)

    def test_domain_argument_errors_have_stable_adapter_code(self):
        for operation in (
            {"operation_type": "BOARD", "actor_ids": [1]},
            {"operation_type": "WAIT", "actor_ids": [], "target_hex": 102},
            {"operation_type": "WAIT", "actor_ids": [1], "weapon_choices": [[1, 0]]},
            {"operation_type": "SHOOT", "actor_ids": [1], "weapon_choices": [[2, 0]]},
            {"operation_type": "GUIDED_FIRE", "actor_ids": [1]},
        ):
            with self.subTest(operation=operation):
                self.assert_code("invalid_arguments", self.preview, operation=operation)
        self.assertEqual(len(self.session.command_events), 0)

    def test_oversized_response_explicit_not_truncated(self):
        adapter = GameToolAdapter(
            self.session, self.reset.red_binding, max_response_bytes=10
        )
        self.assert_code("response_too_large", adapter.dispatch, "game_observation", {})

    def test_schemas_independent_json_values(self):
        schemas = function_schemas()
        self.assertEqual(len(schemas), 10)
        json.dumps(schemas, allow_nan=False)
        schemas[0]["name"] = "corrupt"
        self.assertEqual(function_schemas()[0]["name"], "game_observation")

    def test_actual_move_submission_uses_existing_path(self):
        token = self.preview(
            operation={
                "operation_type": "MOVE",
                "actor_ids": [1],
                "destination_hex": 102,
            }
        )
        receipt = self.submit(token)
        self.session.advance_tick()
        result = self.red.dispatch(
            "game_command_result", {"command_sequence": receipt["command_sequence"]}
        )
        self.assertEqual(result["command"]["status"], "APPLIED")
        self.assertEqual(
            result["command"]["decision"]["operations"][0]["destination_hex"], 102
        )
        self.assertEqual(result["executions"][0]["operation_type"], "MOVE")


if __name__ == "__main__":
    unittest.main()
