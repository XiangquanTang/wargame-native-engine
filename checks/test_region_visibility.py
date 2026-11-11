"""Region queries reuse observation rules without consulting live occupancy."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wargame_native_engine.compat.source_frame.models import UnitDomain
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.empty_fire import (
    EMPTY_FIRE_EXPOSURE_FRAMES, advance_empty_fire_exposure,
    has_empty_fire_exposure, mark_empty_fire_exposure,
)
from wargame_native_engine.core.region_visibility import (
    MAX_REGION_CELLS,
    RegionRectangle,
    RegionVisibilityQuery,
    RegionVisibilityQueryError,
    evaluate_region_visibility,
    region_visibility_query_schema,
)
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.visibility import (
    HexCell, HexMapProfile, LineOfSightEvidence, ObservationProfiles, hex_distance,
)

ROOT = Path(__file__).resolve().parents[1]


def query_record(*, target="VEHICLE", concealed=False, height=0,
                 x=1, y=1, width=6, rows=1, assertion=None, exposure=None):
    record = {
        "region": {"x_min": x, "y_min": y, "width": width, "height": rows},
        "target_profile": {
            "target_class": target, "concealed": concealed, "height_m": height,
        },
    }
    if assertion is not None:
        record["actor_assertion"] = assertion
    if exposure is not None:
        record["target_profile"]["empty_fire_exposure_frames"] = exposure
    return record


def engine_fixture(*, domain="VEHICLE", subtype="fixture-vehicle", altitude=0):
    record = json.loads((ROOT / "examples/source_movement_scenario.json").read_text())
    for cell in record["map"]["hexes"]:
        cell.update(elev=0, cond=0)
    record["operators"][0].update(
        operator_type=domain, subtype=subtype, altitude=altitude,
    )
    engine = NativeEngine(scenario_from_dict(record))
    engine.reset(seed=17)
    observation = engine.observe("red").to_dict()
    # Source current_hex is part of the same own-side projection in production.
    source = {"operators": [{"obj_id": 1, "color": 0, "cur_hex": 101}]}
    return engine, observation, source


def rows_by_hex(result):
    return {row[0]: dict(zip(result["cell_fields"], row)) for row in result["cells"]}


class QueryAssertions:
    def assert_rejected(self, code, callback, *args, **kwargs):
        with self.assertRaises(RegionVisibilityQueryError) as raised:
            callback(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)


class RegionVisibilityContractTests(QueryAssertions, unittest.TestCase):
    def test_rectangle_is_spatial_not_pagination(self):
        rectangle = RegionRectangle(3, 4, 2, 2)
        self.assertEqual(rectangle.hex_ids(), (403, 404, 503, 504))
        self.assertEqual(len(RegionRectangle(0, 0, 50, 30).hex_ids()), 1500)
        self.assertEqual(len(RegionRectangle(0, 0, 64, 64).hex_ids()), MAX_REGION_CELLS)

    def test_bounds_types_and_unknown_fields_fail_before_evaluation(self):
        for changes in (
            {"width": True}, {"height": 0}, {"x_min": -1},
            {"x_min": 99, "width": 2}, {"width": 100, "height": 42},
            {"width": 10**100}, {"offset": 1},
        ):
            record = query_record()
            record["region"].update(changes)
            with self.subTest(changes=changes):
                self.assert_rejected("invalid_arguments", RegionVisibilityQuery.from_dict, record)
        for profile in (
            {"target_class": "UNKNOWN"}, {"concealed": 1}, {"height_m": True},
            {"height_m": 20}, {"target_class": "DRONE", "height_m": 500},
            {"target_class": "HELICOPTER", "height_m": 0},
            {"target_id": 2},
            {"empty_fire_exposure_frames": -1},
            {"empty_fire_exposure_frames": EMPTY_FIRE_EXPOSURE_FRAMES + 1},
            {"empty_fire_exposure_frames": True},
            {"empty_fire_exposure_frames": 1.0},
        ):
            record = query_record()
            record["target_profile"].update(profile)
            with self.subTest(profile=profile):
                self.assert_rejected("invalid_arguments", RegionVisibilityQuery.from_dict, record)
        self.assert_rejected(
            "invalid_arguments", RegionVisibilityQuery.from_dict,
            {**query_record(), "side": "blue"},
        )

    def test_schema_is_fresh_and_describes_required_inputs(self):
        first = region_visibility_query_schema()
        self.assertEqual(first["required"], ["region", "target_profile"])
        self.assertFalse(first["additionalProperties"])
        first["properties"]["region"]["properties"]["width"]["maximum"] = 999
        self.assertEqual(
            region_visibility_query_schema()["properties"]["region"]["properties"]["width"]["maximum"], 100,
        )
        target_schema = first["properties"]["target_profile"]
        self.assertNotIn("empty_fire_exposure_frames", target_schema["required"])
        self.assertEqual(
            target_schema["properties"]["empty_fire_exposure_frames"]["maximum"],
            EMPTY_FIRE_EXPOSURE_FRAMES,
        )

    def test_maximum_response_stays_below_default_tool_limit(self):
        query = RegionVisibilityQuery.from_dict(query_record(x=0, y=0, width=64, rows=64))
        profile = HexMapProfile({h: HexCell(h, 0) for h in query.region.hex_ids()})
        result = evaluate_region_visibility(
            query=query, actor={"current_hex": 0}, observer_domain=UnitDomain.VEHICLE,
            observer_is_drone=False, profile=profile,
            observation_profiles=ObservationProfiles(), terrain_at=lambda _: "OPEN",
            line_of_sight=lambda h: LineOfSightEvidence(True, hex_distance(0, h), (), (), ()),
            map_id="bounded-map", los_authority="test",
        ).to_dict()
        self.assertEqual(result["cell_count"], MAX_REGION_CELLS)
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(encoded.encode()), 512 * 1024)


class RegionVisibilityEngineTests(QueryAssertions, unittest.TestCase):
    def setUp(self):
        self.engine, self.observation, self.source = engine_fixture()

    def query(self, record=None, *, actor_id=1):
        return self.engine.query_region_visibility_from_projection(
            "red", actor_id, self.observation, self.source,
            RegionVisibilityQuery.from_dict(record or query_record()),
        ).to_dict()

    def test_imported_color_evidence_is_required_not_owner_metadata(self):
        expected = self.query()
        for owner in (None, 17, 'blue', 'red'):
            self.source['operators'][0]['owner'] = owner
            self.assertEqual(self.query(), expected)
        for color in (None, True, False, 0.0, '0', 1, -1, 2):
            self.source['operators'][0].update(color=color, owner='red')
            with self.subTest(color=color):
                self.assert_rejected('actor_not_owned', self.query)
        self.source['operators'][0].pop('color')
        self.assert_rejected('actor_not_owned', self.query)

    def test_own_actor_assertions_cannot_override_state(self):
        expected = self.query(query_record(assertion={
            "current_hex": 101, "operator_type": "VEHICLE", "subtype": "fixture-vehicle",
        }))
        self.assertEqual(expected["actor"]["current_hex"], 101)
        for assertion in ({"current_hex": 102}, {"subtype": "source-3-5"},
                          {"operator_type": "AIR"}, {"subtype": None}):
            self.assert_rejected("actor_assertion_mismatch", self.query,
                                 query_record(assertion=assertion))
        self.assert_rejected("actor_not_owned", self.query, actor_id=2)

    def test_query_does_not_read_live_world_or_rng_or_enemy_records(self):
        before = deepcopy(self.engine.snapshot())
        expected = self.query()
        self.observation["visible_enemies"] = [{"secret": "do not inspect me"}]
        self.source["operators"].append({"obj_id": 999, "secret": "do not inspect me"})
        with patch.object(self.engine, "_require_world", side_effect=AssertionError("live read")):
            actual = self.query()
        self.assertEqual(actual, expected)
        self.assertEqual(self.engine.snapshot(), before)
        self.assertNotIn("secret", json.dumps(actual))
        self.assertEqual(actual["semantics"], "hypothetical_target_visibility")

    def test_frozen_projection_wins_over_working_world_and_results_are_detached(self):
        expected = self.query()
        self.engine._world.operators[1].current_hex = 106
        self.engine._world.operators[1].subtype = "changed-live-model"
        self.engine._world.operators[2].current_hex = 101
        self.assertEqual(self.query(), expected)
        expected["actor"]["current_hex"] = 999
        expected["cells"][0][1] = False
        fresh = self.query()
        self.assertEqual(fresh["actor"]["current_hex"], 101)
        self.assertTrue(fresh["cells"][0][1])

    def test_inactive_observers_and_off_map_cells_are_rejected(self):
        self.assert_rejected("region_out_of_map", self.query, query_record(width=7))
        for field in ("alive", "deployed"):
            original = self.observation["friendly_operators"][0][field]
            self.observation["friendly_operators"][0][field] = False
            self.assert_rejected("actor_unavailable", self.query)
            self.observation["friendly_operators"][0][field] = original
        with patch("wargame_native_engine.core.engine.evaluate_line_of_sight") as los:
            self.assert_rejected("region_out_of_map", self.query, query_record(y=10**50))
            los.assert_not_called()

    def test_air_target_classes_have_distinct_coverage(self):
        helicopter = self.query(query_record(target="HELICOPTER", height=20))
        drone = self.query(query_record(target="DRONE", height=200))
        self.assertEqual(helicopter["coverage"], "all")
        self.assertEqual(drone["coverage"], "some")
        self.assertEqual(drone["visible_count"], 2)
        self.assertFalse(drone["all_visible"])
        self.assertTrue(drone["any_visible"])
        far_drone = self.query(query_record(target="DRONE", height=200, x=4, width=3))
        self.assertEqual(far_drone["coverage"], "none")
        self.assertEqual(far_drone["coverage_fraction"], 0)

    def test_drone_observer_uses_canonical_short_range_and_no_air_observation(self):
        self.engine, self.observation, self.source = engine_fixture(
            domain="AIR", subtype="source-3-5", altitude=200,
        )
        vehicle = self.query(query_record(concealed=True))
        self.assertEqual(vehicle["visible_count"], 3)
        self.assertEqual(rows_by_hex(vehicle)[103]["observation_limit"], 2)
        aircraft = self.query(query_record(target="HELICOPTER", height=20))
        self.assertEqual(aircraft["coverage"], "none")
        self.assertEqual(aircraft["cells"][0][-1], "target_category_not_observable")

    def test_omitted_and_zero_exposure_preserve_ordinary_rules(self):
        ordinary = self.query(query_record(target="DRONE", height=200))
        explicit_zero = self.query(query_record(target="DRONE", height=200, exposure=0))
        self.assertEqual(ordinary, explicit_zero)
        self.assertEqual(ordinary["target_profile"]["empty_fire_exposure_frames"], 0)
        self.assertEqual(ordinary["visible_count"], 2)

    def test_positive_exposure_bypasses_los_including_unknown_height_units(self):
        self.engine._source_visibility_map = HexMapProfile(
            self.engine._source_visibility_map.cells, elevation_kind=None,
        )
        with patch("wargame_native_engine.core.engine.evaluate_line_of_sight",
                   side_effect=AssertionError("exposure must not evaluate LOS")) as los:
            result = self.query(query_record(
                target="DRONE", height=200, x=6, width=1, exposure=1,
            ))
            los.assert_not_called()
        row = result["cells"][0]
        self.assertEqual(result["coverage"], "all")
        self.assertEqual(row, [106, True, None, 5, None, "empty_fire_exposure"])
        self.assertEqual(result["los_authority"], "not_evaluated_empty_fire_exposure")
        self.assertEqual(result["target_assumptions"], {"alive": True, "deployed": True})
        self.observation["friendly_operators"][0]["deployed"] = False
        self.assert_rejected("actor_unavailable", self.query, query_record(exposure=10))

    def test_drone_to_air_exposure_matches_the_core_ten_frame_extension(self):
        self.engine, self.observation, self.source = engine_fixture(
            domain="AIR", subtype="source-3-5", altitude=200,
        )
        world = self.engine._world
        target = world.operators[2]
        target.operator_type = "AIR"
        target.subtype = "fixture-helicopter"
        target.altitude = 20
        self.engine._refresh_source_observation()
        self.assertFalse(self.engine.observe("red").visible_enemies)
        mark_empty_fire_exposure(target)
        for remaining in range(EMPTY_FIRE_EXPOSURE_FRAMES, 0, -1):
            self.assertTrue(has_empty_fire_exposure(target))
            self.engine._refresh_source_observation()
            self.assertEqual(
                [value.operator_id for value in self.engine.observe("red").visible_enemies], [2],
            )
            before = deepcopy(self.engine.snapshot())
            exposed = self.query(query_record(
                target="HELICOPTER", height=20, x=6, width=1, exposure=remaining,
            ))
            self.assertEqual(exposed["coverage"], "all")
            self.assertEqual(exposed["cells"][0][-1], "empty_fire_exposure")
            self.assertEqual(self.engine.snapshot(), before)
            advance_empty_fire_exposure(world)
        self.assertFalse(has_empty_fire_exposure(target))
        self.engine._refresh_source_observation()
        self.assertFalse(self.engine.observe("red").visible_enemies)
        expired = self.query(query_record(
            target="HELICOPTER", height=20, x=6, width=1, exposure=0,
        ))
        self.assertEqual(expired["coverage"], "none")
        self.assertEqual(expired["cells"][0][-1], "target_category_not_observable")

    def test_static_los_receives_both_real_and_hypothetical_height_categories(self):
        self.engine, self.observation, self.source = engine_fixture(
            domain="AIR", subtype="fixture-helicopter", altitude=500,
        )
        lookup = Mock(return_value=LineOfSightEvidence(False, 2, (), (), ()))
        self.engine.map_runtime_data = SimpleNamespace(visibility=SimpleNamespace(line_of_sight=lookup))
        result = self.query(query_record(target="DRONE", height=200, x=3, width=1))
        lookup.assert_called_once_with(101, 103, observer_height_m=500, target_height_m=200)
        self.assertEqual(result["los_authority"], "source_static_visibility_lookup")
        self.assertEqual(result["cells"][0][-1], "line_of_sight_blocked")
        self.assertFalse(result["cells"][0][2])

    def test_limits_and_target_terrain_match_the_shared_evaluator(self):
        result = self.query(query_record(target="PERSONNEL", concealed=True))
        self.assertEqual(rows_by_hex(result)[106]["observation_limit"], 5)
        self.assertTrue(rows_by_hex(result)[106]["observable"])
        # Isolate the endpoint terrain modifier from intermediate LOS blockers.
        original = self.engine._source_visibility_map
        cells = dict(original.cells)
        cells[106] = HexCell(106, 0, "FOREST")
        self.engine._source_visibility_map = HexMapProfile(cells)
        with patch.object(type(self.engine._source_map), "terrain", return_value=SimpleNamespace(value="FOREST")):
            forest = self.query(query_record(target="PERSONNEL", concealed=True, x=6, width=1))
        self.assertEqual(forest["cells"][0][4], 2.5)
        self.assertEqual(forest["cells"][0][-1], "outside_observation_limit")


if __name__ == "__main__":
    unittest.main()
