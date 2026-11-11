"""Every product operation executes through tools and produces a strict replay."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from wargame_native_engine.agents.realtime import (
    ManualRealtimeClock,
    RealtimeAgentSession,
)
from wargame_native_engine.agents.tool_calling import GameToolAdapter
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.operations import ScenarioMatchFactory

ROOT = Path(__file__).resolve().parents[1]


class AllGameToolOperationsTests(unittest.TestCase):
    replay_mode = "legacy-debug"  # This historical suite asserts strict re-execution.
    def make_session(self, fixture, prepare=None):
        document = json.loads((ROOT / "examples" / fixture).read_text())
        if prepare is not None:
            prepare(document)
        scenario = scenario_from_dict(document)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        session = RealtimeAgentSession(
            ScenarioMatchFactory(
                bundle=ScenarioBundle.from_scenario(scenario),
                storage_root=directory / "storage",
                red_controller_id="operation-audit-red",
                blue_controller_id="operation-audit-blue",
                decision_interval_ms=1000,
                frame_interval_ms=1000,
                replay_mode=self.replay_mode,
            ),
            run_namespace="operation-audit",
            tick_interval_ms=1000,
            clock=ManualRealtimeClock(),
        )
        binding = session.reset(seed=42).red_binding
        self.addCleanup(session.close)
        return scenario, session, GameToolAdapter(session, binding), directory

    def execute(self, session, adapter, operation):
        version = adapter.dispatch("game_observation", {})["observation_version"]
        operation_type = operation["operation_type"]
        preview = adapter.dispatch("preview_game_operation", {
            "observation_version": version,
            "request_id": f"preview-{operation_type}-{version}",
            "operation": operation,
        })["preview"]
        self.assertTrue(preview["can_execute"], preview)
        arguments = {
            "preview_token": preview["preview_token"],
            "request_id": f"submit-{operation_type}-{version}",
            "confirm_partial": False,
        }
        command_count = len(session.command_events)
        receipt = adapter.dispatch("submit_game_operation", arguments)
        self.assertTrue(receipt["accepted"], receipt)
        self.assertEqual(receipt["status"], "PENDING")
        self.assertEqual(adapter.dispatch("submit_game_operation", arguments), receipt)
        session.advance_tick()
        feedback = adapter.dispatch("game_command_result", {
            "command_sequence": receipt["command_sequence"],
        })
        self.assertEqual(feedback["command"]["status"], "APPLIED", feedback)
        # APPLIED is only admission to the engine. Check native acceptance too.
        match = session._match
        native_results = match.gateway.manager.last_action_results(
            match.session_id, "red",
        )
        if operation_type == "WAIT":
            self.assertEqual(native_results, ())  # Whole-side wait emits no actor action.
        else:
            self.assertTrue(native_results, operation)
        self.assertTrue(all(result["accepted"] for result in native_results), native_results)
        self.assertEqual(adapter.dispatch("submit_game_operation", arguments), receipt)
        self.assertEqual(len(session.command_events), command_count + 1)
        return feedback

    def verify_replay(self, scenario, session, directory):
        session.publish(directory / "published")
        result = replay_source_archive(
            scenario, directory / "published/replay.zip", strict=True,
        )
        self.assertTrue(result.matches, result)

    def test_move_stop_and_wait(self):
        scenario, session, adapter, directory = self.make_session(
            "source_movement_scenario.json",
        )
        self.execute(session, adapter, {
            "operation_type": "MOVE", "actor_ids": [1], "destination_hex": 105,
        })
        self.execute(session, adapter, {
            "operation_type": "STOP_MOVE", "actor_ids": [1],
        })
        self.execute(session, adapter, {
            "operation_type": "WAIT", "actor_ids": [], "whole_side_wait": True,
        })
        self.verify_replay(scenario, session, directory)

    def test_direct_fire(self):
        scenario, session, adapter, directory = self.make_session(
            "maps/scenarios/direct-fire.json",
        )
        self.execute(session, adapter, {
            "operation_type": "SHOOT", "actor_ids": [1],
            "target_id": 2, "weapon_choices": [[1, 36]],
        })
        self.verify_replay(scenario, session, directory)

    def test_guided_fire(self):
        scenario, session, adapter, directory = self.make_session(
            "maps/scenarios/guided-fire.json",
        )
        self.execute(session, adapter, {
            "operation_type": "GUIDED_FIRE", "actor_ids": [20],
            "fire_mode": "GUIDED", "guider_id": 20, "shooter_id": 10,
            "weapon_id": 73, "target_id": 30,
        })
        self.verify_replay(scenario, session, directory)

    def test_indirect_fire(self):
        scenario, session, adapter, directory = self.make_session(
            "maps/scenarios/indirect-fire.json",
        )
        self.execute(session, adapter, {
            "operation_type": "INDIRECT_FIRE", "actor_ids": [1],
            "fire_mode": "INDIRECT", "shooter_id": 1,
            "weapon_id": 72, "target_hex": 102,
        })
        self.verify_replay(scenario, session, directory)

    def test_board_and_unload(self):
        scenario, session, adapter, directory = self.make_session(
            "maps/scenarios/boarding.json",
        )
        self.execute(session, adapter, {
            "operation_type": "BOARD", "actor_ids": [101], "target_id": 102,
        })
        # APPLIED starts the transition; unloading must await boarding completion.
        for _ in range(75):
            session.advance_tick()
        self.execute(session, adapter, {
            "operation_type": "UNLOAD", "actor_ids": [102], "target_id": 101,
        })
        self.verify_replay(scenario, session, directory)

    def test_capture(self):
        def prepare_uncontested_objective(document):
            document["operators"][0].update(x=2, current_hex=102)
            document["deployment"]["positions"]["red"]["1"] = [2, 1]
            # Air presence does not contest the explicit ground-capture region.
            document["operators"][1].update(operator_type="AIR", altitude=20)

        scenario, session, adapter, directory = self.make_session(
            "maps/scenarios/objective-control.json", prepare_uncontested_objective,
        )
        self.execute(session, adapter, {
            "operation_type": "CAPTURE", "actor_ids": [1], "target_id": 1,
        })
        self.verify_replay(scenario, session, directory)

    def test_enter_concealment(self):
        scenario, session, adapter, directory = self.make_session(
            "maps/scenarios/concealment.json",
        )
        self.execute(session, adapter, {
            "operation_type": "ENTER_CONCEALMENT", "actor_ids": [101],
        })
        self.verify_replay(scenario, session, directory)

    def test_lock_requires_valid_explicit_weapon_choice(self):
        _, session, adapter, _ = self.make_session('maps/scenarios/direct-fire.json')
        version = adapter.dispatch('game_observation', {})['observation_version']
        for choice in (None, 999):
            with self.subTest(choice=choice):
                operation = {'operation_type':'WEAPON_LOCK','actor_ids':[1]}
                if choice is not None:
                    operation['weapon_choices'] = [[1, choice]]
                result = adapter.dispatch('preview_game_operation', {
                    'observation_version': version,
                    'request_id': f'bad-lock-{choice}', 'operation': operation,
                })['preview']
                self.assertFalse(result['can_execute'])
        self.assertFalse(session.command_events)

    def test_weapon_lock_and_unfold(self):
        for operation_type, locked in (("WEAPON_LOCK", False), ("WEAPON_UNLOCK", True), ("WEAPON_UNFOLD", False)):
            with self.subTest(operation_type=operation_type):
                def prepare_posture(document):
                    document["operators"][0].update(
                        weapon_locked=locked, locked_weapon_id=36 if locked else None, weapon_unfolded=False,
                    )

                scenario, session, adapter, directory = self.make_session(
                    "maps/scenarios/direct-fire.json", prepare_posture,
                )
                self.execute(session, adapter, {
                    "operation_type": operation_type, "actor_ids": [1],
                    **({"weapon_choices": [[1, 36]]} if operation_type == "WEAPON_LOCK" else {}),
                })
                self.verify_replay(scenario, session, directory)


if __name__ == "__main__":
    unittest.main()
