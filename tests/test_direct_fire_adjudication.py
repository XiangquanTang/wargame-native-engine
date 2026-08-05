from __future__ import annotations

from pathlib import Path

import pytest

from wargame_native_engine.adjudication.dice import DiceRoller, FixedDiceRoller
from wargame_native_engine.adjudication.direct_fire import DirectFireAdjudicator
from wargame_native_engine.adjudication.modifiers import roll_modifiers
from wargame_native_engine.adjudication.request import DirectFireRequest, WeaponProfile
from wargame_native_engine.adjudication.table_loader import (
    AdjudicationTableSet, TableLookup, UnresolvedTableCellError,
)
from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain
from wargame_native_engine.core.event import EventType
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.rng import NativeRNG
from wargame_native_engine.visibility.result import VisibilityResult
from wargame_native_engine.adjudication.replay import (
    direct_fire_request_to_dict, replay_direct_fire, write_direct_fire_replay,
)
from wargame_native_engine.adjudication.scheduler import CommandScheduler


ROOT = Path(__file__).resolve().parents[1]
TABLES = AdjudicationTableSet(ROOT / "config/adjudication/source_tables_v1/manifest.json")


def operator(operator_id: int, side: str, domain: UnitDomain, *, blood: int = 3,
             state: str = "STOPPED", weapon_ids=None) -> OperatorState:
    return OperatorState(operator_id, side, domain.value, None, 0, 0, 0, 0,
                         100, 100, 0, 1, 1, 25, state, True,
                         list(weapon_ids or []), blood=blood, max_blood=3,
                         deployed=True)


class FakeTables:
    content_hash = "f" * 64
    table_set_id = "test-tables"
    table_set_version = "1"
    schema_version = "test/1"
    source_document_hash = "e" * 64
    document = {
        "runtime_rules": {"suppression": {"effective_duration_seconds": 150}},
        "direct_fire": {"personnel_result_modifiers": {"application": {
            "symbolic_base_result": {"ordered_outcomes": [
                "NO_EFFECT", "SUPPRESS", "LOSS_1", "LOSS_2", "LOSS_3", "LOSS_4"]}
        }}},
    }

    def __init__(self, base, correction=0):
        self.base = base
        self.correction = correction
        self.last_armor = None

    def attack_level(self, table_name, weapon_class, strength, distance):
        return TableLookup(table_name, "test", 5, "RESOLVED", 1)

    def elevation_modifier(self, difference, distance):
        return TableLookup("elevation", "test", 0, "RESOLVED", 1)

    def personnel_base_result(self, attack_level, roll):
        return TableLookup("personnel-base", "test", self.base)

    def vehicle_base_losses(self, target_strength, attack_level, roll):
        return TableLookup("vehicle-base", "test", int(self.base))

    def personnel_result_correction(self, modified_roll):
        return TableLookup("personnel-correction", "test", self.correction)

    def vehicle_loss_correction(self, modified_roll, armor):
        self.last_armor = armor
        return TableLookup("vehicle-correction", "test", self.correction)

    @staticmethod
    def roll_modifier_config(target_kind):
        return {"shooter_state": {"suppressed": -1, "moving": -1,
                                    "tank_on_water": -1},
                "target_terrain": {"residential": -1,
                                     "residential_or_fort": -1,
                                     "fort": -1, "forest": -2, "water": -2},
                "target_state": {"concealed": -2, "moving": -2,
                                   "stacked": 2, "marching": 4}}

    @staticmethod
    def timing(name):
        assert name == "direct_fire_cooldown_seconds"
        return 75


def request(*, target_domain=UnitDomain.PERSONNEL, result_state="STOPPED",
            attacker_state="STOPPED", attacker_blood=3, target_blood=3,
            passengers=(), ammo=2) -> DirectFireRequest:
    attacker = operator(1, "red", UnitDomain.VEHICLE, blood=attacker_blood,
                        state=attacker_state, weapon_ids=[9])
    target = operator(2, "blue", target_domain, blood=target_blood,
                      state=result_state)
    return DirectFireRequest(
        4, 42, "action-test", attacker, target,
        WeaponProfile(9, "large_direct_fire_gun", 10,
                      frozenset({UnitDomain.PERSONNEL, UnitDomain.VEHICLE})),
        VisibilityResult(True, True, 2, 25, (), (), "observable"), 2,
        target_domain, ArmorClass.MEDIUM if target_domain is UnitDomain.VEHICLE
        else ArmorClass.NONE, ammo, carried_passengers=passengers,
        attacker_suppressed=attacker_state == "SUPPRESSED",
        attacker_moving=attacker_state == "MOVING")


def resolve(base, *, correction=0, req=None, dice=(3, 4, 3)):
    current = request() if req is None else req
    tables = FakeTables(base, correction)
    result = DirectFireAdjudicator(tables, FixedDiceRoller(dice)).resolve(current)
    return current, result, tables


def test_personnel_attack_level_lookup() -> None:
    assert TABLES.attack_level("direct_fire_personnel_attack_levels",
                               "LARGE_DIRECT_FIRE_CANNON", 1, 10).value == 2


def test_vehicle_attack_level_lookup() -> None:
    assert TABLES.attack_level("direct_fire_vehicle_attack_levels",
                               "LARGE_DIRECT_FIRE_CANNON", 1, 18).value == 2


def test_infantry_vs_vehicle_special_table() -> None:
    assert TABLES.attack_level("infantry_vs_vehicle_attack_levels",
                               "INFANTRY_LIGHT_WEAPON", 4, 3).value == 1


def test_distance_boundary_lookup() -> None:
    with pytest.raises(ValueError, match="outside"):
        TABLES.attack_level("direct_fire_vehicle_attack_levels",
                            "LARGE_DIRECT_FIRE_CANNON", 1, 19)


def test_strength_count_changes_attack_level() -> None:
    low = TABLES.attack_level("direct_fire_personnel_attack_levels",
                              "LARGE_DIRECT_FIRE_CANNON", 1, 0).value
    high = TABLES.attack_level("direct_fire_personnel_attack_levels",
                               "LARGE_DIRECT_FIRE_CANNON", 4, 0).value
    assert (low, high) == (2, 7)


def test_elevation_modifier() -> None:
    assert TABLES.elevation_modifier(1, 1).value == -2


def test_unresolved_cell_is_rejected() -> None:
    with pytest.raises(UnresolvedTableCellError):
        TABLES.weapon_type(999999)


def test_blank_infantry_attack_cell_is_explicit_zero() -> None:
    assert TABLES.attack_level("infantry_vs_vehicle_attack_levels",
                               "INFANTRY_LIGHT_WEAPON", 1, 2).value == 0


def test_fixed_one_die_roll() -> None:
    assert FixedDiceRoller([6]).roll_d6(tick=1, action_id="a").raw_total == 6


def test_fixed_two_dice_roll() -> None:
    record = FixedDiceRoller([3, 4]).roll_2d6(tick=1, action_id="a")
    assert record.individual_dice == (3, 4) and record.raw_total == 7


@pytest.mark.parametrize(("changes", "name", "value"), [
    ({"attacker_suppressed": True}, "shooter_suppressed", -1),
    ({"attacker_moving": True}, "shooter_moving", -1),
    ({"target_terrain": "FOREST"}, "target_forest", -2),
    ({"target_concealed": True}, "target_concealed", -2),
    ({"target_stacked": True}, "target_stacked", 2),
    ({"target_marching": True}, "target_marching", 4),
])
def test_roll_modifiers(changes, name, value) -> None:
    req = request()
    for key, item in changes.items():
        setattr(req, key, item)
    config = TABLES.roll_modifier_config(
        "vehicle" if req.target_domain is UnitDomain.VEHICLE else "personnel")
    assert (name, value) in {(item.name, item.value)
                             for item in roll_modifiers(req, config)}


def test_no_effect() -> None:
    req, result, _ = resolve("NO_EFFECT")
    assert result.outcome.no_effect and req.target.blood == 3


def test_suppression_only() -> None:
    req, result, _ = resolve("SUPPRESS")
    assert result.outcome.suppression_applied and req.target.state == "SUPPRESSED"


def test_personnel_loss() -> None:
    req, result, _ = resolve(2)
    assert result.outcome.squad_losses == 2 and req.target.blood == 1


def test_vehicle_loss_and_suppression() -> None:
    req = request(target_domain=UnitDomain.VEHICLE)
    req, result, _ = resolve(1, req=req, dice=(3, 4, 3, 4))
    assert result.outcome.vehicle_losses == 1 and req.target.state == "SUPPRESSED"


def test_repeated_personnel_suppression_causes_loss() -> None:
    req = request(result_state="SUPPRESSED")
    req, result, _ = resolve("SUPPRESS", req=req)
    assert req.target.blood == 2 and result.outcome.squad_losses == 1


def test_carried_personnel_share_vehicle_losses() -> None:
    passenger = operator(3, "blue", UnitDomain.PERSONNEL, blood=2)
    req = request(target_domain=UnitDomain.VEHICLE, passengers=(passenger,))
    resolve(2, req=req, dice=(3, 4, 3, 4))
    assert passenger.blood == 0 and not passenger.alive


def test_destroyed_at_zero_blood() -> None:
    req = request(target_blood=1)
    req, result, _ = resolve(1, req=req)
    assert result.outcome.destroyed and not req.target.alive


def test_ammunition_consumed() -> None:
    _, result, _ = resolve("NO_EFFECT")
    assert result.ammunition_after == 1
    assert any(item.event_type is EventType.AMMUNITION_CONSUMED for item in result.events)


def test_weapon_cooldown_started() -> None:
    _, result, _ = resolve("NO_EFFECT")
    assert result.cooldown_after == 75
    assert any(item.event_type is EventType.WEAPON_COOLDOWN_STARTED for item in result.events)


def test_armor_modifier_is_explicit_lookup_axis() -> None:
    req = request(target_domain=UnitDomain.VEHICLE)
    _, result, tables = resolve(0, req=req, dice=(1, 6, 3, 4))
    looked_up = next(item for item in result.events
                     if item.event_type is EventType.COMBAT_RESULT_LOOKED_UP)
    assert tables.last_armor == "MEDIUM"
    assert looked_up.data["final_outcome"]["no_effect"]


def test_final_table_metadata_and_hash_are_validated() -> None:
    assert TABLES.schema_version == "native-adjudication-tables/1.2"
    assert TABLES.table_set_version == "1.2.1-final"
    assert TABLES.content_hash == "2d0d03fa7ac5f1072b550378c20347b3e35e088a1348b459ae05967c0bab11d6"


def test_elevation_blank_and_manual_correction() -> None:
    assert TABLES.elevation_modifier(1, 12).value == 0
    assert TABLES.elevation_modifier(7, 12).value == -1
    assert TABLES.elevation_modifier(-7, 12).value == 0


def test_personnel_uses_base_2d6_and_separate_correction_d6() -> None:
    req = request()
    result = DirectFireAdjudicator(
        TABLES, FixedDiceRoller([1, 1, 6], episode_seed=42)).resolve(req)
    rolls = [item.data for item in result.events
             if item.event_type is EventType.DICE_ROLLED]
    assert [item["individual_dice"] for item in rolls] == [[1, 1], [6]]
    assert result.outcome.squad_losses == 1


def test_vehicle_uses_two_independent_2d6_rolls() -> None:
    req = request(target_domain=UnitDomain.VEHICLE)
    result = DirectFireAdjudicator(
        TABLES, FixedDiceRoller([1, 1, 3, 4], episode_seed=42)).resolve(req)
    rolls = [item.data for item in result.events
             if item.event_type is EventType.DICE_ROLLED]
    assert [item["individual_dice"] for item in rolls] == [[1, 1], [3, 4]]
    assert result.outcome.vehicle_losses == 3


def test_legality_returns_reason_and_evidence() -> None:
    req = request(ammo=0)
    result = DirectFireAdjudicator(TABLES, FixedDiceRoller([])).resolve(req)
    assert not result.legality.legal
    assert result.legality.reason_code == "AMMUNITION_UNAVAILABLE"
    assert result.legality.evidence["checks"]["ammunition_available"] is False


def test_command_scheduler_is_first_arrival_first_adjudication() -> None:
    scheduler = CommandScheduler()
    first = scheduler.submit({"side": "red"}, submitted_at=10, duration=5)
    second = scheduler.submit({"side": "blue"}, submitted_at=10, duration=5)
    assert scheduler.advance_to(14) == ()
    assert scheduler.advance_to(15) == (first, second)


def test_direct_fire_replay_strict_round_trip(tmp_path: Path) -> None:
    req = request()
    request_document = direct_fire_request_to_dict(req)
    result = DirectFireAdjudicator(
        TABLES, DiceRoller(NativeRNG(req.episode_seed))).resolve(req)
    output = tmp_path / "replay"
    write_direct_fire_replay(output, request_document=request_document,
                             resolution=result, tables=TABLES)
    verification = replay_direct_fire(output, tables=TABLES)
    assert verification.matches
