"""Optimized boundary copies retain deepcopy semantics and private ownership."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from wargame_native_engine.core.action import ActionType, EngineAction, ValidAction
from wargame_native_engine.gateway.decision_window import DecisionWindow
from wargame_native_engine.gateway.models import CommandResult, ControllerView
from wargame_native_engine.io.detached import detached_copy
from wargame_native_engine.operations import ScenarioMatchFactory


class CommandCopyContractTests(unittest.TestCase):
    def test_registered_actions_preserve_aliases_cycles_and_detachment(self):
        payload = {"items": []}
        action = EngineAction("red", 1, ActionType.WAIT, parameters=payload)
        valid = ValidAction(action, 99)
        payload["items"].extend([action, valid, payload])
        result = detached_copy((action, valid, payload))
        reference = deepcopy((action, valid, payload))
        for copied in (result, reference):
            self.assertIs(copied[1].action, copied[0])
            self.assertIs(copied[0].parameters, copied[2])
            self.assertIs(copied[2]["items"][0], copied[0])
            self.assertIs(copied[2]["items"][1], copied[1])
            self.assertIs(copied[2]["items"][2], copied[2])
            self.assertIsNot(copied[2], payload)

    def test_custom_action_and_command_subclasses_keep_copy_protocol(self):
        class CustomAction(EngineAction):
            def __deepcopy__(self, memo):
                return {"custom_action": True}

        class CustomResult(CommandResult):
            def __deepcopy__(self, memo):
                return {"custom_result": True}

        view = ControllerView("s", "red", 0, {}, {}, (), False, None, None)
        values = [CustomAction("red", 1, ActionType.WAIT),
                  CustomResult("r", True, False, (), view)]
        self.assertEqual(detached_copy(values), deepcopy(values))

    def test_frozen_action_export_does_not_alias_authorization(self):
        action = ValidAction(EngineAction(
            "red", 1, ActionType.WAIT, parameters={"nested": [1]}), 99)
        window = DecisionWindow.open(
            session_id="s", decision_window_index=0, opened_at_sim_time_ms=0,
            red_valid_actions=[action], blue_valid_actions=[],
        )
        first = window.frozen_valid_actions("red")
        self.assertEqual(first, deepcopy((action,)))
        first[0].action.parameters["nested"].append(2)
        action.action.parameters["nested"].append(3)
        self.assertEqual(
            window.frozen_valid_actions("red")[0].action.parameters,
            {"nested": [1]},
        )

    def test_command_return_retry_and_current_view_are_independent(self):
        with tempfile.TemporaryDirectory() as storage:
            match = ScenarioMatchFactory.from_path(
                "examples/public_river_valley_scenario.json",
                storage_root=Path(storage), red_controller_id="red",
                blue_controller_id="blue", decision_interval_ms=1000,
                frame_interval_ms=1000,
            )(42, 1)
            try:
                arguments = dict(request_id="copy-check", expected_window=0,
                                 ordered_action_ids=())
                first = match.gateway.submit_batch(match.session_id, "red", **arguments)
                expected = deepcopy(first.to_record())
                first.view.observation["caller_mutation"] = [1]
                first.view.source_frame["caller_mutation"] = [2]
                if first.decision_window.valid_actions:
                    first.decision_window.valid_actions[0]["caller_mutation"] = [3]
                retry = match.gateway.submit_batch(match.session_id, "red", **arguments)
                self.assertEqual(retry.to_record(), expected)
                _, current = match.gateway.controller_state(match.session_id, "red")
                self.assertNotIn("caller_mutation", current.observation)
                self.assertNotIn("caller_mutation", current.source_frame)
                retry.view.observation["retry_mutation"] = True
                self.assertNotIn("retry_mutation", match.gateway.submit_batch(
                    match.session_id, "red", **arguments).view.observation)
            finally:
                match.close()


if __name__ == "__main__":
    unittest.main()
