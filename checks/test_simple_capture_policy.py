"""Deterministic rule decisions over detached side evidence, then real ticks."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from wargame_native_engine.agents.protocol import AgentResetContext
from wargame_native_engine.agents.seat import AgentDecisionContext, SeatActionCapabilities
from wargame_native_engine.agents.simple_capture import SimpleCaptureSeatAgent


def action(kind, actor=1, *, target=None, weapon=None):
    return {"action": {"side": "red", "actor_id": actor, "action_type": kind,
                       "target_id": target, "parameters": {} if weapon is None else {"weapon_id": weapon}}}


def context(*, locked=True, actors=None, actions=None, enemies=None, objectives=None, ammo=10, tick=0):
    actors = actors or [{"operator_id": 1, "side": "red", "operator_type": "VEHICLE", "alive": True,
                        "deployed": True, "stopped": True, "position": [1, 1], "state": "STOPPED"}]
    inventory = [{"operator_id": actor["operator_id"], "weapon_locked": locked,
                  "locked_weapon_id": 36 if locked else None,
                  "weapons": [{"weapon_id": 36, "attack_modes": ["DIRECT"], "ammunition": ammo}]}
                 for actor in actors]
    summary = {"policy_own_weapon_inventory": inventory,
               "policy_weapon_catalog": {"weapons": [{"weapon_id": 36, "fire_modes": ["DIRECT"],
                                                       "maximum_range": 3, "target_domains": ["VEHICLE", "PERSONNEL"]}]}}
    observation = {"side": "red", "friendly_operators": actors, "visible_enemies": enemies or [],
                   "objectives": objectives if objectives is not None else [{"objective_id": 7, "x": 6, "y": 1}]}
    capabilities = SeatActionCapabilities("red", tick, tuple(a["operator_id"] for a in actors),
                                         ("MOVE", "SHOOT", "WEAPON_LOCK", "STOP_MOVE", "CAPTURE"),
                                         tuple(actions or [action("MOVE"), action("WEAPON_LOCK", weapon=36)]), summary)
    return AgentDecisionContext("red", tick, tick, tick * 1000, observation, capabilities)


class SimpleCapturePolicyTests(unittest.TestCase):
    def agent(self, preview=None):
        agent = SimpleCaptureSeatAgent(preview=preview or (lambda c, o: {"can_execute": True}))
        agent.reset(AgentResetContext("s", "fixture", "red", 42, "1", agent.agent_id))
        return agent

    def test_locks_before_move_and_waits_for_lock_completion(self):
        agent = self.agent()
        ctx = context(locked=False)
        self.assertEqual(agent.act(context=ctx).operations[0].operation_type, "WEAPON_LOCK")
        actor = dict(ctx.observation["friendly_operators"][0], transition_kind="WEAPON_LOCK")
        self.assertEqual(agent.act(context=context(locked=False, actors=[actor])).operations, ())
        self.assertEqual(agent.act(context=context()).operations[0].operation_type, "MOVE")

    def test_only_visible_ready_in_range_exact_weapon_target_is_shot(self):
        agent = self.agent()
        enemies = [{"operator_id": i, "operator_type": "VEHICLE", "alive": True, "position": [3, 1]} for i in (3, 2)]
        actions = [action("SHOOT", target=i, weapon=36) for i in (3, 2, 99)] + [action("MOVE")]
        decision = agent.act(context=context(actions=actions, enemies=enemies))
        shot = decision.operations[0]
        self.assertEqual((shot.operation_type, shot.target_id, shot.weapon_choices), ("SHOOT", 2, ((1, 36),)))
        for changes in ({"ammo": 0}, {"enemies": []}, {"enemies": [dict(enemies[0], position=[9, 1])]},
                        {"enemies": [dict(enemies[0], alive=False)]}):
            values = dict(actions=actions, enemies=enemies)
            values.update(changes)
            self.assertEqual(agent.act(context=context(**values)).operations[0].operation_type, "MOVE")
        actor = dict(context().observation["friendly_operators"][0], weapon_cooldown_remaining_ms=1)
        self.assertEqual(agent.act(context=context(actions=actions, enemies=enemies, actors=[actor])).operations[0].operation_type, "MOVE")

    def test_projected_contact_hex_supports_interpolated_moving_enemy(self):
        ctx = context(actions=[action("SHOOT", target=2, weapon=36)], enemies=[
            {"operator_id": 2, "operator_type": "VEHICLE", "alive": True, "position": [3.2, 1]}])
        summary = dict(ctx.operation_capabilities.operation_summary, policy_visible_enemy_hexes={2: 103})
        ctx = replace(ctx, operation_capabilities=replace(ctx.operation_capabilities, operation_summary=summary))
        self.assertEqual(self.agent().act(context=ctx).operations[0].target_id, 2)

    def test_one_bundle_multiple_actors_and_no_route_reset(self):
        actors = [dict(context().observation["friendly_operators"][0], operator_id=i) for i in (2, 1)]
        actions = [action("MOVE", actor=i) for i in (1, 2)]
        decision = self.agent().act(context=context(actors=actors, actions=actions))
        self.assertEqual([o.actor_ids for o in decision.operations], [(1,), (2,)])
        actors[0].update(has_remaining_movement_task=True, has_active_segment=True, stopped=False)
        decision = self.agent().act(context=context(actors=actors, actions=actions))
        self.assertEqual([o.actor_ids for o in decision.operations], [(1,)])

    def test_stop_once_then_wait_until_capture_and_skip_own_objective(self):
        agent = self.agent()
        actor = dict(context().observation["friendly_operators"][0], stopped=False, has_active_segment=True)
        objectives = [{"objective_id": 7, "x": 2, "y": 1}]
        ctx = context(actors=[actor], objectives=objectives, actions=[action("STOP_MOVE"), action("CAPTURE", target=7)])
        self.assertEqual(agent.act(context=ctx).operations[0].operation_type, "STOP_MOVE")
        self.assertEqual(agent.act(context=ctx).operations, ())
        actor.update(stopped=True, has_active_segment=False, move_to_stop_remaining_ms=1)
        self.assertEqual(agent.act(context=context(actors=[actor], objectives=objectives)).operations, ())
        actor["move_to_stop_remaining_ms"] = 0
        ctx = context(actors=[actor], objectives=objectives, actions=[action("CAPTURE", target=7)])
        self.assertEqual(agent.act(context=ctx).operations[0].operation_type, "CAPTURE")
        objectives[0]["controlling_side"] = "red"
        self.assertEqual(agent.act(context=context(objectives=objectives)).operations, ())

    def test_unreachable_nearest_objective_skipped_no_privileged_inputs(self):
        calls = []
        def preview(ctx, operation):
            calls.append(operation.destination_hex)
            return {"can_execute": operation.destination_hex == 108}
        ctx = context(objectives=[{"objective_id": 9, "x": 8, "y": 1}, {"objective_id": 7, "x": 6, "y": 1}])
        before = deepcopy(ctx)
        self.assertEqual(self.agent(preview).act(context=ctx).operations[0].destination_hex, 108)
        self.assertEqual(calls, [106, 108])
        self.assertEqual(ctx, before)

    def test_dead_air_mounted_and_unsupported_units_are_not_commanded(self):
        base = context().observation["friendly_operators"][0]
        for changes in ({"alive": False}, {"deployed": False}, {"operator_type": "AIRCRAFT"}, {"state": "ON_BOARD"}):
            self.assertEqual(self.agent().act(context=context(actors=[dict(base, **changes)])).operations, ())
        ctx = context()
        cap = replace(ctx.operation_capabilities, operation_summary={})
        self.assertEqual(self.agent().act(context=replace(ctx, operation_capabilities=cap)).operations, ())
        with self.assertRaisesRegex(ValueError, "seat_mismatch"):
            self.agent().act(context=replace(ctx, side="blue"))


class SimpleCaptureActualSessionTests(unittest.TestCase):
    def test_actual_lock_move_stop_capture_no_stop_spam_and_same_seed_determinism(self):
        from wargame_native_engine.agents.realtime import ManualRealtimeClock, RealtimeAgentSession
        from wargame_native_engine.data import ScenarioBundle
        from wargame_native_engine.io.scenario import scenario_from_dict
        from wargame_native_engine.operations import ScenarioMatchFactory
        root = Path(__file__).resolve().parents[1]
        record = json.loads((root / "examples/source_movement_scenario.json").read_text())
        record["objectives"] = [{"objective_id": 7, "x": 4, "y": 1}]
        record["operators"][0]["raw_attributes"]["ammunition"] = {"0": 0}
        traces = []
        for run in range(2):
            with tempfile.TemporaryDirectory() as directory:
                session = RealtimeAgentSession(ScenarioMatchFactory(
                    bundle=ScenarioBundle.from_scenario(scenario_from_dict(record)), storage_root=Path(directory),
                    red_controller_id="r", blue_controller_id="b", decision_interval_ms=1000,
                    frame_interval_ms=1000), run_namespace="simple-rule-test", tick_interval_ms=1000,
                    clock=ManualRealtimeClock())
                try:
                    reset = session.reset(seed=42)
                    binding = reset.red_binding
                    catalog = session.weapon_catalog_snapshot(binding)[1]
                    def preview(ctx, operation):
                        return session.preview_operation(binding, operation,
                            observation_version=session.latest_view(binding).observation_version,
                            request_id=f"move-{ctx.decision_index}-{operation.destination_hex}").to_record()
                    agent = SimpleCaptureSeatAgent(preview=preview)
                    agent.reset(session.agent_reset_context(binding, agent_id=agent.agent_id))
                    trace = []
                    for tick in range(300):
                        view, controller = session.controller_snapshot(binding)
                        capabilities = replace(view.capabilities, operation_summary=dict(
                            view.capabilities.operation_summary, policy_own_weapon_inventory=controller.own_weapon_inventory,
                            policy_weapon_catalog=catalog))
                        ctx = AgentDecisionContext("red", view.tick_index, view.frame_index, view.sim_time_ms,
                            view.observation, capabilities, view.decision_semantic, view.action_catalog)
                        decision = agent.act(context=ctx)
                        if decision.operations:
                            trace.append((tick, decision.to_record()))
                            receipt = session.submit(binding, observation_version=view.observation_version, decision=decision)
                            self.assertTrue(receipt.accepted)
                        session.advance_tick()
                        if session.latest_view(binding).observation["objectives"][0]["controlling_side"] == "red":
                            break
                    self.assertEqual(session.latest_view(binding).observation["objectives"][0]["controlling_side"], "red")
                    kinds = [v[1]["operations"][0]["operation_type"] for v in trace]
                    self.assertEqual(kinds, ["WEAPON_LOCK", "MOVE", "STOP_MOVE", "CAPTURE"])
                    self.assertTrue(all(event.status.value == "APPLIED" for event in session.command_events))
                    traces.append(trace)
                finally:
                    session.close()
        self.assertEqual(traces[0], traces[1])


if __name__ == "__main__":
    unittest.main()
