"""Additive fire intent fields survive the public operation boundary."""
import unittest

from wargame_native_engine.agents.seat import SeatOperation
from wargame_native_engine.agents.tool_calling import function_schemas
from wargame_native_engine.viewer.human_capabilities import _blocked_indirect_fire_options


class PositionFireContractTests(unittest.TestCase):
    def test_direct_position_serializes_without_target_identity(self):
        operation = SeatOperation(
            "SHOOT", actor_ids=(1,), target_hex=104,
            weapon_choices=((1, 36),),
        )
        payload = operation.preview_arguments()
        self.assertEqual(payload["target_hex"], 104)
        self.assertIsNone(payload["target_id"])
        with self.assertRaises(ValueError):
            SeatOperation("SHOOT", actor_ids=(1,), target_hex=104, target_id=2)

    def test_indirect_explicit_target_is_optional_and_preserved(self):
        for target_id in (None, 2):
            operation = SeatOperation(
                "INDIRECT_FIRE", actor_ids=(1,), target_hex=104,
                target_id=target_id, shooter_id=1, weapon_id=72,
                fire_mode="INDIRECT",
            )
            self.assertEqual(operation.preview_arguments()["target_id"], target_id)
            self.assertEqual(operation.preview_arguments()["target_hex"], 104)

    def test_blocked_ui_reasons_are_selected_own_platforms_only(self):
        summary = {"indirect_fire": {"capabilities": [
            {"shooter_id": 1, "weapon_id": 72, "status": "BLOCKED",
             "blockers": ["INDIRECT_FIRE_AMMUNITION_UNAVAILABLE"]},
            {"shooter_id": 2, "weapon_id": 72, "status": "BLOCKED",
             "blockers": ["PRIVATE_ENEMY_REASON"]},
            {"shooter_id": 3, "weapon_id": 72, "status": "AVAILABLE",
             "blockers": []},
        ]}}
        values = _blocked_indirect_fire_options(summary, {1, 3})
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["shooter_id"], 1)
        self.assertEqual(values[0]["blockers"], ["INDIRECT_FIRE_AMMUNITION_UNAVAILABLE"])
        self.assertEqual(_blocked_indirect_fire_options({}, {1}), [])

    def test_function_schema_explains_explicit_target_contract(self):
        schema = next(item for item in function_schemas()
                      if item["name"] == "preview_game_operation")
        props = schema["parameters"]["properties"]["operation"]["properties"]
        self.assertIn("never infers", props["target_hex"]["description"])
        self.assertIn("these two fire operations", props["target_id"]["description"])


if __name__ == "__main__":
    unittest.main()
