"""Thin range tools share native/UI authority, cache and frozen seat evidence."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wargame_native_engine.agents.realtime import (
    ManualRealtimeClock, RealtimeAgentSession, RealtimeAgentSessionError,
    RealtimeSeatBinding,
)
from wargame_native_engine.agents.tool_calling import GameToolAdapter, function_schemas
from wargame_native_engine.agents.tool_transport import GameToolTransport
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.operations import ScenarioMatchFactory

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("game_visibility_range", "game_attack_range")


class GameRangeToolTests(unittest.TestCase):
    def make_session(self, record=None):
        if record is None:
            record = json.loads((ROOT / "examples/source_movement_scenario.json").read_text())
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        session = RealtimeAgentSession(
            ScenarioMatchFactory(
                bundle=ScenarioBundle.from_scenario(scenario_from_dict(record)),
                storage_root=Path(temporary.name),
                red_controller_id="range-red", blue_controller_id="range-blue",
                decision_interval_ms=1000, frame_interval_ms=1000,
            ),
            run_namespace="range-tools", tick_interval_ms=1000,
            clock=ManualRealtimeClock(),
        )
        self.addCleanup(session.close)
        reset = session.reset(seed=42)
        adapter = GameToolAdapter(session, reset.red_binding)
        return session, reset, adapter, GameToolTransport(adapter)

    def setUp(self):
        self.session, self.reset, self.adapter, self.transport = self.make_session()
        self.version = self.reset.red.observation_version
        match = self.session._require_match()
        self.manager = match.gateway.manager
        self.engine = self.manager._managed(match.session_id).environment.session.backend.engine

    def args(self, name, **overrides):
        result = {"observation_version": self.version, "actor_id": 1}
        if name == "game_attack_range":
            result["weapon_id"] = 36
        return {**result, **overrides}

    def query(self, name, **overrides):
        return self.adapter.dispatch(name, self.args(name, **overrides))

    def error(self, name, arguments=None, **overrides):
        reply = self.transport.invoke(name, json.dumps(
            self.args(name, **overrides) if arguments is None else arguments,
        ))
        self.assertFalse(reply["ok"], reply)
        return reply["error"]

    def test_imported_source_color_without_owner_survives_full_session_pipeline(self):
        from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
        record = json.loads((ROOT / "examples/source_movement_scenario.json").read_text())
        template = source_frame_template_for_scenario(scenario_from_dict(record))
        for unit in record["operators"]:
            source = deepcopy(next(value.raw for value in template.operators
                                   if value.obj_id == unit["operator_id"]))
            source.pop("owner")
            unit["raw_attributes"]["source_frame"] = source
        session, reset, adapter, _ = self.make_session(record)
        view, controller = session.controller_snapshot(reset.red_binding)
        self.assertTrue(all("owner" not in value for value in controller.source_frame["operators"]
                            if value["color"] == 0))
        args = {"observation_version": view.observation_version, "actor_id": 1}
        expected = session.tactical_overlay(reset.red_binding,
            observation_version=view.observation_version, unit_id=1).to_record()
        self.assertEqual(adapter.dispatch("game_visibility_range", args)["visibility_field"],
                         expected["visibility_field"])
        self.assertEqual(adapter.dispatch("game_attack_range", {**args, "weapon_id": 36})
                         ["weapon_envelope"]["hex_ids"],
                         expected["weapon_envelope"]["weapons"][0]["hex_ids"])
        self.assertEqual(adapter.dispatch("game_region_visibility", {**args,
            "region": {"x_min": 1, "y_min": 1, "width": 1, "height": 1},
            "target_profile": {"target_class": "VEHICLE", "concealed": False, "height_m": 0}})
                         ["visibility"]["cell_count"], 1)

    def test_native_parity_canonical_origin_and_complete_fields(self):
        native = self.session.tactical_overlay(
            self.reset.red_binding, observation_version=self.version, unit_id=1,
        ).to_record()
        visible = self.query(TOOLS[0])
        attack = self.query(TOOLS[1])
        self.assertEqual(visible["visibility_field"], native["visibility_field"])
        self.assertEqual(attack["weapon_envelope"], {
            "semantics": native["weapon_envelope"]["semantics"],
            **native["weapon_envelope"]["weapons"][0],
        })
        for result in (visible, attack):
            self.assertEqual(result["actor"], {
                "actor_id": 1, "current_hex": 101, "column": 1, "row": 1,
            })
            self.assertEqual(result["coordinate_system"], "oddr_column_row")
            self.assertEqual(result["observation_version"], self.version)
            self.assertEqual(result["frame_index"], self.reset.red.frame_index)
            self.assertEqual(result["sim_time_ms"], self.reset.red.sim_time_ms)
            self.assertEqual(result["episode_id"], self.reset.episode_id)
            self.assertEqual(result["side"], "red")
            self.assertNotIn("fire_targets", result)
        self.assertFalse(attack["readiness_evaluated"])
        self.assertEqual(visible["target_assumptions"]["height_m"], 0)

    def test_both_tools_and_browser_share_one_calculation_per_snapshot(self):
        with patch.object(
            self.manager, "query_unit_tactical_overlay_from_projection",
            wraps=self.manager.query_unit_tactical_overlay_from_projection,
        ) as evaluate:
            self.query(TOOLS[0])
            self.query(TOOLS[1])
            self.session.tactical_overlay(
                self.reset.red_binding, observation_version=self.version, unit_id=1,
            )
            self.assertEqual(evaluate.call_count, 1)
            self.session.advance_tick()
            self.version = self.session.latest_view(self.reset.red_binding).observation_version
            self.query(TOOLS[1])
            self.query(TOOLS[0])
            self.assertEqual(evaluate.call_count, 2)

    def test_read_only_detached_no_rng_tick_commands_or_retry_budget(self):
        before = deepcopy(self.engine.snapshot())
        observation = self.adapter.dispatch("game_observation", {})
        for name in TOOLS:
            result = self.query(name)
            expected = deepcopy(result)
            result["actor"].clear()
            result["visibility_field" if name == TOOLS[0] else "weapon_envelope"]["hex_ids"].clear()
            self.assertEqual(self.query(name), expected)
        self.assertEqual(self.engine.snapshot(), before)
        self.assertEqual(self.adapter.dispatch("game_observation", {}), observation)
        self.assertFalse(self.session.command_events)
        self.assertFalse(self.adapter._preview_requests)
        self.assertFalse(self.adapter._requests)

    def test_frozen_projection_never_reads_live_world_or_hidden_occupancy(self):
        expected = [self.query(name) for name in TOOLS]
        self.session._tactical_overlay_cache.clear()
        self.engine._world.operators[1].current_hex = 106
        self.engine._world.operators[2].current_hex = 101
        with patch.object(self.engine, "_require_world", side_effect=AssertionError("live read")):
            self.assertEqual([self.query(name) for name in TOOLS], expected)
        self.assertNotIn("target_id", json.dumps(expected))

    def test_foreign_unknown_actor_and_uncarried_weapon_fail_closed(self):
        for name in TOOLS:
            foreign = self.error(name, actor_id=2)
            unknown = self.error(name, actor_id=999)
            self.assertEqual(foreign, unknown)
            self.assertEqual(foreign["code"], "actor_not_owned")
        self.assertEqual(self.error(TOOLS[1], weapon_id=999)["code"], "weapon_not_carried")

    def test_strict_parameters_no_state_or_side_overrides(self):
        for name in TOOLS:
            for field in self.args(name):
                for invalid in (True, -1, 1.0, "1", None):
                    with self.subTest(name=name, field=field, invalid=invalid):
                        self.assertEqual(
                            self.error(name, **{field: invalid})["code"], "invalid_arguments",
                        )
            for extra in ({"side": "blue"}, {"current_hex": 102}, {"limit": 1},
                          {"region": {}}, {"target_id": 2}):
                self.assertEqual(self.error(name, **extra)["code"], "invalid_arguments")
            self.assertEqual(self.error(name, arguments={})["code"], "invalid_arguments")

    def test_stale_foreign_versions_and_reset_recovery(self):
        for name in TOOLS:
            reply = self.error(name, observation_version=self.reset.blue.observation_version)
            self.assertEqual(reply["code"], "observation_version_stale")
            self.assertEqual(reply["recovery"], "refresh_observation")
        self.session.advance_tick()
        for name in TOOLS:
            self.assertEqual(self.error(name)["code"], "observation_version_stale")
        self.session.reset(seed=43)
        for name in TOOLS:
            reply = self.error(name)
            self.assertEqual(reply["code"], "episode_changed")
            self.assertEqual(reply["recovery"], "create_adapter_for_current_episode")

    def test_tick_before_snapshot_rejects_and_tick_after_preserves_identity(self):
        original = self.session.tactical_overlay_snapshot
        def before(*args, **kwargs):
            self.session.advance_tick()
            return original(*args, **kwargs)
        with patch.object(self.session, "tactical_overlay_snapshot", before):
            self.assertEqual(self.error(TOOLS[0])["code"], "observation_version_stale")
        self.version = self.session.latest_view(self.reset.red_binding).observation_version
        expected = self.query(TOOLS[1])
        def after(*args, **kwargs):
            result = original(*args, **kwargs)
            self.session.advance_tick()
            return result
        with patch.object(self.session, "tactical_overlay_snapshot", after):
            self.assertEqual(self.query(TOOLS[1]), expected)

    def test_forged_binding_and_reset_during_query_fail_closed(self):
        forged = RealtimeSeatBinding(self.reset.red_binding.binding_id, "blue")
        with self.assertRaises(RealtimeAgentSessionError):
            self.session.tactical_overlay_snapshot(
                forged, observation_version=self.version, unit_id=1,
            )
        original = self.session.tactical_overlay_snapshot
        def reset_before(*args, **kwargs):
            self.session.reset(seed=44)
            return original(*args, **kwargs)
        with patch.object(self.session, "tactical_overlay_snapshot", reset_before):
            reply = self.error(TOOLS[0])
            self.assertEqual(reply["code"], "realtime_binding_invalid")

    def test_response_limits_reject_instead_of_truncating(self):
        self.adapter._max_response_bytes = 32
        for name in TOOLS:
            reply = self.error(name)
            self.assertEqual(reply["code"], "response_too_large")
            self.assertEqual(reply["recovery"], "host_intervention")
        self.adapter._max_response_bytes = 512 * 1024
        self.assertTrue(self.query(TOOLS[0])["visibility_field"]["hex_ids"])
        self.assertFalse(self.session.command_events)

    def test_missing_los_is_explicit_error_but_programming_errors_propagate(self):
        original = self.engine.map_runtime_data
        self.engine.map_runtime_data = SimpleNamespace(visibility=SimpleNamespace(
            line_of_sight=Mock(side_effect=ValueError("missing declared LOS channel")),
        ))
        try:
            reply = self.error(TOOLS[0])
            self.assertEqual(reply["code"], "tactical_overlay_query_rejected")
            self.assertIn("missing declared LOS channel", reply["message"])
        finally:
            self.engine.map_runtime_data = original
        with patch.object(self.session, "tactical_overlay_snapshot",
                          side_effect=RuntimeError("range bug")):
            with self.assertRaisesRegex(RuntimeError, "range bug"):
                self.error(TOOLS[1])

    def test_weapon_selection_not_union_and_readiness_not_authorization(self):
        record = json.loads((ROOT / "examples/source_movement_scenario.json").read_text())
        for cell in record["map"]["hexes"]:
            cell.update(elev=0, cond=0)
        record["operators"][0].update(
            weapon_ids=[36, 99], weapon_locked=True, locked_weapon_id=36,
            weapon_unfolded=False, weapon_cooldown_remaining_ms=5000,
        )
        weapons = record["metadata"]["source_compatible"]["weapons"]
        weapons["99"] = {**weapons["36"], "maximum_range": 1}
        session, reset, adapter, _ = self.make_session(record)
        version = reset.red.observation_version
        args = {"observation_version": version, "actor_id": 1}
        selected = adapter.dispatch(TOOLS[1], {**args, "weapon_id": 99})
        native = session.tactical_overlay(
            reset.red_binding, observation_version=version, unit_id=1,
        ).weapon_envelope
        self.assertEqual(selected["weapon_envelope"]["hex_ids"], list(native.weapons[1].hex_ids))
        self.assertLess(len(native.weapons[1].hex_ids), len(native.hex_ids))
        self.assertFalse(selected["readiness_evaluated"])
        preview = adapter.dispatch("preview_game_operation", {
            "observation_version": version, "request_id": "not-ready",
            "operation": {"operation_type": "SHOOT", "actor_ids": [1],
                          "weapon_choices": [[1, 99]], "target_hex": 102},
        })
        self.assertFalse(session.command_events)
        # v22 previews authenticate a fire intent, not final readiness. The
        # existing engine still rejects this intent at application time.
        receipt = adapter.dispatch("submit_game_operation", {
            "preview_token": preview["preview"]["preview_token"],
            "request_id": "not-ready-submit", "confirm_partial": False,
        })
        self.assertTrue(receipt["accepted"])
        session.advance_tick()
        outcome = adapter.dispatch("game_command_result", {
            "command_sequence": receipt["command_sequence"],
        })
        self.assertEqual(outcome["command"]["status"], "REJECTED_AT_APPLY")
        self.assertEqual(outcome["command"]["error_code"], "WEAPON_LOCKED_TO_OTHER_WEAPON")
        self.assertEqual(outcome["executions"][0]["execution_status"], "NOT_STARTED")

    def test_drone_origin_and_visibility_reuse_existing_ui_semantics(self):
        record = json.loads((ROOT / "examples/maps/scenarios/drone.json").read_text())
        session, reset, adapter, transport = self.make_session(record)
        args = {"observation_version": reset.red.observation_version, "actor_id": 1}
        result = adapter.dispatch(TOOLS[0], args)
        native = session.tactical_overlay(
            reset.red_binding, observation_version=args["observation_version"], unit_id=1,
        )
        self.assertEqual(result["visibility_field"], native.visibility_field.to_record())
        reply = transport.invoke(TOOLS[1], json.dumps({**args, "weapon_id": 36}))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "weapon_not_carried")

    def test_schemas_are_closed_and_detached(self):
        schemas = {value["name"]: value for value in function_schemas()}
        for name in TOOLS:
            schema = schemas[name]["parameters"]
            self.assertEqual(set(schema["required"]), set(self.args(name)))
            self.assertEqual(set(schema["properties"]), set(self.args(name)))
            self.assertFalse(schema["additionalProperties"])
            schema["properties"]["actor_id"]["minimum"] = -1
        fresh = {value["name"]: value for value in function_schemas()}
        self.assertEqual(fresh[TOOLS[0]]["parameters"]["properties"]["actor_id"]["minimum"], 0)


if __name__ == "__main__":
    unittest.main()
