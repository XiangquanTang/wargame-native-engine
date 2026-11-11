"""Air concealment exemptions and unit-safe Native fallback LOS.

Source rules 10b/11b/12b exempt concealed vehicles, not personnel or terrain.
The Native LOS height bonuses are elevation levels, never unscaled meters.
"""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from wargame_native_engine.compat.source_frame.models import UnitDomain
from wargame_native_engine.cli.resolve_direct_fire import main as direct_fire_main
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.data.map_runtime import (
    MapRuntimeData,
    MovementCostLookup,
    StaticVisibilityLookup,
)
from wargame_native_engine.io.scenario import (
    native_map_identity_hash,
    scenario_from_dict,
)
from wargame_native_engine.visibility import (
    HexCell,
    HexMapProfile,
    evaluate_line_of_sight,
    evaluate_visibility,
)


ROOT = Path(__file__).resolve().parents[1]


def profile(heights, *, terrain=None, kind="LEVEL", unit=10):
    return HexMapProfile(
        {
            101 + index: HexCell(
                101 + index, height, (terrain or {}).get(101 + index, "OPEN")
            )
            for index, height in enumerate(heights)
        },
        elevation_kind=kind,
        elevation_unit_m=unit,
    )


def scenario_record(*, target_hex=104, target_domain="VEHICLE"):
    """Keep production scenario defaults; vary only observation inputs."""

    record = json.loads(
        (ROOT / "examples/source_movement_scenario.json").read_text("utf-8")
    )
    for cell in record["map"]["hexes"]:
        cell.update(elev=0, cond=0)
    target = record["operators"][1]
    target.update(operator_type=target_domain, current_hex=target_hex,
                  x=target_hex % 100, y=target_hex // 100)
    record["deployment"]["positions"]["blue"]["2"] = [
        target_hex % 100, target_hex // 100,
    ]
    return record


class ConcealmentObservationTests(unittest.TestCase):
    def visibility(self, *, observer=UnitDomain.AIR, drone=False,
                   target=UnitDomain.VEHICLE, terrain="OPEN", heights=(0, 0, 0)):
        return evaluate_visibility(
            profile(heights, terrain={103: terrain}),
            observer_hex=101,
            target_hex=103,
            observer_domain=observer,
            target_domain=target,
            observer_is_drone=drone,
            target_concealed=True,
        )

    def test_air_vehicle_exemption_does_not_depend_on_ground_height(self):
        for drone, limit in ((True, 2), (False, 25)):
            for heights in ((0, 0, 0), (0, 0, 2), (2, 0, 0)):
                with self.subTest(drone=drone, heights=heights):
                    result = self.visibility(drone=drone, heights=heights)
                    self.assertTrue(result.observable)
                    self.assertEqual(result.observation_limit, limit)
                    self.assertIn(
                        "vehicle_concealment_ignored_from_air_observer",
                        {item["rule"] for item in result.modifiers},
                    )

    def test_air_personnel_concealment_still_halves_distance(self):
        for drone, limit in ((True, 1), (False, 5)):
            with self.subTest(drone=drone):
                result = self.visibility(drone=drone, target=UnitDomain.PERSONNEL)
                self.assertEqual(result.observation_limit, limit)
                self.assertEqual(result.observable, not drone)

    def test_air_vehicle_exemption_does_not_remove_target_terrain_penalty(self):
        for terrain in ("FOREST", "SETTLEMENT"):
            for drone, limit in ((True, 1), (False, 12.5)):
                with self.subTest(terrain=terrain, drone=drone):
                    result = self.visibility(drone=drone, terrain=terrain)
                    self.assertEqual(result.observation_limit, limit)
                    self.assertEqual(result.observable, not drone)

    def test_personnel_concealment_and_terrain_penalties_stack(self):
        for drone, limit in ((True, 0.5), (False, 2.5)):
            result = self.visibility(
                drone=drone, target=UnitDomain.PERSONNEL, terrain="FOREST"
            )
            self.assertEqual(result.observation_limit, limit)

    def test_ground_vehicle_exemption_still_requires_higher_observer(self):
        for heights, limit in (((0, 0, 0), 12.5), ((2, 0, 0), 25)):
            result = self.visibility(observer=UnitDomain.VEHICLE, heights=heights)
            self.assertEqual(result.observation_limit, limit)

    def test_engine_two_hex_drone_and_loitering_see_concealed_vehicle(self):
        for subtype in ("source-3-5", "source-3-7"):
            with self.subTest(subtype=subtype):
                record = scenario_record(target_hex=103)
                record["operators"][0].update(operator_type="AIR", subtype=subtype)
                record["operators"][1]["concealed"] = True
                engine = NativeEngine(scenario_from_dict(record))
                self.addCleanup(engine.close)
                world = engine.reset()
                self.assertEqual(
                    world.operators[1].raw_attributes["see_enemy_bop_ids"], [2]
                )

    def test_engine_two_hex_drone_does_not_see_concealed_personnel(self):
        record = scenario_record(target_hex=103, target_domain="PERSONNEL")
        record["operators"][0].update(operator_type="AIR", subtype="source-3-5")
        record["operators"][1]["concealed"] = True
        engine = NativeEngine(scenario_from_dict(record))
        self.addCleanup(engine.close)
        self.assertEqual(
            engine.reset().operators[1].raw_attributes["see_enemy_bop_ids"], []
        )

    def test_engine_helicopter_retains_full_vehicle_limit(self):
        record = scenario_record(target_hex=114)
        # Thirteen hexes is beyond the incorrectly halved 12.5-hex limit.
        cells = record["map"]["hexes"]
        for hex_id in range(107, 115):
            cells[-1]["neighbors"][0] = hex_id
            cells.append({
                "pos": hex_id, "elev": 0, "cond": 0,
                "neighbors": [-1, -1, -1, hex_id - 1, -1, -1],
                "roads": [0] * 6, "rivers": [0] * 6,
            })
        record["operators"][0].update(operator_type="AIR", subtype="armed-helicopter")
        record["operators"][1]["concealed"] = True
        engine = NativeEngine(scenario_from_dict(record))
        self.addCleanup(engine.close)
        world = engine.reset()
        result = evaluate_visibility(
            engine._source_visibility_map,
            observer_hex=world.operators[1].current_hex,
            target_hex=world.operators[2].current_hex,
            observer_domain=UnitDomain.AIR,
            target_domain=UnitDomain.VEHICLE,
            target_concealed=world.operators[2].concealed,
        )
        self.assertEqual(result.observation_limit, 25)
        self.assertEqual(result.distance, 13)
        self.assertIn(2, world.operators[1].raw_attributes["see_enemy_bop_ids"])


class ElevationUnitObservationTests(unittest.TestCase):
    def test_meter_and_level_profiles_have_identical_los_evidence(self):
        for terrain in ({}, {102: "FOREST"}, {102: "SETTLEMENT"}):
            with self.subTest(terrain=terrain):
                levels = profile([2, 1, 1, 0], terrain=terrain)
                meters = profile([20, 10, 10, 0], terrain=terrain, kind="METERS")
                for start, end in ((101, 104), (104, 101)):
                    expected = evaluate_line_of_sight(levels, start, end)
                    actual = evaluate_line_of_sight(meters, start, end)
                    self.assertTrue(expected.visible)
                    self.assertEqual(actual, expected)

    def test_meter_terrain_bonus_is_a_whole_grade_not_one_meter(self):
        for kind, heights in (("LEVEL", [2, 1, 2, 0]),
                              ("METERS", [20, 10, 20, 0])):
            result = evaluate_line_of_sight(
                profile(heights, terrain={103: "FOREST"}, kind=kind), 101, 104
            )
            self.assertFalse(result.visible)
            self.assertEqual(result.blocking_hexes, (103,))
            self.assertEqual(result.samples[1]["modified_elevation"], 3)

    def test_legacy_direct_profiles_remain_level_based(self):
        legacy = HexMapProfile({101: HexCell(101, 2), 102: HexCell(102, 1),
                                103: HexCell(103, 1), 104: HexCell(104, 0)})
        self.assertEqual(evaluate_line_of_sight(legacy, 101, 104),
                         evaluate_line_of_sight(profile([2, 1, 1, 0]), 101, 104))

    def test_unknown_or_invalid_units_fail_explicitly(self):
        for kind, unit in ((None, 10), ("UNKNOWN", 10), ("METERS", None),
                           ("METERS", 0), ("METERS", -1), ("METERS", True),
                           ("METERS", float("nan")), ("METERS", float("inf"))):
            with self.subTest(kind=kind, unit=unit):
                with self.assertRaisesRegex(ValueError, "fallback LOS"):
                    evaluate_line_of_sight(
                        profile([0, 0, 0], kind=kind, unit=unit), 101, 103
                    )

    def test_same_and_adjacent_hexes_do_not_require_height_units(self):
        for kind, unit in ((None, None), ("METERS", None)):
            for target_hex, distance in ((101, 0), (102, 1)):
                with self.subTest(kind=kind, target_hex=target_hex):
                    result = evaluate_line_of_sight(
                        profile([0, 0], kind=kind, unit=unit), 101, target_hex
                    )
                    self.assertTrue(result.visible)
                    self.assertEqual(result.distance, distance)
                    self.assertEqual(result.modifiers, (
                        {"rule": "adjacent_hex_always_visible"},
                    ))

    def make_engine(self, *, kind, cached=False, altitude=None, unit=10,
                    cache_visible=True):
        record = scenario_record()
        record["map"].update(elevation_kind=kind, elevation_unit_m=unit)
        scale = 10 if kind == "METERS" else 1
        for cell, height in zip(record["map"]["hexes"], (2, 1, 1, 0, 0, 0)):
            cell["elev"] = height * scale
        record["map"]["hexes"][1]["cond"] = "FOREST"
        if altitude is not None:
            record["operators"][0].update(operator_type="AIR", subtype="helicopter",
                                           altitude=altitude)
        scenario = scenario_from_dict(record)
        runtime = (
            self.runtime_fixture(scenario.map, visible=cache_visible)
            if cached else None
        )
        engine = NativeEngine(scenario, map_runtime_data=runtime)
        self.addCleanup(engine.close)
        return engine

    def runtime_fixture(self, native_map, *, visible=True):
        """Small deterministic payload using the production static lookup."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "visibility.bin"
        path.write_bytes(bytes([int(visible)]) * (10 * 2 * 7 * 2 * 7))
        identity = dict(map_id=native_map.map_id, rows=2, columns=7,
                        source_sha256="1" * 64, payload_sha256="2" * 64)
        visibility = StaticVisibilityLookup(
            **identity, path=path, shape=(10, 2, 7, 2, 7), data_offset=0
        )
        self.addCleanup(visibility.close)
        return MapRuntimeData(
            map_id=native_map.map_id,
            map_sha256=native_map_identity_hash(native_map),
            manifest_sha256="3" * 64,
            movement=MovementCostLookup(**identity, factors={}),
            visibility=visibility,
        )

    def test_engine_fallback_propagates_declared_units(self):
        for kind in ("LEVEL", "METERS"):
            with self.subTest(kind=kind):
                engine = self.make_engine(kind=kind)
                world = engine.reset()
                self.assertIn(2, world.operators[1].raw_attributes["see_enemy_bop_ids"])
                self.assertEqual(engine._source_visibility_map.elevation_kind, kind)
                self.assertTrue(evaluate_line_of_sight(
                    engine._source_visibility_map, 101, 104
                ).visible)

    def test_cached_los_does_not_require_or_reinterpret_grade_units(self):
        engine = self.make_engine(kind="METERS", cached=True, unit=None)
        world = engine.reset()
        result = engine._runtime_line_of_sight(world.operators[1], world.operators[2])
        self.assertTrue(result.visible)
        self.assertEqual(result.modifiers[0]["rule"], "source_static_visibility_lookup")
        self.assertIn(2, world.operators[1].raw_attributes["see_enemy_bop_ids"])
        with self.assertRaisesRegex(ValueError, "elevation_unit_m"):
            evaluate_line_of_sight(engine._source_visibility_map, 101, 104)

    def test_cached_blocked_los_stays_authoritative_over_clear_fallback(self):
        engine = self.make_engine(kind="METERS", cached=True, cache_visible=False)
        world = engine.reset()
        self.assertTrue(evaluate_line_of_sight(
            engine._source_visibility_map, 101, 104
        ).visible)
        self.assertFalse(engine._runtime_line_of_sight(
            world.operators[1], world.operators[2]
        ).visible)
        self.assertNotIn(2, world.operators[1].raw_attributes["see_enemy_bop_ids"])

    def test_unsupported_air_height_falls_through_to_unit_safe_native_los(self):
        for kind in ("LEVEL", "METERS"):
            with self.subTest(kind=kind):
                engine = self.make_engine(kind=kind, cached=True, altitude=80)
                world = engine.reset()
                self.assertIsNone(engine._runtime_line_of_sight(
                    world.operators[1], world.operators[2]
                ))
                self.assertIn(2, world.operators[1].raw_attributes["see_enemy_bop_ids"])

    def test_fallback_does_not_guess_unknown_meter_grade(self):
        engine = self.make_engine(kind="METERS", cached=True, altitude=80, unit=None)
        with self.assertRaisesRegex(ValueError, "elevation_unit_m"):
            engine.reset()

    def test_direct_fire_cli_propagates_declared_units(self):
        source = scenario_record(target_domain="PERSONNEL")
        for operator in source["operators"]:
            operator["raw_attributes"]["hex_id"] = operator["current_hex"]
        weapon = source["metadata"]["source_compatible"]["weapons"]["36"]
        record = {
            "schema_version": "native-direct-fire-scenario/1.0",
            "scenario_id": "observation-unit-cli-regression",
            "operators": source["operators"],
            "weapons": [dict(weapon, weapon_id=36)],
        }
        reports = []
        for kind, heights in (("LEVEL", [2, 1, 1, 0]),
                              ("METERS", [20, 10, 10, 0])):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                record["map"] = {
                    "elevation_kind": kind, "elevation_unit_m": 10,
                    "cells": [
                        {"hex_id": 101 + index, "elevation": height,
                         "terrain": "FOREST" if index == 1 else "OPEN"}
                        for index, height in enumerate(heights)
                    ],
                }
                scenario = Path(temporary) / "scenario.json"
                scenario.write_text(json.dumps(record), encoding="utf-8")
                output = io.StringIO()
                with redirect_stdout(output):
                    status = direct_fire_main([
                        "--scenario", str(scenario), "--attacker", "1",
                        "--target", "2", "--weapon", "36",
                        "--output", str(Path(temporary) / "result"),
                    ])
                self.assertEqual(status, 0, output.getvalue())
                reports.append(json.loads(output.getvalue())["visibility"])
        self.assertTrue(reports[0]["observable"])
        self.assertEqual(reports[0], reports[1])


if __name__ == "__main__":
    unittest.main()
