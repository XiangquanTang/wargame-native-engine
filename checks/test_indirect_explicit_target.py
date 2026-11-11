"""Indirect gameplay requires an explicit target; blank hexes are resource-only."""
import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path

from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.indirect_fire import FireMission
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.rules.indirect_fire import IndirectRangePolicy

ROOT = Path(__file__).resolve().parents[1]


def fixture(*, ammunition=2):
    record = json.loads((ROOT / 'examples/source_movement_scenario.json').read_text())
    actor = record['operators'][0]
    actor['weapon_ids'] = [72]
    actor['weapon_unfolded'] = True
    actor['raw_attributes']['ammunition'] = {'0': ammunition}
    for cell in record['map']['hexes']:
        cell.update(elev=0, cond=0)
    return record


class ExplicitIndirectTests(unittest.TestCase):
    def engine(self, *, ammunition=2, seed=42):
        engine = NativeEngine(scenario_from_dict(fixture(ammunition=ammunition)))
        engine.reset(seed=seed)
        self.addCleanup(engine.close)
        return engine

    def test_empty_occupied_hex_consumes_resources_but_never_damage_or_rng(self):
        engine = self.engine()
        before = copy.deepcopy(engine._require_world().operators[2].to_dict())
        rng = engine._rng.snapshot()
        action = engine.indirect_fire_action(engine.get_indirect_fire_capabilities('red')[0], 106)
        result = engine.step([action], delta_time_ms=1000)
        world = engine._require_world()
        self.assertEqual(world.operators[1].raw_attributes['ammunition']['0'], 1)
        self.assertGreater(world.operators[1].indirect_fire_planned_cooldown_remaining_ms, 0)
        self.assertEqual(world.operators[1].raw_attributes['empty_fire_exposure_frames_remaining'], 10)
        self.assertTrue(any(e.data.get('reason') == 'EMPTY_FIRE' for e in result.events))
        # Flight still respects normal timing, but landing cannot infer an occupant.
        engine.step([], delta_time_ms=150000)
        world = engine._require_world()
        mission = world.indirect_fire_missions[0]
        self.assertEqual(mission.phase.value, 'RESOLVED')
        self.assertEqual(mission.affected_operator_ids, ())
        self.assertEqual(world.operators[2].blood, before['blood'])
        self.assertEqual(world.operators[2].health, before['health'])
        self.assertEqual(world.operators[2].suppression_remaining_ms, 0)
        self.assertEqual(engine._rng.snapshot(), rng)

    def test_zero_inventory_rejected_before_action_creation(self):
        engine = self.engine(ammunition=0)
        capability = engine.get_indirect_fire_capabilities('red')[0]
        self.assertEqual(capability.status.value, 'BLOCKED')
        self.assertIn('indirect_fire_ammunition_unavailable', capability.blockers)
        with self.assertRaisesRegex(ValueError, 'INDIRECT_FIRE_AMMUNITION_UNAVAILABLE'):
            engine.indirect_fire_action(capability, 106)

    def test_target_binding_round_trips_and_no_implicit_collateral(self):
        engine = self.engine()
        world = engine._require_world()
        collateral = copy.deepcopy(world.operators[2])
        collateral.operator_id = 3
        world.operators[3] = collateral
        engine._refresh_source_observation()
        action = engine.indirect_fire_action(
            engine.get_indirect_fire_capabilities('red')[0], 106, target_id=2,
        )
        engine.step([action], delta_time_ms=1)
        world = engine._require_world()
        mission = world.indirect_fire_missions[0]
        self.assertEqual(mission.damage_target_id, 2)
        self.assertTrue(mission.damage_allowed)
        self.assertEqual(FireMission.from_dict(mission.to_dict()), mission)
        clone = WorldState.from_dict(world.to_dict())
        self.assertEqual(clone.indirect_fire_missions[0], mission)
        self.assertEqual(engine._indirect_fire_service.mission_target_ids(world, 106, mission), (2,))
        self.assertEqual(engine._indirect_fire_service.mission_target_ids(world, 106, mission, excluded_operator_ids=(2,)), ())

    def test_selected_target_loses_blood_without_hitting_same_hex_neighbor(self):
        engine = self.engine(seed=1)
        world = engine._require_world()
        collateral = copy.deepcopy(world.operators[2])
        collateral.operator_id = 3
        world.operators[3] = collateral
        engine._refresh_source_observation()
        action = engine.indirect_fire_action(
            engine.get_indirect_fire_capabilities('red')[0], 106, target_id=2,
        )
        engine.step([action], delta_time_ms=1)
        result = engine.step([], delta_time_ms=150000)
        world = engine._require_world()
        self.assertEqual(world.operators[2].blood, 2)
        self.assertEqual(world.operators[3].blood, 3)
        self.assertEqual(world.indirect_fire_missions[0].affected_operator_ids, (2,))
        self.assertFalse(any(event.target_id == 3 for event in result.events))
        engine.step([], delta_time_ms=1000)
        self.assertEqual(engine._require_world().operators[3].blood, 3)

    def test_bounded_range_overshoot_and_under_minimum_spend_without_effect(self):
        # Explicit test-only bounded authority; published Map123 remains global.
        for minimum, maximum in [(0, 4), (6, 10)]:
            with self.subTest(minimum=minimum, maximum=maximum):
                engine = self.engine(seed=1)
                authority = engine._indirect_fire_authority
                profile = replace(
                    authority.profile_for_weapon(72),
                    range_policy=IndirectRangePolicy.BOUNDED_HEX_DISTANCE,
                    minimum_range=minimum, maximum_range=maximum,
                )
                engine._indirect_fire_authority = replace(
                    authority, profiles=tuple(
                        profile if item.weapon_id == 72 else item
                        for item in authority.profiles
                    ),
                )
                capability = engine.get_indirect_fire_capabilities('red')[0]
                validation = engine.validate_indirect_fire_target_hex(capability, 106)
                self.assertEqual(validation.status.value, 'VALID')
                self.assertEqual(validation.reason, 'TARGET_HEX_OUT_OF_RANGE_ZERO_DAMAGE')
                action = engine.indirect_fire_action(capability, 106, target_id=2)
                rng = engine._rng.snapshot()
                engine.step([action], delta_time_ms=1)
                world = engine._require_world()
                self.assertFalse(world.indirect_fire_missions[0].damage_allowed)
                self.assertEqual(world.operators[1].raw_attributes['ammunition']['0'], 1)
                self.assertEqual(world.operators[1].raw_attributes['empty_fire_exposure_frames_remaining'], 10)
                self.assertGreater(world.operators[1].indirect_fire_planned_cooldown_remaining_ms, 0)
                result = engine.step([], delta_time_ms=150000)
                self.assertEqual(engine._require_world().operators[2].blood, 3)
                self.assertEqual(engine._rng.snapshot(), rng)
                self.assertTrue(any(event.data.get('outcome') == 'OUT_OF_RANGE_ZERO_DAMAGE' for event in result.events))

    def test_bounded_authority_requires_valid_explicit_limits(self):
        profile = self.engine()._indirect_fire_authority.profile_for_weapon(72)
        for minimum, maximum in [(None, 5), (0, None), (6, 5), (-1, 5), (False, 5)]:
            with self.subTest(minimum=minimum, maximum=maximum):
                with self.assertRaises((ValueError, TypeError)):
                    replace(profile, range_policy=IndirectRangePolicy.BOUNDED_HEX_DISTANCE,
                            minimum_range=minimum, maximum_range=maximum)

    def test_invalid_target_binding_rejected_without_resources(self):
        engine = self.engine()
        capability = engine.get_indirect_fire_capabilities('red')[0]
        for target, hex_id, reason in [(True,106,'TARGET_ID_INVALID'), (1,101,'TARGET_NOT_ENEMY'), (2,104,'TARGET_HEX_MISMATCH')]:
            with self.subTest(target=target,hex_id=hex_id):
                with self.assertRaisesRegex(ValueError, reason):
                    engine.indirect_fire_action(capability, hex_id, target_id=target)
        self.assertEqual(engine._require_world().operators[1].raw_attributes['ammunition']['0'],2)


if __name__ == '__main__':
    unittest.main()
