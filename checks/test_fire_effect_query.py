"""Rule parity, honest probability semantics, and side-safe query boundaries."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from itertools import product
import json
from pathlib import Path
import unittest

from wargame_native_engine.adjudication import DirectFireAdjudicator, FixedDiceRoller
from wargame_native_engine.adjudication.fire_effect_query import (
    FireEffectQuery, FireEffectQueryError, direct_fire_probabilities,
)
from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.scenario import scenario_from_dict

ROOT = Path(__file__).resolve().parents[1]
PROFILE = {
    "domain": "PERSONNEL", "blood": 3, "armor_class": "NONE",
    "concealed": False, "suppressed": False, "moving": False,
    "marching": False, "stacked": False, "close_combat": False,
}


def fixture(*, visible=True):
    value = json.loads((ROOT / "examples/source_movement_scenario.json").read_text())
    if visible:
        for cell in value["map"]["hexes"]:
            cell.update(elev=0, cond=0)
    return value


def guided_fixture():
    value = fixture()
    value["operators"][0]["weapon_ids"] = [73]
    value["operators"][1].update(operator_type="VEHICLE")
    value["operators"][1]["raw_attributes"]["armor_class"] = "MEDIUM"
    guider = deepcopy(value["operators"][0])
    guider.update(operator_id=3, current_hex=104, x=4, weapon_ids=[29],
                  guidance_profile_id="GROUND_AFFILIATED_GUIDER",
                  guidance_affiliation_id=1,
                  guidance_affiliation_evidence={"source_field": "test", "source_value": 1,
                                                 "mapping_version": "test-v1"})
    value["operators"].append(guider)
    value["deployment"]["positions"]["red"]["3"] = [4, 1]
    value["metadata"]["source_compatible"]["weapons"]["73"] = {
        "weapon_class": "heavy_missile", "maximum_range": 20,
        "target_domains": ["VEHICLE"], "ammunition_key": "0",
        "requires_unfolded": True, "can_fire_while_moving": False,
    }
    return value


class FireEffectQueryTests(unittest.TestCase):
    def engine(self, *, visible=True):
        engine = NativeEngine(scenario_from_dict(fixture(visible=visible)))
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        return engine

    def projection(self, engine):
        observation = engine.observe("red").to_dict()
        visible = {item["operator_id"] for item in observation["visible_enemies"]}
        source = {"operators": [
            {"obj_id": actor.operator_id, "cur_hex": actor.current_hex,
             **({"carry_weapon_ids": list(actor.weapon_ids)} if actor.side == "red" else {})}
            for actor in engine._world.operators.values()
            if actor.side == "red" or actor.operator_id in visible
        ]}
        actions = [item.to_dict() for item in engine.valid_actions("red")]
        return observation, source, actions, engine.own_weapon_inventory("red")

    def query(self, engine, query=None, projection=None):
        query = query or FireEffectQuery(1, 36, target_id=2, target_profile=PROFILE)
        return engine.query_fire_effect_from_projection(
            "red", *(projection or self.projection(engine)), query=query,
        )

    def test_visible_query_is_exact_detached_and_does_not_mutate_engine(self):
        engine = self.engine()
        projection = self.projection(engine)
        before = engine.snapshot()
        result = self.query(engine, projection=projection)
        self.assertEqual(result["status"], "RESOLVED")
        self.assertIsNone(result["hit_probability"])
        self.assertTrue(result["can_execute"])
        self.assertGreater(result["damage_probability"], 0)
        self.assertAlmostEqual(sum(row["probability"] for row in result["damage_distribution"]), 1)
        self.assertAlmostEqual(result["expected_damage"], sum(
            row["losses"] * row["probability"] for row in result["damage_distribution"]
        ))
        self.assertEqual(engine.snapshot(), before)
        result["damage_distribution"].clear()
        self.assertTrue(self.query(engine, projection=projection)["damage_distribution"])

    def test_frozen_query_never_reads_live_world_or_enemy_template(self):
        engine = self.engine()
        projection = self.projection(engine)
        expected = self.query(engine, projection=projection)
        engine._world.operators[2].blood = 1
        engine._world.operators[2].raw_attributes["armor_class"] = "COMPOSITE"
        engine._world = None
        actual = self.query(engine, projection=projection)
        self.assertEqual(actual, expected)

    def test_missing_assumptions_do_not_reveal_enemy_strength(self):
        engine = self.engine()
        result = self.query(engine, FireEffectQuery(1, 36, target_id=2))
        self.assertEqual(result["reason_code"], "TARGET_PROFILE_REQUIRED")
        self.assertIsNone(result["damage_probability"])
        self.assertNotIn("blood", result["query"])

    def test_hidden_and_nonexistent_target_ids_have_same_failure(self):
        engine = self.engine(visible=False)
        for target_id in (2, 99999):
            with self.assertRaises(FireEffectQueryError) as caught:
                self.query(engine, FireEffectQuery(1, 36, target_id=target_id, target_profile=PROFILE))
            self.assertEqual(caught.exception.code, "TARGET_NOT_VISIBLE")

    def test_hypothetical_position_never_resolves_its_real_occupant(self):
        engine = self.engine()
        query = FireEffectQuery(1, 36, target_hex=106, target_model="source-1-2", target_profile=PROFILE)
        result = self.query(engine, query)
        self.assertEqual(result["status"], "RESOLVED")
        self.assertIsNone(result["can_execute"])
        self.assertIsNone(result["query"]["target_id"])
        self.assertEqual(result["query_kind"], "HYPOTHETICAL_TARGET")

    def test_actor_weapon_and_model_assertions_are_enforced(self):
        engine = self.engine()
        cases = [
            ({"actor_id": 2}, "ACTOR_NOT_CONTROLLED"),
            ({"actor_id": 99}, "ACTOR_NOT_CONTROLLED"),
            ({"weapon_id": 29}, "WEAPON_NOT_CARRIED"),
            ({"attacker_model": "source-1-2"}, "ATTACKER_MODEL_MISMATCH"),
            ({"target_model": "source-2-0"}, "TARGET_MODEL_MISMATCH"),
        ]
        for changes, code in cases:
            values = dict(actor_id=1, weapon_id=36, target_id=2, target_profile=PROFILE)
            values.update(changes)
            with self.subTest(code=code), self.assertRaises(FireEffectQueryError) as caught:
                self.query(engine, FireEffectQuery(**values))
            self.assertEqual(caught.exception.code, code)

    def test_changed_profile_changes_only_explicit_conditional_result(self):
        engine = self.engine()
        weak = dict(PROFILE, blood=1)
        result = self.query(engine, FireEffectQuery(1, 36, target_id=2, target_profile=weak))
        self.assertLessEqual(result["expected_damage"], 1)
        self.assertEqual(result["assumptions"]["blood"], 1)

    def test_unsupported_fire_modes_are_not_given_fabricated_probabilities(self):
        engine = self.engine()
        for mode, reason in (("GUIDED", "GUIDER_REQUIRED"), ("INDIRECT", "INDIRECT_PROFILE_REQUIRED")):
            result = self.query(engine, FireEffectQuery(1, 36, target_id=2, target_profile=PROFILE, fire_mode=mode))
            self.assertEqual(result["reason_code"], reason)
            self.assertIsNone(result["damage_probability"])

    def test_malformed_enum_values_raise_typed_validation_errors(self):
        for field in ("fire_mode", "domain", "armor_class"):
            for value in ([], {}, None, 1):
                data = dict(actor_id=1, weapon_id=36, target_id=2, target_profile=dict(PROFILE))
                if field == "fire_mode":
                    data[field] = value
                else:
                    data["target_profile"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(FireEffectQueryError):
                    FireEffectQuery.from_dict(data)

    def test_catalog_is_derived_and_marks_missing_runtime_profiles(self):
        engine = self.engine()
        before = engine.snapshot()
        catalog = engine.get_weapon_catalog()
        entries = {entry["weapon_id"]: entry for entry in catalog["weapons"]}
        self.assertEqual(entries[36]["runtime_profile"], fixture()["metadata"]["source_compatible"]["weapons"]["36"])
        self.assertIn("WEAPON_PROFILE_UNAVAILABLE", entries[1]["reason_codes"])
        self.assertTrue(entries[36]["compatible_unit_models"])
        self.assertIn("DIRECT", entries[36]["fire_modes"])
        self.assertIn("INDIRECT", entries[72]["fire_modes"])
        self.assertEqual(entries[72]["indirect_profile"]["ammunition_key"], "0")
        self.assertEqual(entries[72]["indirect_profile"]["ammunition_cost"], 1)
        self.assertIn("INDIRECT_AMMUNITION_MAPPING_UNAVAILABLE", entries[89]["reason_codes"])
        self.assertEqual(engine.snapshot(), before)
        entries[36]["runtime_profile"]["maximum_range"] = 999
        self.assertEqual(next(x for x in engine.get_weapon_catalog()["weapons"] if x["weapon_id"] == 36)["maximum_range"], 10)

    def test_unknown_table_cells_fail_closed(self):
        engine = self.engine()
        profile = dict(PROFILE, blood=999, domain="VEHICLE", armor_class="HEAVY")
        result = self.query(engine, FireEffectQuery(1, 36, target_hex=103, target_model="source-2-0", target_profile=profile))
        self.assertEqual(result["status"], "UNRESOLVED")
        self.assertIsNone(result["damage_probability"])

    def test_weighted_sums_match_exhaustive_actual_stage_faces(self):
        engine = self.engine()
        action = EngineAction("red", 1, ActionType.SHOOT, target_id=2, parameters={"weapon_id": 36})
        original = engine._direct_fire_request(action, engine._world.operators[1], engine._world.operators[2])
        for domain in (UnitDomain.PERSONNEL, UnitDomain.VEHICLE):
            request = replace(original, target_domain=domain, target_armor=ArmorClass.MEDIUM)
            expected = Counter()
            dice_count = 4 if domain is UnitDomain.VEHICLE else 3
            before = request.target.to_dict()
            for faces in product(range(1, 7), repeat=dice_count):
                resolved = DirectFireAdjudicator(engine._adjudication_tables, FixedDiceRoller(faces)).stage(request)
                self.assertIsNone(resolved.error)
                outcome = resolved.outcome
                expected[outcome.vehicle_losses + outcome.squad_losses] += 1
            result = direct_fire_probabilities(engine._adjudication_tables, request)
            actual = {row["losses"]: row["probability"] for row in result["damage_distribution"]}
            self.assertEqual(actual, {loss: weight / 6 ** dice_count for loss, weight in expected.items()})
            self.assertEqual(request.target.to_dict(), before)

    def test_guided_query_uses_legal_guidance_and_guider_visibility(self):
        engine = NativeEngine(scenario_from_dict(guided_fixture()))
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        query = FireEffectQuery(1, 73, target_id=2, guider_id=3, fire_mode="GUIDED",
                                target_profile=dict(PROFILE, domain="VEHICLE", armor_class="MEDIUM"))
        projection = self.projection(engine)
        result = self.query(engine, query, projection)
        self.assertEqual(result["status"], "RESOLVED", result)
        self.assertTrue(result["can_execute"])
        self.assertEqual(result["geometry"]["distance"], 5)
        self.assertEqual(result["geometry"]["visibility_under_assumptions"]["distance"], 2)
        self.assertAlmostEqual(result["hit_probability"], sum(
            row["probability"] for row in result["outcome_distribution"] if not row["no_effect"]
        ))
        changed = list(projection)
        changed[2] = []
        unavailable = self.query(engine, query, changed)
        self.assertEqual(unavailable["reason_code"], "GUIDED_CAPABILITY_NOT_AVAILABLE")
        self.assertFalse(unavailable["can_execute"])
        with self.assertRaises(FireEffectQueryError) as caught:
            self.query(engine, replace(query, guider_id=2), projection)
        self.assertEqual(caught.exception.code, "GUIDER_NOT_CONTROLLED")

    def test_oversized_json_integers_raise_typed_errors(self):
        for field in ("actor_id", "weapon_id", "target_hex", "blood"):
            values = dict(actor_id=1, weapon_id=36, target_hex=102,
                          target_model="source-1-2", target_profile=dict(PROFILE))
            if field == "blood":
                values["target_profile"][field] = 10 ** 400
            else:
                values[field] = 10 ** 400
            with self.subTest(field=field), self.assertRaises(FireEffectQueryError):
                FireEffectQuery.from_dict(values)

    def test_out_of_range_attempt_has_exact_zero_effect(self):
        engine = self.engine()
        action = EngineAction("red", 1, ActionType.SHOOT, target_id=2, parameters={"weapon_id": 36})
        request = engine._direct_fire_request(action, engine._world.operators[1], engine._world.operators[2])
        request = replace(request, weapon=replace(request.weapon, maximum_range=1))
        result = direct_fire_probabilities(engine._adjudication_tables, request)
        self.assertEqual(result["damage_probability"], 0)
        self.assertEqual(result["expected_damage"], 0)
        self.assertEqual(result["damage_distribution"], [{"losses": 0, "probability": 1.0, "probability_fraction": "1/1"}])


if __name__ == "__main__":
    unittest.main()
