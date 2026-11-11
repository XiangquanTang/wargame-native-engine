"""Committed game projectiles retain launch evidence across actor lifecycles."""
import copy
import tempfile
import unittest

from checks.test_indirect_explicit_target import fixture
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.replay import ReplayWriter, replay_actions
from wargame_native_engine.core.indirect_fire import FireMission
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.io.scenario import scenario_from_dict


class IndirectLaunchLifecycleTests(unittest.TestCase):
    def launch(self):
        engine = NativeEngine(scenario_from_dict(fixture()))
        engine.reset(seed=1)
        observer = copy.deepcopy(engine._require_world().operators[1])
        observer.operator_id = 3
        engine._require_world().operators[3] = observer
        engine._refresh_source_observation()
        self.addCleanup(engine.close)
        action = engine.indirect_fire_action(
            engine.get_indirect_fire_capabilities('red')[0], 106, target_id=2,
        )
        engine.step([action], delta_time_ms=1)
        return engine

    def test_actual_shooter_destruction_actions_strict_replay(self):
        record = fixture()
        launcher = record['operators'][0]
        launcher.update(blood=1, health=33)
        observer = copy.deepcopy(launcher)
        observer.update(operator_id=3, blood=3, health=100)
        attacker = copy.deepcopy(observer)
        attacker.update(operator_id=4, side='blue', current_hex=102, weapon_ids=[36])
        attacker['raw_attributes']['ammunition'] = {'0': 20}
        record['operators'].extend((observer, attacker))
        record['deployment']['positions']['red']['3'] = [1, 1]
        record['deployment']['positions']['blue']['4'] = [2, 1]
        scenario = scenario_from_dict(record)
        engine = NativeEngine(scenario)
        engine.reset(seed=1)
        self.addCleanup(engine.close)
        with tempfile.TemporaryDirectory() as directory:
            writer = ReplayWriter(directory, run_id='launch-lifecycle', scenario=scenario,
                                  seed=1, agents={'red': 'test', 'blue': 'test'},
                                  max_ticks=1000, ruleset=engine.ruleset, project_commit=None)
            writer.record_initial_state(engine.snapshot())
            launch = engine.indirect_fire_action(
                engine.get_indirect_fire_capabilities('red')[0], 106, target_id=2,
            )
            destroy = EngineAction('blue', 4, ActionType.SHOOT, target_id=1,
                                   parameters={'weapon_id': 36})
            for actions, elapsed in (([launch], 1), ([destroy], 1), ([], 150000), ([], 1000)):
                result = engine.step(actions, delta_time_ms=elapsed)
                self.assertTrue(all(decision.accepted for decision in result.decisions))
                writer.record_step(result, engine.snapshot())
            world = engine._require_world()
            self.assertFalse(world.operators[1].alive)
            self.assertEqual(world.indirect_fire_missions[0].launch_strength, 1)
            self.assertEqual(world.indirect_fire_missions[0].affected_operator_ids, (2,))
            writer.finalize({'result': engine.result().to_dict()}, status='completed')
            self.assertTrue(replay_actions(scenario, directory, strict=True).matches)

    def test_launch_contract_round_trip(self):
        mission = self.launch()._require_world().indirect_fire_missions[0]
        self.assertEqual((mission.launch_side, mission.launch_hex, mission.launch_strength),
                         ('red', 101, 3))
        self.assertEqual(FireMission.from_dict(mission.to_dict()), mission)
        self.assertEqual(mission.to_dict()['schema_version'], 'native-indirect-fire-mission/1.4')

    def test_flying_projectile_survives_launcher_lifecycle_and_restore(self):
        for change in ('dead', 'undeployed', 'off_map', 'removed', 'weakened'):
            with self.subTest(change=change):
                engine = self.launch()
                world = engine._require_world()
                shooter = world.operators[1]
                if change == 'dead':
                    shooter.alive = False
                    shooter.health = shooter.blood = 0
                elif change == 'undeployed':
                    shooter.deployed = False
                elif change == 'off_map':
                    shooter.current_hex = None
                elif change == 'removed':
                    del world.operators[1]
                else:
                    shooter.blood = 1
                # The engine clones this authoritative snapshot on the next step.
                clone = WorldState.from_dict(world.to_dict())
                self.assertEqual(clone.indirect_fire_missions, world.indirect_fire_missions)
                engine._world = clone
                engine.step([], delta_time_ms=150000)
                world = engine._require_world()
                self.assertEqual(world.operators[2].blood, 2)
                self.assertEqual(world.indirect_fire_missions[0].affected_operator_ids, (2,))
                self.assertEqual(len(engine.get_indirect_fire_active_areas('red')), 1)
                self.assertEqual(engine.get_indirect_fire_active_areas('blue'), ())
                view = engine.get_indirect_fire_mission_areas('red')[0].to_record()
                self.assertFalse({'launch_strength', 'launch_hex', 'launch_side'} & view.keys())
                engine.step([], delta_time_ms=1000)
                self.assertEqual(engine._require_world().operators[2].blood, 2)

    def test_ineligible_targets_are_not_replaced_by_collateral(self):
        for change in ('dead', 'off_map', 'undeployed', 'moved'):
            with self.subTest(change=change):
                engine = self.launch()
                target = engine._require_world().operators[2]
                if change == 'dead':
                    target.alive = False
                    target.health = target.blood = 0
                elif change == 'off_map':
                    target.current_hex = None
                elif change == 'undeployed':
                    target.deployed = False
                else:
                    target.current_hex = 105
                blood = target.blood
                engine.step([], delta_time_ms=150000)
                world = engine._require_world()
                self.assertEqual(world.operators[2].blood, blood)
                self.assertEqual(world.indirect_fire_missions[0].affected_operator_ids, ())

    def test_active_area_retains_launch_strength_for_first_time_target_entry(self):
        engine = self.launch()
        engine._require_world().operators[2].current_hex = 105
        engine.step([], delta_time_ms=150000)
        world = engine._require_world()
        self.assertEqual(world.indirect_fire_missions[0].phase.value, 'ACTIVE')
        self.assertEqual(world.indirect_fire_missions[0].affected_operator_ids, ())
        world.operators[1].alive = False
        world.operators[1].health = world.operators[1].blood = 0
        world.operators[2].current_hex = 106
        engine._world = WorldState.from_dict(world.to_dict())
        engine.step([], delta_time_ms=1000)
        self.assertEqual(engine._require_world().operators[2].blood, 2)
        engine.step([], delta_time_ms=1000)
        self.assertEqual(engine._require_world().operators[2].blood, 2)

    def test_current_schema_rejects_missing_launch_evidence(self):
        record = self.launch()._require_world().indirect_fire_missions[0].to_dict()
        record.pop('launch_strength')
        with self.assertRaisesRegex(ValueError, 'sealed launch evidence'):
            FireMission.from_dict(record)

    def test_active_projectile_does_not_repeat_damage_after_launcher_destruction(self):
        engine = self.launch()
        engine.step([], delta_time_ms=150000)
        shooter = engine._require_world().operators[1]
        shooter.alive = False
        shooter.blood = shooter.health = 0
        engine.step([], delta_time_ms=1000)
        self.assertEqual(engine._require_world().operators[2].blood, 2)


if __name__ == '__main__':
    unittest.main()
