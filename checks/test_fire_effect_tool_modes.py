"""Public JSON tools retain GUIDED and INDIRECT mode semantics end to end."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from checks.test_fire_effect_query import PROFILE, guided_fixture
from checks.test_indirect_explicit_target import fixture as indirect_fixture
from wargame_native_engine.agents.realtime import ManualRealtimeClock, RealtimeAgentSession
from wargame_native_engine.agents.tool_calling import GameToolAdapter
from wargame_native_engine.agents.tool_transport import GameToolTransport
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.operations import ScenarioMatchFactory


class FireEffectToolModeTests(unittest.TestCase):
    def adapter(self, scenario):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        session = RealtimeAgentSession(
            ScenarioMatchFactory(
                bundle=ScenarioBundle.from_scenario(scenario_from_dict(scenario)),
                storage_root=Path(temporary.name),
                red_controller_id="mode-red", blue_controller_id="mode-blue",
                decision_interval_ms=1000, frame_interval_ms=1000,
            ),
            run_namespace="fire-effect-tool-modes", tick_interval_ms=1000,
            clock=ManualRealtimeClock(),
        )
        self.addCleanup(session.close)
        binding = session.reset(seed=42).red_binding
        adapter = GameToolAdapter(session, binding)
        return session, adapter, GameToolTransport(adapter)

    def query(self, transport, observation, **arguments):
        reply = transport.invoke("game_fire_effect", json.dumps({
            "observation_version": observation["observation_version"],
            **arguments,
        }))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["observation_version"], observation["observation_version"])
        return reply["result"]["effect"]

    def test_guided_query_uses_shooter_id_and_distinguishes_hit_from_damage(self):
        session, adapter, transport = self.adapter(guided_fixture())
        before = adapter.dispatch("game_observation", {})
        result = self.query(
            transport, before, actor_id=1, weapon_id=73,
            target_id=2, guider_id=3, fire_mode="GUIDED",
            target_profile={**PROFILE, "domain": "VEHICLE", "armor_class": "MEDIUM"},
        )
        self.assertEqual(result["status"], "RESOLVED")
        self.assertTrue(result["can_execute"])
        self.assertGreaterEqual(result["hit_probability"], result["damage_probability"])
        self.assertIn("GUIDED_HIT_EVENT", result["hit_probability_reason"])
        self.assertEqual(adapter.dispatch("game_observation", {}), before)
        self.assertFalse(session.command_events)

    def test_indirect_query_preserves_scatter_coverage_damage_and_launch_readiness(self):
        session, adapter, transport = self.adapter(indirect_fixture())
        before = adapter.dispatch("game_observation", {})
        result = self.query(
            transport, before, actor_id=1, weapon_id=72,
            target_id=2, fire_mode="INDIRECT", target_profile=PROFILE,
            indirect_profile={"observation_policy": "UNOBSERVED"},
        )
        self.assertEqual(result["status"], "RESOLVED")
        self.assertTrue(result["can_execute"])
        self.assertIsNone(result["hit_probability"])
        self.assertEqual(result["target_cell_affected_probability_fraction"], "5/6")
        self.assertAlmostEqual(result["dispersion_table_hit_probability"], 1 / 3)
        self.assertLessEqual(result["damage_probability"], result["target_cell_affected_probability"])
        self.assertGreater(result["indirect_context"]["flight_time_seconds"], 0)
        self.assertEqual(adapter.dispatch("game_observation", {}), before)
        self.assertFalse(session.command_events)

    def test_indirect_hypothesis_is_not_real_target_authorization(self):
        session, adapter, transport = self.adapter(indirect_fixture())
        before = adapter.dispatch("game_observation", {})
        result = self.query(
            transport, before, actor_id=1, weapon_id=72,
            target_hex=106, target_model="fixture-personnel", fire_mode="INDIRECT",
            target_profile=PROFILE,
            indirect_profile={"observation_policy": "TARGET_CORRECTED", "planned_hex": 106},
        )
        self.assertEqual(result["status"], "RESOLVED")
        self.assertIsNone(result["can_execute"])
        self.assertEqual(result["indirect_context"]["observation_policy"], "TARGET_CORRECTED")
        self.assertEqual(
            result["distribution_condition"],
            "launched_mission_frozen_target_initial_indirect_impact",
        )
        self.assertFalse(session.command_events)


if __name__ == "__main__":
    unittest.main()
