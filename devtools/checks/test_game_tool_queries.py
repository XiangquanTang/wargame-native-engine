"""Read-only game queries share authentic snapshots and the existing operation path."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_game_tools

from wargame_native_engine.agents.realtime import (
    RealtimeAgentSessionError, RealtimeSeatBinding,
)
from wargame_native_engine.agents.tool_calling import _operation, function_schemas
from wargame_native_engine.agents.tool_transport import GameToolTransport
from wargame_native_engine.operations.product import PRODUCT_OPERATION_TYPES


class GameToolQueryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_game_tools.GameToolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.session = self.fixture.session
        self.adapter = self.fixture.red
        self.transport = GameToolTransport(self.adapter)
        self.version = self.session.latest_view(
            self.fixture.reset.red_binding,
        ).observation_version
        self.region = {
            "observation_version": self.version,
            "actor_id": 1,
            "region": {"x_min": 1, "y_min": 1, "width": 6, "height": 1},
            "target_profile": {
                "target_class": "PERSONNEL", "concealed": False, "height_m": 0,
            },
        }
        self.effect = {
            "observation_version": self.version,
            "actor_id": 1,
            "weapon_id": 36,
            "target_hex": 102,
            "target_model": "fixture-personnel",
            "target_profile": {
                "domain": "PERSONNEL", "blood": 3, "armor_class": "NONE",
                "concealed": False, "suppressed": False, "moving": False,
                "marching": False, "stacked": False, "close_combat": False,
            },
        }

    def error(self, name, args):
        response = self.transport.invoke(name, json.dumps(args))
        self.assertFalse(response["ok"], response)
        return response["error"]

    def test_region_is_versioned_compact_and_detached(self):
        first = self.adapter.dispatch("game_region_visibility", self.region)
        self.assertEqual(first["observation_version"], self.version)
        self.assertEqual(first["side"], "red")
        result = first["visibility"]
        self.assertEqual(result["semantics"], "hypothetical_target_visibility")
        self.assertEqual(result["cell_count"], 6)
        self.assertEqual([row[0] for row in result["cells"]], list(range(101, 107)))
        self.assertEqual(result["cell_fields"][:3], ["hex_id", "observable", "line_of_sight"])
        self.assertEqual(result["visible_count"], sum(row[1] for row in result["cells"]))
        baseline = deepcopy(first)
        first["visibility"]["cells"].clear()
        first["visibility"]["actor"].clear()
        self.assertEqual(self.adapter.dispatch("game_region_visibility", self.region), baseline)

    def test_region_owned_actor_assertions_and_map_bounds(self):
        cases = [
            ({"actor_id": 2}, "actor_not_owned"),
            ({"actor_assertion": {"current_hex": 102}}, "actor_assertion_mismatch"),
            ({"actor_assertion": {"subtype": "wrong-model"}}, "actor_assertion_mismatch"),
            (
                {"region": {"x_min": 0, "y_min": 0, "width": 1, "height": 1}},
                "region_out_of_map",
            ),
        ]
        for override, expected in cases:
            with self.subTest(expected=expected):
                error = self.error("game_region_visibility", {**self.region, **override})
                self.assertEqual(error["code"], expected)
        before = self.adapter.dispatch("game_observation", {})
        self.adapter.dispatch("game_region_visibility", {
            **self.region,
            "actor_assertion": {"current_hex": 101, "subtype": "fixture-vehicle"},
        })
        self.assertEqual(self.adapter.dispatch("game_observation", {}), before)

    def test_region_input_shape_and_size_fail_closed(self):
        overrides = [
            {"side": "blue"},
            {"observation_version": True},
            {"actor_id": True},
            {"region": {"x_min": 0, "y_min": 0, "width": 100, "height": 100}},
            {"target_profile": {"target_class": "PERSONNEL", "concealed": 1, "height_m": 0}},
            {"target_profile": {"target_class": "DRONE", "concealed": False, "height_m": 0}},
            {"actor_assertion": {"sensor_range": 999}},
        ]
        invalid = [None, [], {}, *[{**self.region, **value} for value in overrides]]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                error = self.error("game_region_visibility", arguments)
                self.assertIn(error["code"], {"invalid_arguments", "invalid_json"})

    def test_catalog_is_public_detached_metadata_not_occupant_inventory(self):
        first = self.adapter.dispatch("game_weapon_catalog", {})
        catalog = first["catalog"]
        self.assertIn(36, [weapon["weapon_id"] for weapon in catalog["weapons"]])
        self.assertEqual(len(catalog["catalog_hash"]), 64)
        baseline = deepcopy(catalog)
        catalog["weapons"].clear()
        self.assertEqual(self.adapter.dispatch("game_weapon_catalog", {})["catalog"], baseline)
        self.assertEqual(self.fixture.blue.dispatch("game_weapon_catalog", {})["catalog"], baseline)
        error = self.error("game_weapon_catalog", {"side": "blue"})
        self.assertEqual(error["code"], "invalid_arguments")

    def test_effect_declares_assumptions_and_uses_no_extra_action_path(self):
        result = self.adapter.dispatch("game_fire_effect", self.effect)
        self.assertEqual(result["observation_version"], self.version)
        effect = result["effect"]
        self.assertIsNone(effect["hit_probability"])
        self.assertEqual(effect["status"], "RESOLVED")
        self.assertGreaterEqual(effect["damage_probability"], 0)
        self.assertLessEqual(effect["damage_probability"], 1)
        self.assertGreaterEqual(effect["expected_damage"], 0)
        original = deepcopy(result)
        effect.clear()
        self.assertEqual(self.adapter.dispatch("game_fire_effect", self.effect), original)
        args = {name: value for name, value in self.effect.items() if name != "target_profile"}
        unresolved = self.adapter.dispatch("game_fire_effect", args)["effect"]
        self.assertEqual(unresolved["status"], "UNRESOLVED")
        self.assertIsNone(unresolved["damage_probability"])

    def test_extended_modes_reach_the_same_snapshot_boundary(self):
        view = self.session.latest_view(self.fixture.reset.red_binding)
        guided = {
            name: value for name, value in self.effect.items()
            if name not in {"target_hex", "target_model"}
        }
        modes = [
            {**guided, "target_id": 2, "fire_mode": "GUIDED", "guider_id": 3},
            {
                **self.effect, "fire_mode": "INDIRECT",
                "indirect_profile": {
                    "observation_policy": "GRID_CORRECTED", "planned_hex": 103,
                },
            },
        ]

        def snapshot(binding, *, observation_version, query):
            self.assertEqual(binding, self.fixture.reset.red_binding)
            self.assertEqual(observation_version, self.version)
            return view, {"forwarded": query.to_dict()}

        with patch.object(self.session, "fire_effect_snapshot", snapshot):
            for arguments in modes:
                with self.subTest(mode=arguments["fire_mode"]):
                    result = self.adapter.dispatch("game_fire_effect", arguments)
                    forwarded = result["effect"]["forwarded"]
                    for name, value in arguments.items():
                        if name != "observation_version":
                            self.assertEqual(forwarded[name], value)
        self.assertFalse(self.session.command_events)

    def test_effect_malformed_fields_remain_documented_domain_errors(self):
        invalid = [
            {**self.effect, "actor_id": True},
            {**self.effect, "weapon_id": -1},
            {**self.effect, "target_hex": 10 ** 400},
            {**self.effect, "target_profile": {**self.effect["target_profile"], "blood": 10 ** 400}},
            {**self.effect, "fire_mode": "INDIRECT", "indirect_profile": {}},
            {**self.effect, "guider_id": 1},
            {**self.effect, "target_id": 2},
            {**self.effect, "side": "blue"},
        ]
        for bad_value in ([], {}, None, True, "unknown"):
            invalid.append({**self.effect, "fire_mode": bad_value})
            for field in ("domain", "armor_class"):
                invalid.append({
                    **self.effect,
                    "target_profile": {**self.effect["target_profile"], field: bad_value},
                })
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                error = self.error("game_fire_effect", arguments)
                self.assertEqual(error["recovery"], "review_request")
                self.assertTrue(error["code"])

    def test_new_query_response_limits_do_not_truncate_or_queue(self):
        self.adapter._max_response_bytes = 32
        for name, arguments in (
            ("game_region_visibility", self.region),
            ("game_weapon_catalog", {}),
            ("game_fire_effect", self.effect),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.error(name, arguments)["code"], "response_too_large")
        self.assertFalse(self.session.command_events)
        self.adapter._max_response_bytes = 512 * 1024
        recovered = self.adapter.dispatch("game_region_visibility", self.region)
        self.assertEqual(recovered["visibility"]["cell_count"], 6)

    def test_new_query_programming_errors_are_not_hidden_as_invalid_requests(self):
        with (
            patch.object(
                self.session, "fire_effect_snapshot", side_effect=RuntimeError("query bug"),
            ),
            self.assertRaisesRegex(RuntimeError, "query bug"),
        ):
            self.transport.invoke("game_fire_effect", json.dumps(self.effect))

    def test_effect_hidden_target_does_not_become_an_identity_oracle(self):
        # Neither a real hidden enemy nor a nonexistent ID can be looked up.
        errors = []
        for target_id in (2, 999):
            arguments = {name: value for name, value in self.effect.items()
                         if name not in {"target_hex", "target_model"}}
            errors.append(self.error("game_fire_effect", {**arguments, "target_id": target_id}))
        self.assertEqual(errors[0], errors[1])
        self.assertIn("visible", errors[0]["message"].lower())

    def test_queries_reject_old_or_foreign_versions_under_session_lock(self):
        foreign = self.session.latest_view(self.fixture.reset.blue_binding).observation_version
        for name, arguments in (
            ("game_region_visibility", self.region), ("game_fire_effect", self.effect),
        ):
            with self.subTest(name=name):
                error = self.error(name, {**arguments, "observation_version": foreign})
                self.assertEqual(error["code"], "observation_version_stale")
                self.assertEqual(error["recovery"], "refresh_observation")
        self.session.advance_tick()
        for name, arguments in (
            ("game_region_visibility", self.region), ("game_fire_effect", self.effect),
        ):
            self.assertEqual(self.error(name, arguments)["code"], "observation_version_stale")

    def test_tick_or_reset_before_query_snapshot_never_relabels_old_evidence(self):
        original = self.session.region_visibility_snapshot

        def advance_then_query(*args, **kwargs):
            self.session.advance_tick()
            return original(*args, **kwargs)

        with patch.object(self.session, "region_visibility_snapshot", advance_then_query):
            error = self.error("game_region_visibility", self.region)
            self.assertEqual(error["code"], "observation_version_stale")
        current = self.session.latest_view(self.fixture.reset.red_binding)
        self.region["observation_version"] = current.observation_version

        def reset_then_query(*args, **kwargs):
            self.session.reset(seed=44)
            return original(*args, **kwargs)

        with patch.object(self.session, "region_visibility_snapshot", reset_then_query):
            error = self.error("game_region_visibility", self.region)
            self.assertEqual(error["code"], "realtime_binding_invalid")
            self.assertEqual(error["recovery"], "create_adapter_for_current_episode")

    def test_read_only_queries_preserve_tick_queue_and_retry_budget(self):
        before = self.adapter.dispatch("game_observation", {})
        for _ in range(2):
            self.adapter.dispatch("game_region_visibility", self.region)
            self.adapter.dispatch("game_weapon_catalog", {})
            self.adapter.dispatch("game_fire_effect", self.effect)
        self.assertEqual(self.adapter.dispatch("game_observation", {}), before)
        self.assertFalse(self.session.command_events)
        self.assertFalse(self.adapter._preview_requests)
        self.assertFalse(self.adapter._requests)
        token = self.fixture.preview()
        receipt = self.fixture.submit(token)
        self.session.advance_tick()
        self.assertEqual(self.fixture.submit(token), receipt)
        self.assertEqual(len(self.session.command_events), 1)

    def test_forged_binding_cannot_read_static_catalog(self):
        forged = RealtimeSeatBinding(self.fixture.reset.red_binding.binding_id, "blue")
        with self.assertRaisesRegex(RealtimeAgentSessionError, "realtime_binding_invalid"):
            self.session.weapon_catalog_snapshot(forged)

    def test_every_operation_uses_the_existing_seat_contract(self):
        simple = ("ENTER_CONCEALMENT", "STOP_MOVE", "WEAPON_UNFOLD", "WEAPON_UNLOCK", "WAIT")
        fields_by_kind = {kind: {} for kind in simple}
        fields_by_kind.update({
            "MOVE": {"destination_hex": 102},
            "WEAPON_LOCK": {"weapon_choices": [[1, 36]]},
            "SHOOT": {"target_id": 2, "weapon_choices": [[1, 36]]},
            "GUIDED_FIRE": {
                "guider_id": 1, "shooter_id": 3, "weapon_id": 72,
                "fire_mode": "GUIDED", "target_id": 2,
            },
            "INDIRECT_FIRE": {
                "shooter_id": 1, "weapon_id": 72,
                "fire_mode": "INDIRECT", "target_hex": 104,
            },
            **{kind: {"target_id": 2} for kind in ("BOARD", "UNLOAD", "CAPTURE")},
        })
        self.assertEqual(set(fields_by_kind), set(PRODUCT_OPERATION_TYPES))
        for kind, fields in fields_by_kind.items():
            with self.subTest(operation=kind):
                operation = _operation({
                    "operation_type": kind, "actor_ids": [1], **fields,
                })
                forwarded = operation.preview_arguments()
                self.assertEqual(forwarded["operation_type"], kind)
                self.assertEqual(forwarded["actor_ids"], (1,))
                for field, expected in fields.items():
                    if field == "weapon_choices":
                        expected = dict(expected)
                    self.assertEqual(forwarded[field], expected)

    def test_schemas_retain_optional_fields_and_domain_authority(self):
        schemas = {value["name"]: value["parameters"] for value in function_schemas()}
        region = schemas["game_region_visibility"]
        self.assertEqual(
            set(region["required"]),
            {"observation_version", "actor_id", "region", "target_profile"},
        )
        self.assertNotIn("actor_assertion", region["required"])
        effect = schemas["game_fire_effect"]
        self.assertEqual(set(effect["required"]), {"observation_version", "actor_id", "weapon_id"})
        region["properties"]["target_profile"]["properties"]["target_class"]["enum"].clear()
        fresh = {value["name"]: value["parameters"] for value in function_schemas()}
        target_schema = fresh["game_region_visibility"]["properties"]["target_profile"]
        self.assertTrue(target_schema["properties"]["target_class"]["enum"])


if __name__ == "__main__":
    unittest.main()
