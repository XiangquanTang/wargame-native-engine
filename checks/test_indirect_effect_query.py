"""Exact indirect probability parity, bounded work and side-safe inputs."""

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
from itertools import product
import unittest
from unittest.mock import patch

from checks.test_indirect_explicit_target import fixture
from wargame_native_engine.adjudication.fire_effect_query import FireEffectQuery
from wargame_native_engine.adjudication.indirect_effect_query import (
    _damage_distribution, indirect_fire_probabilities,
)
from wargame_native_engine.adjudication.indirect_fire import (
    IndirectFireObservationPolicy, IndirectFireResolution,
    IndirectFireResolutionEvidence, IndirectFireRngState,
    IndirectFireRngTransition, IndirectFireScatterResult,
    IndirectFireTargetEvidence, _resolve_indirect_fire_target_effect,
)
from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.indirect_fire import FireMissionObservationEvidence
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.rules.indirect_fire import IndirectFireMissionState, IndirectRangePolicy

PROFILE = {
    "domain": "PERSONNEL", "blood": 3, "armor_class": "NONE",
    "concealed": False, "suppressed": False, "moving": False,
    "marching": False, "stacked": False, "close_combat": False,
}


class FixedNativeDice:
    """Controlled draws for the real resolver, not the estimator's helpers."""

    def __init__(self, dice, before=None):
        self.dice = iter(dice)
        self.state = dict(before or {"seed": 0, "state": 0, "draws": 0})

    def randbelow(self, limit):
        assert limit == 6
        self.state["draws"] += 1
        return next(self.dice) - 1

    def snapshot(self):
        return dict(self.state)


def observation_evidence(policy, *, world=None, authority=None):
    return FireMissionObservationEvidence(
        correction_level=policy, state_tick=0 if world is None else world.tick,
        state_time_ms=0 if world is None else world.elapsed_time_ms,
        state_hash="0" * 64 if world is None else canonical_hash(world.to_dict()),
        planned_hex=106, firing_side="red",
        policy_id="test" if authority is None else authority.policy_id,
        policy_hash="0" * 64 if authority is None else authority.content_hash,
        source_interface="test" if authority is None else authority.observation_interface,
        eligible_observer_ids=(1,), grid_observer_ids=() if policy == "UNOBSERVED" else (1,),
        target_observations=((1, 2),) if policy == "TARGET_CORRECTED" else (),
        selected_observer_id=None if policy == "UNOBSERVED" else 1,
        selected_target_id=2 if policy == "TARGET_CORRECTED" else None,
        reason="explicit test evidence",
    )


class IndirectEffectQueryTests(unittest.TestCase):
    def setUp(self):
        record = fixture()
        record["map"]["width"] = 80
        for position in (131, 151, 171):
            record["map"]["hexes"].append(dict(
                pos=position, elev=0, cond=0, neighbors=[-1] * 6,
                roads=[0] * 6, rivers=[0] * 6,
            ))
        self.engine = NativeEngine(scenario_from_dict(record))
        self.engine.reset(seed=1)
        self.addCleanup(self.engine.close)
        self.attacker = deepcopy(self.engine._world.operators[1])
        self.target = deepcopy(self.engine._world.operators[2])
        self.profile = self.engine._indirect_fire_authority.profile_for_weapon(72)

    def query(self, *, target_profile=None, **changes):
        args = dict(
            source_map=self.engine._source_map, profile=self.profile,
            damage_policy=self.engine._indirect_fire_damage_policy,
            execution_policy=self.engine._indirect_fire_execution_policy,
            attacker=self.attacker, target=self.target,
            target_profile=PROFILE if target_profile is None else target_profile,
            observation_policy="UNOBSERVED",
        )
        args.update(changes)
        return indirect_fire_probabilities(self.engine._adjudication_tables, **args)

    def impact(self, table_result):
        policy = self.engine._indirect_fire_execution_policy
        rng = IndirectFireRngState(0, 0, 0)
        return IndirectFireResolution(
            mission_id="test", shooter_id=1, weapon_id=72, planned_hex=106, impact_hex=106,
            execution_policy_id=policy.policy_id, execution_policy_hash=policy.content_hash,
            affected_operator_ids=(2,),
            scatter_result=IndirectFireScatterResult(1, 0, (2, 2), 4, "1-20", "test", table_result, 0),
            rng_transition=IndirectFireRngTransition(rng, rng),
            resolution_evidence=IndirectFireResolutionEvidence(
                observation_policy=IndirectFireObservationPolicy.UNOBSERVED,
                spotter_evidence="test", observation_evidence=observation_evidence("UNOBSERVED"),
                affected_hex_policy="IMPACT_HEX_ONLY", operator_order="OPERATOR_ID_ASCENDING",
                pre_state_hash="0" * 64, adjudication_table_hash=self.engine._adjudication_tables.content_hash,
            ),
        )

    def test_coverage_table_hit_and_damage_are_distinct(self):
        result = self.query()
        self.assertEqual(result["status"], "RESOLVED")
        self.assertEqual(result["target_cell_affected_probability_fraction"], "5/6")
        self.assertEqual(result["dispersion_table_hit_probability"], 1 / 3)
        self.assertAlmostEqual(result["out_of_map_probability"], 5 / 36)
        self.assertLess(result["damage_probability"], result["target_cell_affected_probability"])
        self.assertIsNone(result["hit_probability"])
        self.assertIsNone(result["can_execute_under_assumptions"])
        self.assertEqual(result["indirect_context"]["flight_time_seconds"], 150)
        self.assertEqual(result["indirect_context"]["active_duration_seconds"], 300)
        self.assertEqual(result["enumeration"], {"scatter_branches": 66, "damage_branches": 132})
        self.assertEqual(sum(Fraction(row["probability_fraction"]) for row in result["damage_distribution"]), 1)
        self.assertEqual(sum(Fraction(row["probability_fraction"]) for row in result["scatter_distribution"]), 1)
        self.assertAlmostEqual(result["expected_damage"], sum(row["losses"] * row["probability"] for row in result["damage_distribution"]))

    def test_probability_never_touches_live_rng_or_world_and_is_detached(self):
        before = self.engine.snapshot()
        expected = self.query()
        self.assertEqual(before, self.engine.snapshot())
        self.engine._world = None
        with patch("wargame_native_engine.core.rng.NativeRNG.restore", side_effect=AssertionError("query used RNG")):
            self.assertEqual(self.query(), expected)
        expected["damage_distribution"].clear()
        self.assertTrue(self.query()["damage_distribution"])

    def test_all_damage_faces_match_production_resolver(self):
        indirect = self.engine._adjudication_tables.document["indirect_fire"]
        policy = self.engine._indirect_fire_damage_policy
        cases = (
            (PROFILE, "OPEN", 3),
            (dict(PROFILE, blood=1, concealed=True), "FOREST", 1),
            (dict(PROFILE, moving=True, marching=True), "SETTLEMENT", 4),
            (dict(PROFILE, domain="VEHICLE", armor_class="LIGHT"), "OPEN", 2),
            (dict(PROFILE, domain="VEHICLE", armor_class="COMPOSITE", concealed=True), "FOREST", 4),
        )
        for weapon, (assumptions, terrain, strength), table_result in product((72, 88, 89), cases, ("HIT", "SCATTER")):
            with self.subTest(weapon=weapon, profile=assumptions, result=table_result):
                profile = self.engine._indirect_fire_authority.profile_for_weapon(weapon)
                base_table = "hit_target_result" if table_result == "HIT" else "off_target_result"
                expected, _, blockers = _damage_distribution(
                    indirect, base_table=base_table, profile=profile, damage_policy=policy,
                    shooter_strength=strength, target=self.target, target_profile=assumptions, terrain=terrain,
                )
                self.assertFalse(blockers)
                evidence = IndirectFireTargetEvidence(
                    operator_id=2, domain=UnitDomain(assumptions["domain"]), armor=ArmorClass(assumptions["armor_class"]),
                    blood=assumptions["blood"], terrain=terrain, concealed=assumptions["concealed"],
                    moving=assumptions["moving"], stacked=False, marching=assumptions["marching"],
                )
                actual = Counter()
                for dice in product(range(1, 7), repeat=3):
                    effect = _resolve_indirect_fire_target_effect(
                        self.impact(table_result), target=evidence, shooter_id=1, weapon_id=weapon,
                        artillery_class=profile.artillery_class, shooter_strength=strength,
                        indirect=indirect, modifiers_document=indirect["result_modifiers"],
                        table_content_hash=self.engine._adjudication_tables.content_hash,
                        damage_policy=policy, rng=FixedNativeDice(dice),
                    )
                    loss = int(effect.damage_result.split("_", 1)[1]) if effect.damage_result.startswith("LOSS_") else 0
                    actual[(loss, effect.suppression_result.value == "TRUE", effect.destroyed_result.value == "TRUE")] += Fraction(1, 216)
                self.assertEqual(actual, expected)

    def test_all_scatter_faces_match_actual_lifecycle_service(self):
        action = self.engine.indirect_fire_action(self.engine.get_indirect_fire_capabilities("red")[0], 106, target_id=2)
        self.engine.step([action], delta_time_ms=1)
        world = deepcopy(self.engine._world)
        world.indirect_fire_missions = (replace(world.indirect_fire_missions[0], phase=IndirectFireMissionState.ACTIVE, cancelable=False, remaining_flight_seconds=0, flight_elapsed_remainder_ms=0, active_started_tick=world.tick, active_remaining_seconds=300),)
        mission = world.indirect_fire_missions[0]
        snapshot = self.engine._rng.snapshot()
        for correction, launch_hex in product(("UNOBSERVED", "GRID_CORRECTED", "TARGET_CORRECTED"), (101, 131, 151, 171)):
            with self.subTest(correction=correction, launch_hex=launch_hex):
                world.indirect_fire_missions = (replace(mission, launch_hex=launch_hex),)
                attacker = deepcopy(self.attacker)
                attacker.current_hex = launch_hex
                actual = Counter()
                evidence = observation_evidence(correction, world=world, authority=self.engine._indirect_fire_observation_authority)
                for dice in product(range(1, 7), repeat=3):
                    with patch("wargame_native_engine.adjudication.indirect_fire_service.NativeRNG.restore", return_value=FixedNativeDice(dice, snapshot)):
                        result = self.engine._indirect_fire_service.resolve_impact(
                            world, snapshot, mission.mission_id,
                            authority=self.engine._indirect_fire_authority,
                            execution_policy=self.engine._indirect_fire_execution_policy,
                            observation_authority=self.engine._indirect_fire_observation_authority,
                            observation_evidence=evidence, spotter_evidence="test",
                        )
                    self.assertTrue(result.valid)
                    resolved = result.resolution or result.miss_resolution
                    cell = result.resolution.impact_hex if result.resolution else None
                    scatter = resolved.scatter_result
                    actual[(cell, scatter.table_result, scatter.scatter_distance)] += Fraction(1, 216)
                estimated = self.query(observation_policy=correction, attacker=attacker)
                expected = {(row["impact_hex"], row["table_result"], row["scatter_distance"]): Fraction(row["probability_fraction"]) for row in estimated["scatter_distribution"]}
                self.assertEqual(actual, expected)

    def frozen_projection(self):
        observation = self.engine.observe("red").to_dict()
        visible = {item["operator_id"] for item in observation["visible_enemies"]}
        source = {"operators": [
            {"obj_id": actor.operator_id, "cur_hex": actor.current_hex,
             **({"carry_weapon_ids": list(actor.weapon_ids)} if actor.side == "red" else {})}
            for actor in self.engine._world.operators.values()
            if actor.side == "red" or actor.operator_id in visible
        ]}
        return observation, source, [item.to_dict() for item in self.engine.valid_actions("red")], self.engine.own_weapon_inventory("red")

    def integrated_query(self, projection, **changes):
        args = dict(actor_id=1, weapon_id=72, target_id=2, target_profile=PROFILE,
                    fire_mode="INDIRECT", indirect_profile={"observation_policy": "TARGET_CORRECTED"})
        args.update(changes)
        return self.engine.query_fire_effect_from_projection("red", *projection, query=FireEffectQuery(**args))

    def test_integrated_indirect_query_uses_only_frozen_projection(self):
        projection = self.frozen_projection()
        before = self.engine.snapshot()
        expected = self.integrated_query(projection)
        self.assertEqual(expected["status"], "RESOLVED")
        self.assertTrue(expected["can_execute"])
        self.assertTrue(expected["can_execute_under_assumptions"])
        self.assertEqual(self.engine.snapshot(), before)
        self.engine._world.operators[2].blood = 1
        self.engine._world.operators[2].raw_attributes["armor_class"] = "COMPOSITE"
        self.engine._world = None
        with patch("wargame_native_engine.core.rng.NativeRNG.restore", side_effect=AssertionError("query used RNG")):
            self.assertEqual(self.integrated_query(projection), expected)

    def test_integrated_readiness_does_not_turn_offset_or_hypothesis_into_action(self):
        projection = self.frozen_projection()
        offset = self.integrated_query(projection, indirect_profile={"observation_policy": "UNOBSERVED", "planned_hex": 105})
        self.assertEqual(offset["status"], "RESOLVED")
        self.assertFalse(offset["can_execute"])
        self.assertIn("TARGET_HEX_MISMATCH", offset["execution_blockers"])
        hypothetical = self.integrated_query(projection, target_id=None, target_hex=106, target_model="source-1-2")
        self.assertEqual(hypothetical["status"], "RESOLVED")
        self.assertIsNone(hypothetical["can_execute"])
        missing = self.integrated_query(projection, indirect_profile=None)
        self.assertEqual(missing["reason_code"], "INDIRECT_PROFILE_REQUIRED")
        self.assertIsNone(missing["damage_probability"])

    def test_offset_target_uses_scatter_and_never_implicit_occupants(self):
        target = deepcopy(self.target)
        target.current_hex = 105
        result = self.query(target=target, planned_hex=106)
        self.assertEqual(result["target_cell_affected_probability_fraction"], "1/36")
        self.assertEqual(result["enumeration"]["damage_branches"], 66)
        self.assertLessEqual(result["damage_probability"], 1 / 36)

    def test_incompatible_or_missing_assumptions_fail_closed(self):
        cases = (
            ({"observation_policy": "UNRESOLVED"}, "INDIRECT_OBSERVATION_POLICY_REQUIRED"),
            ({"target_profile": dict(PROFILE, stacked=True)}, "INDIRECT_STACKED_PROFILE_INCOMPATIBLE"),
            ({"target_profile": dict(PROFILE, domain="VEHICLE", armor_class="NONE")}, "INDIRECT_DAMAGE_UNRESOLVED"),
            ({"planned_hex": 999}, "TARGET_HEX_NOT_FOUND"),
        )
        for args, reason in cases:
            with self.subTest(reason=reason):
                result = self.query(**args)
                self.assertEqual(result["status"], "UNRESOLVED")
                self.assertEqual(result["reason_code"], reason)
                self.assertIsNone(result["damage_probability"])

    def test_out_of_range_is_zero_and_query_budget_is_bounded(self):
        bounded = replace(self.profile, range_policy=IndirectRangePolicy.BOUNDED_HEX_DISTANCE, minimum_range=0, maximum_range=4)
        result = self.query(profile=bounded)
        self.assertEqual(result["damage_probability"], 0)
        self.assertEqual(result["damage_distribution"], [{"losses": 0, "probability": 1.0, "probability_fraction": "1/1"}])
        tables = deepcopy(self.engine._adjudication_tables)
        tables.document["indirect_fire"]["unobserved_dispersion"]["by_roll"]["2"][0] = "SCATTER_100000000"
        result = indirect_fire_probabilities(
            tables, source_map=self.engine._source_map, profile=self.profile,
            damage_policy=self.engine._indirect_fire_damage_policy,
            execution_policy=self.engine._indirect_fire_execution_policy,
            attacker=self.attacker, target=self.target, target_profile=PROFILE,
            observation_policy="UNOBSERVED",
        )
        self.assertEqual(result["reason_code"], "INDIRECT_QUERY_BUDGET_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
