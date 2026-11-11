"""Adapter tests use fake protocols only; never import the uploaded package."""
import random
import unittest
from copy import deepcopy

from wargame_native_engine.agents.miaosuan import MiaosuanBaselineAdapter, build_miaosuan_setup
from types import SimpleNamespace
from fractions import Fraction
from wargame_native_engine.io.scenario import NativeHex
from wargame_native_engine.core.action import ActionType, EngineAction, ValidAction


def frame():
    return {"extra": {"native": {"perspective": "RED"}},
            "operators": [{"obj_id": 1, "color": 0},
                          {"obj_id": 2, "color": 1, "observe_distance": [], "blood": None}],
            "passengers": [], "communication": [{"private": "not forwarded"}],
            "time": {"stage": 2, "cur_step": 0}}


class FakeAgent:
    def __init__(self, actions=()):
        self.actions = list(actions)
        self.observation = None
        self.draws = []

    def setup(self, info):
        self.info = info
        self.draws.append(random.random())

    def step(self, observation):
        self.observation = observation
        self.draws.append(random.random())
        return self.actions

    def reset(self):
        self.draws.clear()


def adapter(agent, seed=11):
    value = MiaosuanBaselineAdapter(agent, side="red", seed=seed)
    value.setup({"seat": 1, "faction": 0, "role": 1})
    return value


def legal(kind, actor=1, target=None, **parameters):
    return ValidAction(EngineAction("red", actor, kind, target_id=target,
                                   parameters=parameters), priority=0)


class MiaosuanAdapterTests(unittest.TestCase):
    def test_projection_detached_unknown_enemy_and_role_policy(self):
        source = frame()
        before = deepcopy(source)
        agent = FakeAgent()
        adapted = adapter(agent)
        adapted.step(source, [legal(ActionType.MOVE, path=[101])])
        self.assertEqual(agent.observation["valid_actions"], {1: {1: []}})
        self.assertEqual(agent.observation["role_and_grouping_info"],
                         {1: {"role": 1, "faction": 0, "operators": [1]}})
        self.assertEqual(agent.observation["communication"], [])
        enemy = agent.observation["operators"][1]
        self.assertIsNone(enemy["observe_distance"])
        self.assertIsNone(enemy["blood"])
        agent.observation["operators"].clear()
        self.assertEqual(source, before)

    def test_exact_authority_candidates_and_no_silent_output_loss(self):
        agent = FakeAgent([
            {"actor": 1, "obj_id": 1, "type": 1, "move_path": [101]},
            {"actor": 1, "obj_id": 1, "type": 1, "move_path": [999]},
            {"actor": 1, "type": 100, "info": {}},
            {"actor": 1, "type": 204, "msg_body": "hello"},
            {"actor": 1, "obj_id": 2, "type": 5},
            {"actor": 1, "obj_id": 1, "type": 20},
            {"actor": 2, "obj_id": 1, "type": 10}, None])
        move = legal(ActionType.MOVE, path=[101], movement_mode="NORMAL")
        result = adapter(agent).step(frame(), [move])
        self.assertEqual(result.actions, (move.action,))
        self.assertEqual(len(result.trace), len(agent.actions))
        self.assertEqual([value["status"] for value in result.trace],
                         ["translated", "rejected", "unsupported", "unsupported",
                          "rejected", "unsupported", "rejected", "rejected"])

    def test_shoot_and_board_preserve_native_authority(self):
        values = [legal(ActionType.SHOOT, target=2, weapon_id="w"),
                  legal(ActionType.BOARD, target=3, profile_id="p", authority_hash="hash")]
        source = frame()
        source["operators"].append({"obj_id": 3, "color": 0})
        agent = FakeAgent([{"actor": 1, "obj_id": 1, "type": 2,
                            "target_obj_id": 2, "weapon_id": "w"},
                           {"actor": 1, "obj_id": 1, "type": 3, "target_obj_id": 3}])
        result = adapter(agent).step(source, values)
        self.assertEqual(result.actions, tuple(value.action for value in values))
        self.assertEqual(agent.observation["valid_actions"][1][2][0]["attack_level"], 0)
        self.assertEqual(result.actions[1].parameters["authority_hash"], "hash")

    def test_private_or_wrong_side_frame_refused(self):
        adapted = adapter(FakeAgent())
        for perspective in ("GREEN", "BLUE", None):
            source = frame()
            source["extra"]["native"]["perspective"] = perspective
            with self.assertRaisesRegex(ValueError, "restricted_side_frame"):
                adapted.step(source, [])

    def test_seed_and_reset_isolation(self):
        before = random.getstate()
        first, second = FakeAgent(), FakeAgent()
        left, right = adapter(first, 57), adapter(second, 57)
        left.step(frame(), [])
        right.step(frame(), [])
        self.assertEqual(first.draws, second.draws)
        expected = list(first.draws)
        self.assertEqual(random.getstate(), before)
        left.reset()
        with self.assertRaisesRegex(RuntimeError, "not_setup"):
            left.step(frame(), [])
        left.setup({"seat": 1, "faction": 0, "role": 1})
        left.step(frame(), [])
        self.assertEqual(first.draws, expected)
        self.assertEqual(random.getstate(), before)

    def test_static_setup_coordinates_costs_visibility_and_no_enemy_setup(self):
        class Movement:
            rows, columns = 1, 2
            def factor(self, mode, source, target):
                return Fraction(3, 2) if (source, target) == (0, 1) else None
        class Visibility:
            def visible(self, first, second, **heights):
                self.last = (first, second, heights)
                return True
        visibility = Visibility()
        runtime = SimpleNamespace(movement=Movement(), visibility=visibility,
                                  identity_record=lambda: {"map_id": "test"})
        native_map = SimpleNamespace(hexes=(
            NativeHex(0, 20, 1, (1, -1, -1, -1, -1, -1)),
            NativeHex(1, 40, 2, (0, -1, -1, -1, -1, -1))))
        bundle = SimpleNamespace(map_runtime_data=runtime,
                                 scenario=SimpleNamespace(map=native_map,
                                                          operators=["PRIVATE_ENEMY"]),
                                 scenario_identity=SimpleNamespace(scenario_id="scenario"))
        setup = build_miaosuan_setup(bundle, "red")
        self.assertEqual(setup["scenario"], "scenario")
        self.assertEqual(setup["basic_data"]["map_data"][0][1]["pos"], 1)
        self.assertEqual(setup["cost_data"][0][0][0], {1: 1.5})
        self.assertTrue(setup["see_data"][2][0, 0, 0, 1])
        self.assertEqual(visibility.last, (0, 1, {"observer_height_m": 200,
                                               "target_height_m": 0}))
        copied = deepcopy(setup)
        self.assertIs(copied["see_data"][0], setup["see_data"][0])
        copied["cost_data"][0][0][0][1] = 999
        self.assertEqual(setup["cost_data"][0][0][0][1], 1.5)
        self.assertNotIn("PRIVATE_ENEMY", str(setup))

    def test_indirect_fire_uses_authority_builder_and_audits_rejection(self):
        from wargame_native_engine.core.fire_capability import (
            IndirectFireCapability, IndirectFireCapabilityStatus)
        from wargame_native_engine.rules.indirect_fire import (
            ArtilleryClass, IndirectRangePolicy, IndirectTargetDomain)
        capability = IndirectFireCapability(
            1, 7, ArtilleryClass.LIGHT, "profile",
            IndirectRangePolicy.BOUNDED_HEX_DISTANCE, 0, 20,
            IndirectTargetDomain.AREA, IndirectFireCapabilityStatus.AVAILABLE, ())
        calls = []
        def builder(capability, target):
            calls.append(target)
            if target == 99999:
                raise ValueError("out of map")
            return EngineAction("red", 1, ActionType.FIRE,
                                parameters={"fire_mode": "INDIRECT", "weapon_id": 7,
                                            "target_hex": target, "authority": "bound"})
        base = {"actor": 1, "obj_id": 1, "type": 8, "weapon_id": 7}
        agent = FakeAgent([{**base, "jm_pos": 101}, {**base, "jm_pos": 99999},
                           {**base, "jm_pos": True}])
        batch = adapter(agent).step(frame(), [], indirect_capabilities=[capability],
                                    indirect_action_builder=builder)
        self.assertEqual(calls, [101, 99999])
        self.assertEqual(len(batch.actions), 1)
        self.assertEqual(batch.actions[0].parameters["authority"], "bound")
        self.assertEqual(agent.observation["valid_actions"][1][8], [{"weapon_id": 7}])
        self.assertEqual([row["status"] for row in batch.trace],
                         ["translated", "rejected", "rejected"])
        self.assertNotIn("indirect_action_builder", agent.observation)

    def test_exception_restores_rng(self):
        class Broken(FakeAgent):
            def step(self, observation):
                random.random()
                raise RuntimeError("fake_failure")
        adapted = adapter(Broken())
        before = random.getstate()
        with self.assertRaisesRegex(RuntimeError, "fake_failure"):
            adapted.step(frame(), [])
        self.assertEqual(random.getstate(), before)


if __name__ == "__main__":
    unittest.main()
