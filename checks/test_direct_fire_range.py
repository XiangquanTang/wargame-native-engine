"""DIRECT overshoots spend ammunition/cooldown, never combat effects.

Use the public six-cell scenario and production tables/runtime.  Only terrain
and weapon range are adjusted to isolate range from line-of-sight failures.
"""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from wargame_native_engine.adjudication import (
    AdjudicationTableSet,
    CombatOutcome,
    DiceRoller,
    DirectFireAdjudicator,
    DirectFireResourcePolicy,
    FixedDiceRoller,
)
from wargame_native_engine.adjudication.replay import (
    direct_fire_request_from_dict,
    direct_fire_request_to_dict,
    replay_direct_fire,
    write_direct_fire_replay,
)
from wargame_native_engine.agents.realtime import (
    CommandStatus,
    ManualRealtimeClock,
    RealtimeAgentSession,
)
from wargame_native_engine.agents.seat import SeatOperation
from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.core.fire_intent import FireMode
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.rng import NativeRNG
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.scenario import load_scenario, scenario_from_dict
from wargame_native_engine.operations import ScenarioMatchFactory
from wargame_native_engine.version import ADJUDICATION_SEMANTICS_VERSION

ROOT = Path(__file__).resolve().parents[1]
TABLES = ROOT / (
    "config/adjudication/source_tables_v1/native_adjudication_tables_final.json"
)


def fixture_record(*, grouped=False):
    record = json.loads(
        (ROOT / "examples/source_movement_scenario.json").read_text("utf-8")
    )
    for cell in record["map"]["hexes"]:
        cell.update(elev=0, cond=0)
    record["metadata"]["source_compatible"]["weapons"]["36"][
        "maximum_range"
    ] = 4
    if grouped:
        near = copy.deepcopy(record["operators"][0])
        near.update(operator_id=3, current_hex=104, x=4)
        record["operators"].append(near)
        record["deployment"]["positions"]["red"]["3"] = [4, 1]
    return record


def shoot(actor_id=1, target_id=2, *, side="red", weapon_id=36):
    return EngineAction(
        side, actor_id, ActionType.SHOOT,
        target_id=target_id, parameters={"weapon_id": weapon_id},
    )


def event_types(result):
    return {event.event_type for event in result.events}


class DirectFireAdjudicatorRangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = AdjudicationTableSet(TABLES)

    def request(self):
        engine = NativeEngine(scenario_from_dict(fixture_record()))
        world = engine.reset(seed=42)
        self.addCleanup(engine.close)
        return engine._direct_fire_request(
            shoot(), world.operators[1], world.operators[2], world=world,
        )

    def test_overshoot_stage_and_resolve_have_no_effect_or_rng(self):
        for method in ("stage", "resolve"):
            with self.subTest(method=method):
                request = self.request()
                # Health need not be an exact blood/max_blood normalization.
                request.target.health = 71.25
                before = request.target.to_dict()
                rng = NativeRNG(42)
                rng_before = rng.snapshot()
                result = getattr(
                    DirectFireAdjudicator(self.tables, DiceRoller(rng)), method
                )(request)
                self.assertTrue(result.legality.legal)
                self.assertIsNone(result.error)
                self.assertEqual(result.outcome, CombatOutcome(no_effect=True))
                self.assertEqual(request.target.to_dict(), before)
                self.assertEqual(rng.snapshot(), rng_before)
                self.assertEqual(result.ammunition_after, 19)
                self.assertEqual(
                    result.cooldown_after,
                    self.tables.timing("direct_fire_cooldown_seconds"),
                )
                self.assertFalse(
                    result.legality.evidence["checks"]["within_weapon_range"]
                )
                self.assertEqual(
                    result.legality.evidence["range_effect"],
                    "OUT_OF_RANGE_NO_EFFECT",
                )
                self.assertTrue({
                    EventType.DIRECT_FIRE_VALIDATED,
                    EventType.AMMUNITION_CONSUMED,
                    EventType.WEAPON_COOLDOWN_STARTED,
                } <= event_types(result))
                self.assertFalse({
                    EventType.DICE_ROLLED,
                    EventType.ATTACK_LEVEL_SELECTED,
                    EventType.LOSSES_APPLIED,
                    EventType.SUPPRESSION_APPLIED,
                    EventType.UNIT_DESTROYED,
                } & event_types(result))

    def test_vehicle_passengers_and_existing_suppression_are_unchanged(self):
        request = self.request()
        request.target.operator_type = UnitDomain.VEHICLE.value
        request.target.state = "SUPPRESSED"
        request.target.suppression_remaining_ms = 12345
        request.target.raw_attributes["suppression_remaining_seconds"] = 13
        passenger = OperatorState.from_dict(request.target.to_dict())
        passenger.operator_id = 9
        passenger.operator_type = UnitDomain.PERSONNEL.value
        request = replace(
            request,
            target_domain=UnitDomain.VEHICLE,
            target_armor=ArmorClass.MEDIUM,
            carried_passengers=(passenger,),
        )
        before = (request.target.to_dict(), passenger.to_dict())
        result = DirectFireAdjudicator(
            self.tables, FixedDiceRoller([])
        ).resolve(request)
        self.assertEqual(result.outcome, CombatOutcome(no_effect=True))
        self.assertEqual((request.target.to_dict(), passenger.to_dict()), before)

    def test_custom_weapon_cooldown_and_last_round_are_spent(self):
        request = self.request()
        request = replace(
            request, ammunition_before=1,
            weapon=replace(request.weapon, cooldown_ticks=9),
        )
        result = DirectFireAdjudicator(
            self.tables, FixedDiceRoller([])
        ).resolve(request)
        self.assertEqual((result.ammunition_after, result.cooldown_after), (0, 9))

    def test_boundary_and_in_range_keep_normal_table_resolution(self):
        for distance in (3, 4):
            with self.subTest(distance=distance):
                request = self.request()
                request = replace(
                    request, distance=distance,
                    visibility=replace(request.visibility, distance=distance),
                )
                before = request.target.to_dict()
                staged = DirectFireAdjudicator(
                    self.tables, FixedDiceRoller([6] * 8)
                ).stage(request)
                self.assertTrue(staged.legality.legal)
                self.assertIsNone(staged.error)
                self.assertEqual(
                    staged.legality.evidence["range_effect"], "NORMAL"
                )
                self.assertIn(EventType.ATTACK_LEVEL_SELECTED, event_types(staged))
                self.assertIn(EventType.DICE_ROLLED, event_types(staged))
                self.assertTrue(staged.outcome.suppression_applied)
                self.assertFalse(staged.outcome.no_effect)
                self.assertEqual(request.target.to_dict(), before)
                resolved = DirectFireAdjudicator(
                    self.tables, FixedDiceRoller([6] * 8)
                ).resolve(request)
                self.assertEqual(resolved.outcome, staged.outcome)
                self.assertEqual(request.target.state, "SUPPRESSED")
                self.assertEqual(
                    (resolved.ammunition_after, resolved.cooldown_after),
                    (staged.ammunition_after, staged.cooldown_after),
                )

    def test_other_legality_failures_still_reject_without_consumption(self):
        cases = [
            ("ammunition", {"ammunition_before": 0}, "AMMUNITION_UNAVAILABLE"),
            ("cooldown", {"weapon_cooldown_before": 3}, "WEAPON_ON_COOLDOWN"),
            ("folded", {"weapon_unfolded": False}, "WEAPON_NOT_UNFOLDED"),
            ("transition", {"attacker_transitioning": True},
             "ATTACKER_TRANSITIONING"),
            ("moving", {"attacker_moving": True}, "ATTACKER_MUST_STOP"),
            ("close_combat", {"target_external_direct_fire_prohibited": True},
             "TARGET_IN_CLOSE_COMBAT"),
        ]
        for name, updates, code in cases:
            with self.subTest(rule=name):
                self.assert_rejected(replace(self.request(), **updates), code)
        request = self.request()
        self.assert_rejected(
            replace(request, visibility=replace(
                request.visibility, observable=False, line_of_sight=False,
            )),
            "TARGET_NOT_OBSERVED",
        )
        request = self.request()
        self.assert_rejected(
            replace(request, weapon=replace(
                request.weapon, target_domains=frozenset({UnitDomain.AIR}),
            )),
            "UNSUPPORTED_TARGET_DOMAIN",
        )
        for name, code in (
            ("dead_target", "UNIT_NOT_ALIVE"),
            ("dead_attacker", "UNIT_NOT_ALIVE"),
            ("friendly", "SAME_SIDE_TARGET"),
            ("not_carried", "WEAPON_NOT_CARRIED"),
            ("not_hovering", "AIR_UNIT_MUST_HOVER"),
        ):
            with self.subTest(rule=name):
                request = self.request()
                if name == "dead_target":
                    request.target.alive = False
                elif name == "dead_attacker":
                    request.attacker.alive = False
                elif name == "friendly":
                    request.target.side = request.attacker.side
                elif name == "not_carried":
                    request.attacker.weapon_ids = []
                else:
                    request.attacker.operator_type = UnitDomain.AIR.value
                    request.attacker_hovering = False
                self.assert_rejected(request, code)

    def assert_rejected(self, request, code):
        before = (request.attacker.to_dict(), request.target.to_dict())
        result = DirectFireAdjudicator(
            self.tables, FixedDiceRoller([])
        ).resolve(request)
        self.assertFalse(result.legality.legal)
        self.assertEqual(result.error, code)
        self.assertIsNone(result.outcome)
        self.assertEqual(result.ammunition_after, request.ammunition_before)
        self.assertEqual(result.cooldown_after, request.weapon_cooldown_before)
        self.assertEqual((request.attacker.to_dict(), request.target.to_dict()), before)
        self.assertEqual(event_types(result), {EventType.DIRECT_FIRE_REQUESTED})

    def test_guided_and_self_as_munition_keep_strict_range(self):
        for mode in (FireMode.GUIDED, FireMode.INDIRECT):
            with self.subTest(mode=mode):
                request = replace(self.request(), fire_mode=mode)
                self.assertFalse(request.permits_out_of_range_attempt)
                self.assert_rejected(request, "TARGET_OUT_OF_RANGE")
        request = self.request()
        request = replace(
            request,
            resource_policy=DirectFireResourcePolicy.SELF_AS_MUNITION,
            ammunition_before=None,
            weapon=replace(request.weapon, ammunition_key=None),
        )
        self.assertFalse(request.permits_out_of_range_attempt)
        self.assert_rejected(request, "TARGET_OUT_OF_RANGE")

    def test_request_round_trip_preserves_mode_and_legacy_direct_default(self):
        for mode in FireMode:
            with self.subTest(mode=mode):
                request = replace(self.request(), fire_mode=mode)
                record = direct_fire_request_to_dict(request)
                restored = direct_fire_request_from_dict(record)
                self.assertIs(restored.fire_mode, mode)
                self.assertEqual(
                    restored.permits_out_of_range_attempt, mode is FireMode.DIRECT
                )
        legacy = direct_fire_request_to_dict(self.request())
        legacy.pop("fire_mode", None)
        self.assertIs(
            direct_fire_request_from_dict(legacy).fire_mode, FireMode.DIRECT
        )

    def test_direct_fire_replay_reproduces_overshoot_and_rejects_old_semantics(self):
        request = self.request()
        request.target.health = 71.25
        record = direct_fire_request_to_dict(request)
        result = DirectFireAdjudicator(
            self.tables, DiceRoller(NativeRNG(request.episode_seed))
        ).resolve(request)
        with tempfile.TemporaryDirectory() as directory:
            write_direct_fire_replay(
                directory, request_document=record,
                resolution=result, tables=self.tables,
            )
            self.assertTrue(
                replay_direct_fire(directory, tables=self.tables).matches
            )
            path = Path(directory) / "manifest.json"
            manifest = json.loads(path.read_text("utf-8"))
            self.assertEqual(
                manifest["adjudication_semantics_version"],
                ADJUDICATION_SEMANTICS_VERSION,
            )
            for old_version in (None, "native-joint-prestate/1.2",
                                "native-joint-prestate/1.9", "native-joint-prestate/1.10"):
                with self.subTest(old_version=old_version):
                    if old_version is None:
                        manifest.pop("adjudication_semantics_version", None)
                    else:
                        manifest["adjudication_semantics_version"] = old_version
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaisesRegex(
                        ValueError, "adjudication_semantics_version"
                    ):
                        replay_direct_fire(directory, tables=self.tables)
                    self.assertFalse(replay_direct_fire(
                        directory, tables=self.tables, strict=False,
                    ).matches)


class SourceEngineRangeTests(unittest.TestCase):
    def engine(self, record=None):
        engine = NativeEngine(scenario_from_dict(
            fixture_record() if record is None else record
        ))
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        return engine

    def test_overshoot_commits_resources_but_preserves_target_and_rng(self):
        engine = self.engine()
        before = engine.snapshot()
        result = engine.step([shoot()], delta_time_ms=1)
        after = engine.snapshot()
        self.assertTrue(result.decisions[0].accepted)
        self.assertEqual(after["rng"], before["rng"])
        self.assertEqual(
            after["world"]["operators"]["2"],
            before["world"]["operators"]["2"],
        )
        attacker = after["world"]["operators"]["1"]
        self.assertEqual(attacker["raw_attributes"]["ammunition"]["0"], 19)
        cooldown = AdjudicationTableSet(TABLES).timing("direct_fire_cooldown_seconds")
        self.assertEqual(attacker["weapon_cooldown_remaining_ms"], cooldown * 1000 - 1)
        self.assertFalse({
            EventType.DICE_ROLLED, EventType.LOSSES_APPLIED,
            EventType.SUPPRESSION_APPLIED, EventType.UNIT_DESTROYED,
        } & event_types(result))
        self.assertNotIn(shoot().action_id, {
            action.action_id for action in engine.valid_actions("red")
        })
        again = engine.step([shoot()], delta_time_ms=1)
        self.assertEqual(again.decisions[0].code, "WEAPON_ON_COOLDOWN")
        self.assertEqual(
            engine.snapshot()["world"]["operators"]["1"]["raw_attributes"][
                "ammunition"
            ]["0"], 19,
        )

    def test_overshoot_does_not_reset_suppression_or_damage_passengers(self):
        record = fixture_record()
        target = record["operators"][1]
        target.update(
            operator_type="VEHICLE", state="SUPPRESSED",
            suppression_remaining_ms=12000,
        )
        target["raw_attributes"].update(armor_class="MEDIUM", passenger_ids=[4])
        passenger = copy.deepcopy(target)
        passenger.update(
            operator_id=4, operator_type="PERSONNEL",
            state="STOPPED", suppression_remaining_ms=0,
        )
        passenger["raw_attributes"].pop("passenger_ids")
        passenger["raw_attributes"]["armor_class"] = "NONE"
        record["operators"].append(passenger)
        record["deployment"]["positions"]["blue"]["4"] = [6, 1]
        engine = self.engine(record)
        before = engine.snapshot()["world"]["operators"]
        result = engine.step([shoot()], delta_time_ms=1000)
        after = engine.snapshot()["world"]["operators"]
        self.assertTrue(result.decisions[0].accepted)
        expected_target = copy.deepcopy(before["2"])
        expected_target["suppression_remaining_ms"] -= 1000
        self.assertEqual(after["2"], expected_target)
        self.assertEqual(after["4"], before["4"])

    def test_mixed_range_joint_group_spends_both_shots(self):
        record = fixture_record(grouped=True)
        grouped = self.engine(record)
        control = self.engine(record)
        actions = [shoot(1), shoot(3)]
        valid_ids = {item.action_id for item in grouped.valid_actions("red")}
        self.assertTrue({item.action_id for item in actions} <= valid_ids)
        result = grouped.step(actions, delta_time_ms=1)
        control.step([shoot(3)], delta_time_ms=1)
        self.assertTrue(all(item.accepted for item in result.decisions))
        self.assertEqual(len(result.decisions), 2)
        actual, expected = grouped.snapshot(), control.snapshot()
        self.assertEqual(actual["rng"], expected["rng"])
        self.assertEqual(
            actual["world"]["operators"]["2"],
            expected["world"]["operators"]["2"],
        )
        expected_cooldown = (
            AdjudicationTableSet(TABLES).timing("direct_fire_cooldown_seconds")
            * 1000 - 1
        )
        for actor_id in ("1", "3"):
            actor = actual["world"]["operators"][actor_id]
            self.assertEqual(actor["raw_attributes"]["ammunition"]["0"], 19)
            self.assertEqual(actor["weapon_cooldown_remaining_ms"], expected_cooldown)
        self.assertEqual(
            {event.actor_id for event in result.events
             if event.event_type is EventType.DICE_ROLLED},
            {3},
        )

    def test_engine_still_rejects_invalid_target_owner_ammo_and_visibility(self):
        for name, expected in (
            ("missing_target", "TARGET_NOT_FOUND"),
            ("wrong_owner", "ACTOR_NOT_CONTROLLED"),
            ("friendly_target", "SAME_SIDE_TARGET"),
            ("ammunition", "AMMUNITION_UNAVAILABLE"),
            ("not_carried", "WEAPON_NOT_CARRIED"),
            ("visibility", "TARGET_NOT_OBSERVED"),
            ("missing_hex", "CURRENT_HEX_REQUIRED"),
            ("invalid_map_hex", "unknown hex: 999"),
        ):
            with self.subTest(rule=name):
                record = fixture_record()
                action = shoot()
                if name == "missing_target":
                    action = shoot(target_id=9999)
                elif name == "wrong_owner":
                    action = shoot(side="blue")
                elif name == "friendly_target":
                    action = shoot(target_id=1)
                elif name == "ammunition":
                    record["operators"][0]["raw_attributes"]["ammunition"]["0"] = 0
                elif name == "not_carried":
                    action = shoot(weapon_id=29)
                elif name == "visibility":
                    # Preserve the original fixture's genuine ridge/forest LOS block.
                    original = json.loads((ROOT / (
                        "examples/source_movement_scenario.json"
                    )).read_text("utf-8"))
                    record["map"]["hexes"] = original["map"]["hexes"]
                elif name == "missing_hex":
                    record["operators"][1]["current_hex"] = None
                else:
                    record["operators"][1]["current_hex"] = 999
                engine = self.engine(record)
                before = engine.snapshot()
                result = engine.step([action], delta_time_ms=1)
                after = engine.snapshot()
                self.assertFalse(result.decisions[0].accepted)
                self.assertEqual(result.decisions[0].code, expected)
                self.assertEqual(after["rng"], before["rng"])
                for actor_id in ("1", "2"):
                    self.assertEqual(
                        after["world"]["operators"][actor_id],
                        before["world"]["operators"][actor_id],
                    )
                self.assertFalse({
                    EventType.AMMUNITION_CONSUMED,
                    EventType.WEAPON_COOLDOWN_STARTED,
                } & event_types(result))

    def test_actual_loitering_fixture_rejects_overshoot_without_consuming_actor(self):
        engine = NativeEngine(load_scenario(
            ROOT / "examples/maps/scenarios/loitering-munition.json"
        ))
        self.addCleanup(engine.close)
        engine.reset(seed=42)
        before = engine.snapshot()
        result = engine.step([shoot(weapon_id=76)], delta_time_ms=1)
        after = engine.snapshot()
        # The real drone's observation range equals its attack range, so this
        # public fixture fails visibility before reaching the strict range check.
        # The isolated SELF_AS_MUNITION case above covers that later check.
        self.assertEqual(result.decisions[0].code, "TARGET_NOT_OBSERVED")
        self.assertFalse(result.decisions[0].accepted)
        self.assertTrue(after["world"]["operators"]["1"]["alive"])
        self.assertEqual(
            after["world"]["operators"]["1"]["lifecycle_remaining_ms"],
            before["world"]["operators"]["1"]["lifecycle_remaining_ms"] - 1,
        )
        self.assertEqual(after["rng"], before["rng"])
        self.assertEqual(
            after["world"]["operators"]["2"], before["world"]["operators"]["2"]
        )
        self.assertNotIn(EventType.UNIT_DESTROYED, event_types(result))

    def test_actual_guided_request_is_tagged_and_keeps_range_restriction(self):
        engine = NativeEngine(load_scenario(
            ROOT / "examples/maps/scenarios/guided-fire.json"
        ))
        self.addCleanup(engine.close)
        world = engine.reset(seed=42)
        capability = engine.get_guided_fire_capabilities("red")[0]
        guided = engine.build_guided_fire_request(capability)
        direct = engine._guided_direct_fire_request(guided, world)
        self.assertIs(direct.fire_mode, FireMode.GUIDED)
        self.assertFalse(direct.permits_out_of_range_attempt)
        overshoot = replace(direct, distance=direct.weapon.maximum_range + 1)
        result = DirectFireAdjudicator(
            AdjudicationTableSet(TABLES), FixedDiceRoller([])
        ).stage(overshoot)
        self.assertEqual(result.error, "TARGET_OUT_OF_RANGE")
        self.assertIsNone(result.outcome)

    def test_product_group_preview_and_apply_accept_both_ranges(self):
        with tempfile.TemporaryDirectory() as storage:
            session = RealtimeAgentSession(
                ScenarioMatchFactory(
                    bundle=ScenarioBundle.from_scenario(
                        scenario_from_dict(fixture_record(grouped=True))
                    ),
                    storage_root=Path(storage),
                    red_controller_id="range-red", blue_controller_id="range-blue",
                    decision_interval_ms=1000, frame_interval_ms=1000,
                ),
                run_namespace="direct-range", tick_interval_ms=1000,
                clock=ManualRealtimeClock(),
            )
            self.addCleanup(session.close)
            binding = session.reset(seed=42).red_binding
            version = session.latest_view(binding).observation_version
            operation = SeatOperation(
                "SHOOT", actor_ids=(1, 3), target_id=2,
                weapon_choices=((1, 36), (3, 36)),
            )
            preview = session.preview_operation(
                binding, operation, observation_version=version,
                request_id="mixed-range-preview",
            )
            self.assertEqual(
                (preview.selected_count, preview.executable_count, preview.rejected_count),
                (2, 2, 0),
            )
            self.assertTrue(preview.can_execute)
            self.assertFalse(preview.partial_confirmation_required)
            self.assertTrue(all(
                item.accepted_for_preview for item in preview.per_unit_results
            ))
            receipt = session.submit_operation(
                binding, observation_version=version, operation=operation,
            )
            self.assertTrue(receipt.accepted)
            session.advance_tick()
            event = session.command_event(binding, receipt.command_sequence)
            self.assertIs(event.status, CommandStatus.APPLIED)


if __name__ == "__main__":
    unittest.main()
