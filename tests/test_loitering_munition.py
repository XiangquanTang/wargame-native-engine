from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_source_movement import _move, _scenario_document
from wargame_native_engine.agents.authorization import (
    freeze_valid_actions,
    resolve_agent_selection,
)
from wargame_native_engine.agents.protocol import AgentAction
from wargame_native_engine.compat.source_frame.exporter import SourceFrameExporter
from wargame_native_engine.compat.source_frame.template import (
    source_frame_template_for_scenario,
)
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.io.replay import ReplayWriter, replay_actions
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.rules.config import load_ruleset
from wargame_native_engine.rules.operator_lifecycle import (
    LifecycleExpiryBehavior,
    load_operator_lifecycle_authority,
)
from wargame_native_engine.rules.loitering_munition import (
    ATTACK_RESOLUTION_COMMITTED,
    SELF_AS_MUNITION,
    load_loitering_munition_attack_authority,
)


PROFILE_ID = "LOITERING_MUNITION_PREDEPLOYED_V1"


def _scenario(
    *,
    lifecycle_remaining_ms: int = 1_200_000,
    target_blood: int = 3,
):
    document = _scenario_document(mover_domain="AIR", mover_speed=90)
    document["scenario_id"] = "loitering-munition-lifecycle-test"
    document["terminal_rules"]["max_ticks"] = 5_000
    loiter = document["operators"][0]
    loiter.update({
        "subtype": "source-3-7",
        "weapon_ids": [76],
        "lifecycle_profile_id": PROFILE_ID,
        "lifecycle_remaining_ms": lifecycle_remaining_ms,
    })
    loiter["raw_attributes"] = {
        "armor_class": "UNARMORED",
        "ammunition": {},
    }
    document["metadata"]["source_compatible"]["weapons"]["76"] = {
        "weapon_class": "loitering_munition",
        "maximum_range": 2,
        "target_domains": ["PERSONNEL", "VEHICLE"],
        "requires_unfolded": False,
        "can_fire_while_moving": True,
    }
    blue = next(
        item for item in document["operators"] if item["operator_id"] == 2
    )
    blue.update({
        "current_hex": 104,
        "x": 4,
        "y": 1,
        "blood": target_blood,
        "max_blood": target_blood,
    })
    document["deployment"]["positions"]["blue"]["2"] = [4, 1]
    return scenario_from_dict(document)


def _simultaneous_destruction_scenario():
    document = _scenario_document(mover_domain="AIR", mover_speed=90)
    document["scenario_id"] = "loitering-simultaneous-destruction-test"
    document["terminal_rules"]["max_ticks"] = 5_000
    loiter = document["operators"][0]
    loiter.update({
        "subtype": "source-3-7",
        "current_hex": 102,
        "x": 2,
        "y": 1,
        "weapon_ids": [76],
        "blood": 1,
        "max_blood": 1,
        "lifecycle_profile_id": PROFILE_ID,
        "lifecycle_remaining_ms": 1_200_000,
    })
    loiter["raw_attributes"] = {
        "armor_class": "NONE",
        "ammunition": {},
    }
    blue = next(
        item for item in document["operators"] if item["operator_id"] == 2
    )
    blue.update({
        "operator_type": "VEHICLE",
        "current_hex": 103,
        "x": 3,
        "y": 1,
        "weapon_ids": [36],
        "weapon_unfolded": True,
    })
    blue["raw_attributes"] = {
        "armor_class": "LIGHT",
        "ammunition": {"0": 20},
    }
    document["deployment"]["positions"]["red"]["1"] = [2, 1]
    document["deployment"]["positions"]["blue"]["2"] = [3, 1]
    document["metadata"]["source_compatible"]["weapons"].update({
        "36": {
            "weapon_class": "large_direct_fire_gun",
            "maximum_range": 10,
            "target_domains": ["AIR", "PERSONNEL", "VEHICLE"],
            "ammunition_key": "0",
            "requires_unfolded": False,
            "can_fire_while_moving": False,
        },
        "76": {
            "weapon_class": "loitering_munition",
            "maximum_range": 2,
            "target_domains": ["PERSONNEL", "VEHICLE"],
            "requires_unfolded": False,
            "can_fire_while_moving": True,
        },
    })
    return scenario_from_dict(document)


def _simultaneous_fire_actions(engine: NativeEngine):
    red = next(
        value.action for value in engine.valid_actions("red")
        if value.action.actor_id == 1
        and value.action.action_type is ActionType.SHOOT
        and value.action.target_id == 2
        and value.action.parameters["weapon_id"] == 76
    )
    blue = next(
        value.action for value in engine.valid_actions("blue")
        if value.action.actor_id == 2
        and value.action.action_type is ActionType.SHOOT
        and value.action.target_id == 1
        and value.action.parameters["weapon_id"] == 36
    )
    return red, blue


def _record_empty_replay(
    scenario,
    output: Path,
    delta_times_ms: list[int],
    *,
    seed: int = 42,
) -> NativeEngine:
    engine = NativeEngine(scenario)
    engine.reset(seed)
    writer = ReplayWriter(
        output,
        run_id="loitering-subsecond",
        scenario=scenario,
        seed=seed,
        agents={"red": "test", "blue": "test"},
        max_ticks=5_000,
        ruleset=load_ruleset(),
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    for delta_time_ms in delta_times_ms:
        result = engine.step([], delta_time_ms=delta_time_ms)
        writer.record_step(result, engine.snapshot())
    writer.finalize({"result": engine.result().to_dict()}, status="complete")
    return engine


def _engine() -> NativeEngine:
    engine = NativeEngine(_scenario())
    engine.reset(42)
    return engine


def _move_into_attack_position(engine: NativeEngine):
    move = next(
        value.action
        for value in engine.valid_actions("red")
        if value.action.actor_id == 1
        and value.action.action_type is ActionType.MOVE
        and value.action.parameters["path"] == [102]
    )
    engine.step([move], delta_time_ms=8_000)
    return next(
        value.action
        for value in engine.valid_actions("red")
        if value.action.actor_id == 1
        and value.action.action_type is ActionType.SHOOT
        and value.action.target_id == 2
        and value.action.parameters["weapon_id"] == 76
    )


def test_source_identity_and_lifecycle_authority_are_explicit() -> None:
    authority = load_operator_lifecycle_authority()
    profile = authority.profile_for_source_codes(3, 7)

    assert profile is not None
    assert profile.profile_id == PROFILE_ID
    assert profile.canonical_identity == "LOITERING_MUNITION"
    assert profile.operator_domain == "AIR"
    assert profile.maximum_lifetime_ms == 1_200_000
    assert profile.expiry_behavior is LifecycleExpiryBehavior.DESTROY_OPERATOR


def test_initial_lifecycle_is_authoritative_and_snapshot_restores() -> None:
    engine = _engine()
    assert engine._require_world().operators[1].lifecycle_remaining_ms == 1_200_000

    engine.step([], delta_time_ms=321_000)
    snapshot = engine.snapshot()
    assert snapshot["operator_lifecycle_authority"] == (
        engine._operator_lifecycle_authority.identity_record()
    )

    restored = NativeEngine(_scenario())
    restored.reset(7)
    restored.restore(snapshot)

    assert canonical_hash(restored.snapshot()) == canonical_hash(snapshot)
    assert restored._require_world().operators[1].lifecycle_remaining_ms == 879_000


@pytest.mark.parametrize("delta_ms", [1_000, 5_000, 10_000])
def test_expiry_uses_authoritative_time_not_step_count(delta_ms: int) -> None:
    engine = _engine()
    expiry_events = []

    for _ in range(1_200_000 // delta_ms):
        result = engine.step([], delta_time_ms=delta_ms)
        expiry_events.extend(
            event
            for event in result.events
            if event.event_type is EventType.UNIT_DESTROYED
            and event.data.get("reason_code") == "LIFECYCLE_EXPIRED"
        )

    unit = engine._require_world().operators[1]
    assert engine._require_world().elapsed_time_ms == 1_200_000
    assert unit.lifecycle_remaining_ms == 0
    assert unit.alive is False
    assert unit.blood == 0
    assert unit.health == 0
    assert unit.state == "destroyed"
    assert len(expiry_events) == 1
    assert expiry_events[0].data["event_time_ms"] == 1_200_000


def test_large_tick_expires_at_exact_boundary_and_clears_movement() -> None:
    engine = _engine()
    rng_before = engine.snapshot()["rng"]
    engine.step([_move([102])], delta_time_ms=8_000)
    engine.step([_move([103])], delta_time_ms=1_500_000)

    unit = engine._require_world().operators[1]
    assert unit.alive is False
    assert unit.lifecycle_remaining_ms == 0
    assert unit.segment_to_hex is None
    assert unit.move_path == []
    assert unit.speed == 0
    assert engine.snapshot()["rng"] == rng_before


def test_air_operator_reuses_move_and_observation_pipeline() -> None:
    engine = _engine()
    assert engine._require_world().operators[1].raw_attributes[
        "see_enemy_bop_ids"
    ] == []
    move = next(
        value.action
        for value in engine.valid_actions("red")
        if value.action.actor_id == 1
        and value.action.action_type is ActionType.MOVE
        and value.action.parameters["path"] == [102]
    )

    result = engine.step([move], delta_time_ms=8_000)

    unit = engine._require_world().operators[1]
    assert result.decisions[0].accepted is True
    assert unit.current_hex == 102
    assert unit.lifecycle_remaining_ms == 1_192_000
    assert any(
        event.event_type is EventType.ARRIVED_HEX
        and event.actor_id == 1
        for event in result.events
    )
    # The profile identity, rather than a Viewer mapping or raw canonical
    # attribute, activates the existing two-hex drone observation profile.
    assert unit.raw_attributes["see_enemy_bop_ids"] == [2]


def test_expired_operator_has_no_valid_actions() -> None:
    engine = _engine()
    engine.step([], delta_time_ms=1_200_000)

    assert not any(
        value.action.actor_id == 1 for value in engine.valid_actions("red")
    )


def test_agent_consumes_the_existing_frozen_move_capability() -> None:
    engine = _engine()
    valid = engine.valid_actions("red")
    selected = next(
        value for value in valid
        if value.action.actor_id == 1
        and value.action.action_type is ActionType.MOVE
    )
    frozen = freeze_valid_actions("red", valid)

    resolved = resolve_agent_selection(
        "red",
        AgentAction.from_valid_action(selected),
        frozen,
    )

    assert resolved == selected.action
    assert resolved.action_type is ActionType.MOVE


def test_enemy_observation_does_not_leak_lifecycle_authority() -> None:
    engine = _engine()

    assert all(
        value.operator_id != 1 for value in engine.observe("blue").visible_enemies
    )


def test_expiry_event_and_world_hash_are_deterministic() -> None:
    values = []
    for _ in range(2):
        engine = _engine()
        result = engine.step([], delta_time_ms=1_200_000)
        values.append((
            canonical_hash(engine._require_world().to_dict()),
            tuple(event.to_dict() for event in result.events),
            engine.snapshot()["rng"],
        ))

    assert values[0] == values[1]


def test_out_of_range_attack_is_not_exposed() -> None:
    engine = _engine()

    assert not any(
        value.action.actor_id == 1
        and value.action.action_type in {ActionType.SHOOT, ActionType.FIRE}
        for value in engine.valid_actions("red")
    )


def test_attack_authority_is_explicit_self_as_munition() -> None:
    authority = load_loitering_munition_attack_authority()
    policy = authority.policy_for(
        lifecycle_profile_id=PROFILE_ID,
        weapon_id=76,
    )

    assert policy is not None
    assert policy.resource_policy == SELF_AS_MUNITION
    assert policy.consume_condition == ATTACK_RESOLUTION_COMMITTED
    assert policy.maximum_range == 2
    assert policy.target_domains == ("PERSONNEL", "VEHICLE")
    # The consumption condition is independent of presentation labels.  The
    # current Native tables expose losses/suppression/NO_EFFECT rather than a
    # separate hit-check or MISS result.
    for formal_result in ("HIT", "MISS", "NO_EFFECT"):
        assert formal_result and policy.consume_condition == (
            ATTACK_RESOLUTION_COMMITTED
        )


@pytest.mark.parametrize(
    ("seed", "expected_blood", "expected_state", "expected_no_effect"),
    [
        (7, 2, "STOPPED", False),
        (1, 3, "SUPPRESSED", False),
        (9, 3, "STOPPED", True),
    ],
)
def test_every_formal_weapon76_resolution_consumes_attacker(
    seed: int,
    expected_blood: int,
    expected_state: str,
    expected_no_effect: bool,
) -> None:
    engine = NativeEngine(_scenario())
    engine.reset(seed)
    shoot = _move_into_attack_position(engine)

    result = engine.step([shoot], delta_time_ms=1_000)

    attacker = engine._require_world().operators[1]
    target = engine._require_world().operators[2]
    lookup = next(
        event for event in result.events
        if event.event_type is EventType.COMBAT_RESULT_LOOKED_UP
    )
    final = lookup.data["final_outcome"]
    assert result.decisions[0].accepted is True
    assert target.blood == expected_blood
    assert target.state == expected_state
    assert final["no_effect"] is expected_no_effect
    assert attacker.alive is False
    assert attacker.blood == 0
    assert attacker.health == 0
    assert attacker.lifecycle_remaining_ms == 0
    assert attacker.raw_attributes["ammunition"] == {}
    assert not any(
        event.event_type is EventType.AMMUNITION_CONSUMED
        and event.actor_id == attacker.operator_id
        for event in result.events
    )
    consumed = tuple(
        event for event in result.events
        if event.event_type is EventType.UNIT_DESTROYED
        and event.actor_id == attacker.operator_id
        and event.data.get("reason_code") == "LOITERING_MUNITION_CONSUMED"
    )
    assert len(consumed) == 1
    assert consumed[0].data["adjudication_id"]
    assert consumed[0].data["consume_condition"] == (
        ATTACK_RESOLUTION_COMMITTED
    )


def test_illegal_weapon76_attack_does_not_consume_attacker_or_rng() -> None:
    engine = _engine()
    rng_before = engine.snapshot()["rng"]
    forged = EngineAction(
        "red",
        1,
        ActionType.SHOOT,
        target_id=2,
        parameters={"weapon_id": 76},
    )

    result = engine.step([forged], delta_time_ms=1_000)

    assert result.decisions[0].accepted is False
    assert engine._require_world().operators[1].alive is True
    assert engine._require_world().operators[1].blood == 3
    assert engine.snapshot()["rng"] == rng_before
    assert not any(
        event.event_type is EventType.UNIT_DESTROYED
        and event.actor_id == 1
        for event in result.events
    )


def test_attack_effect_and_self_consumption_are_one_deterministic_commit() -> None:
    records = []
    for _ in range(2):
        engine = NativeEngine(_scenario())
        engine.reset(7)
        shoot = _move_into_attack_position(engine)
        before = canonical_hash(engine._require_world().to_dict())
        result = engine.step([shoot], delta_time_ms=1_000)
        records.append({
            "pre_state_hash": before,
            "world_hash": canonical_hash(engine._require_world().to_dict()),
            "rng": engine.snapshot()["rng"],
            "events": tuple(event.to_dict() for event in result.events),
            "target_blood": engine._require_world().operators[2].blood,
            "attacker_alive": engine._require_world().operators[1].alive,
        })

    assert records[0] == records[1]
    assert records[0]["target_blood"] == 2
    assert records[0]["attacker_alive"] is False


def test_agent_consumes_existing_frozen_weapon76_shoot_action() -> None:
    engine = _engine()
    shoot = _move_into_attack_position(engine)
    valid = engine.valid_actions("red")
    selected = next(value for value in valid if value.action == shoot)
    frozen = freeze_valid_actions("red", valid)

    resolved = resolve_agent_selection(
        "red",
        AgentAction.from_valid_action(selected),
        frozen,
    )

    assert resolved.action_type is ActionType.SHOOT
    assert resolved.parameters == {"weapon_id": 76}


def test_attack_and_expiry_destruction_reasons_are_distinct() -> None:
    attack_engine = _engine()
    attack = _move_into_attack_position(attack_engine)
    attack_result = attack_engine.step([attack], delta_time_ms=1_000)

    expiry_engine = _engine()
    expiry_result = expiry_engine.step([], delta_time_ms=1_200_000)

    attack_reasons = {
        event.data.get("reason_code") for event in attack_result.events
        if event.event_type is EventType.UNIT_DESTROYED
        and event.actor_id == 1
    }
    expiry_reasons = {
        event.data.get("reason_code") for event in expiry_result.events
        if event.event_type is EventType.UNIT_DESTROYED
        and event.actor_id == 1
    }
    assert attack_reasons == {"LOITERING_MUNITION_CONSUMED"}
    assert expiry_reasons == {"LIFECYCLE_EXPIRED"}


def test_weapon76_attack_strict_replay(tmp_path: Path) -> None:
    scenario = _scenario()
    ruleset = load_ruleset()
    engine = NativeEngine(scenario)
    engine.reset(7)
    output = tmp_path / "replay"
    writer = ReplayWriter(
        output,
        run_id="loitering-attack",
        scenario=scenario,
        seed=7,
        agents={"red": "test", "blue": "test"},
        max_ticks=5_000,
        ruleset=ruleset,
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    move = next(
        value.action for value in engine.valid_actions("red")
        if value.action.actor_id == 1
        and value.action.action_type is ActionType.MOVE
        and value.action.parameters["path"] == [102]
    )
    move_result = engine.step([move], delta_time_ms=8_000)
    writer.record_step(move_result, engine.snapshot())
    shoot = next(
        value.action for value in engine.valid_actions("red")
        if value.action.actor_id == 1
        and value.action.action_type is ActionType.SHOOT
    )
    attack_result = engine.step([shoot], delta_time_ms=1_000)
    writer.record_step(attack_result, engine.snapshot())
    writer.finalize(
        {"result": engine.result().to_dict()},
        status="complete",
    )

    verification = replay_actions(scenario, output, strict=True, seed=7)

    assert verification.matches is True
    assert verification.actual_world_hash == verification.expected_world_hash


def test_lifecycle_expiry_strict_replay(tmp_path: Path) -> None:
    scenario = _scenario()
    ruleset = load_ruleset()
    engine = NativeEngine(scenario)
    engine.reset(42)
    output = tmp_path / "replay"
    writer = ReplayWriter(
        output,
        run_id="loitering-expiry",
        scenario=scenario,
        seed=42,
        agents={"red": "test", "blue": "test"},
        max_ticks=5_000,
        ruleset=ruleset,
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    result = engine.step([], delta_time_ms=1_200_000)
    writer.record_step(result, engine.snapshot())
    writer.finalize(
        {"result": engine.result().to_dict()},
        status="complete",
    )

    verification = replay_actions(scenario, output, strict=True, seed=42)

    assert verification.matches is True
    assert verification.actual_world_hash == verification.expected_world_hash


def test_simultaneous_combat_and_self_consumption_publish_one_death_fact() -> None:
    records = []
    for _ in range(2):
        scenario = _simultaneous_destruction_scenario()
        engine = NativeEngine(scenario)
        engine.reset(104)
        red, blue = _simultaneous_fire_actions(engine)

        result = engine.step([red, blue], delta_time_ms=1_000)

        assert [decision.accepted for decision in result.decisions] == [True, True]
        assert sum(
            event.event_type is EventType.COMBAT_RESULT_LOOKED_UP
            for event in result.events
        ) == 2
        death_facts = tuple(
            event for event in result.events
            if event.event_type is EventType.UNIT_DESTROYED
            and event.target_id == 1
        )
        assert len(death_facts) == 1
        fact = death_facts[0]
        assert fact.data["destruction_fact_policy"] == (
            "ONE_EVENT_PER_OPERATOR_PER_COMMIT"
        )
        assert fact.data["primary_reason_code"] is None
        assert fact.data["cause_codes"] == [
            "LOITERING_MUNITION_CONSUMED",
            "FIRE_COMBAT_DESTROYED",
        ]
        assert [item["cause_code"] for item in fact.data["causes"]] == (
            fact.data["cause_codes"]
        )
        nested_events = [item["event"] for item in fact.data["causes"]]
        assert all(
            item["event_type"] == EventType.UNIT_DESTROYED.value
            for item in nested_events
        )
        assert all(item["data"].get("adjudication_id") for item in nested_events)
        loiter = engine._require_world().operators[1]
        assert loiter.alive is False
        assert loiter.blood == 0
        assert loiter.health == 0
        assert loiter.state == "destroyed"
        assert loiter.lifecycle_remaining_ms == 0
        green = SourceFrameExporter().export(
            engine._require_world(),
            template=source_frame_template_for_scenario(scenario),
            actions=[action.to_dict() for action in result.actions],
            adjudication={
                "events": [event.to_dict() for event in result.events]
            },
        )
        records.append((
            canonical_hash(engine._require_world().to_dict()),
            canonical_hash(green),
            engine.snapshot()["rng"],
            tuple(event.to_dict() for event in result.events),
        ))

    assert records[0] == records[1]


def test_combat_destruction_before_lifecycle_advance_does_not_double_expire() -> None:
    scenario = _simultaneous_destruction_scenario()
    engine = NativeEngine(scenario)
    engine.reset(57)
    blue = next(
        value.action for value in engine.valid_actions("blue")
        if value.action.actor_id == 2
        and value.action.action_type is ActionType.SHOOT
        and value.action.target_id == 1
    )

    result = engine.step([blue], delta_time_ms=1_200_000)

    assert result.decisions[0].accepted is True
    death_facts = tuple(
        event for event in result.events
        if event.event_type is EventType.UNIT_DESTROYED
        and event.target_id == 1
    )
    assert len(death_facts) == 1
    assert death_facts[0].data.get("reason_code") != "LIFECYCLE_EXPIRED"
    assert engine._require_world().operators[1].alive is False
    # Fire resolution precedes time advancement. The later lifecycle pass
    # ignores dead operators, so no expiry fact is emitted or timer consumed.
    assert engine._require_world().operators[1].lifecycle_remaining_ms == 1_200_000


def test_simultaneous_destruction_strict_replay_at_subsecond_step(
    tmp_path: Path,
) -> None:
    scenario = _simultaneous_destruction_scenario()
    engine = NativeEngine(scenario)
    engine.reset(104)
    output = tmp_path / "replay"
    writer = ReplayWriter(
        output,
        run_id="loitering-simultaneous-subsecond",
        scenario=scenario,
        seed=104,
        agents={"red": "test", "blue": "test"},
        max_ticks=5_000,
        ruleset=load_ruleset(),
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    red, blue = _simultaneous_fire_actions(engine)
    result = engine.step([red, blue], delta_time_ms=1)
    writer.record_step(result, engine.snapshot())
    writer.finalize({"result": engine.result().to_dict()}, status="complete")

    death_facts = tuple(
        event for event in result.events
        if event.event_type is EventType.UNIT_DESTROYED
        and event.target_id == 1
    )
    assert len(death_facts) == 1
    assert death_facts[0].data["cause_codes"] == [
        "LOITERING_MUNITION_CONSUMED",
        "FIRE_COMBAT_DESTROYED",
    ]
    assert [path.name for path in sorted((output / "snapshots").glob("*.json"))] == [
        "step-00000000.json",
        "step-00000001.json",
    ]
    verification = replay_actions(scenario, output, strict=True, seed=104)
    assert verification.matches is True


@pytest.mark.parametrize(
    ("delta_times_ms", "expected_elapsed_ms"),
    [
        ([1], [0, 1]),
        ([1, 1, 997], [0, 1, 2, 999]),
        ([500, 500, 500], [0, 500, 1_000, 1_500]),
        ([1, 998, 1, 1, 3_999], [0, 1, 999, 1_000, 1_001, 5_000]),
    ],
)
def test_subsecond_replay_snapshot_identity_is_unique_and_strict(
    tmp_path: Path,
    delta_times_ms: list[int],
    expected_elapsed_ms: list[int],
) -> None:
    scenario = _scenario()
    output = tmp_path / "replay"
    _record_empty_replay(scenario, output, delta_times_ms)

    paths = sorted((output / "snapshots").glob("*.json"))
    assert [path.name for path in paths] == [
        f"step-{index:08d}.json" for index in range(len(expected_elapsed_ms))
    ]
    snapshots = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    assert [item["world"]["elapsed_time_ms"] for item in snapshots] == (
        expected_elapsed_ms
    )
    assert replay_actions(scenario, output, strict=True, seed=42).matches is True


def test_one_millisecond_lifecycle_expiry_replay_keeps_both_snapshots(
    tmp_path: Path,
) -> None:
    scenario = _scenario(lifecycle_remaining_ms=1)
    output = tmp_path / "replay"
    engine = _record_empty_replay(scenario, output, [1])

    assert [path.name for path in sorted((output / "snapshots").glob("*.json"))] == [
        "step-00000000.json",
        "step-00000001.json",
    ]
    unit = engine._require_world().operators[1]
    assert unit.alive is False
    assert unit.lifecycle_remaining_ms == 0
    assert replay_actions(scenario, output, strict=True, seed=42).matches is True


def test_subsecond_snapshot_order_is_deterministic_across_recordings(
    tmp_path: Path,
) -> None:
    scenario = _scenario()
    outputs = [tmp_path / "first", tmp_path / "second"]
    for output in outputs:
        _record_empty_replay(scenario, output, [1, 998, 1, 1, 3_999])

    for relative in (
        "actions.jsonl",
        "events.jsonl",
        "steps.jsonl",
        "episode_summary.json",
    ):
        assert (outputs[0] / relative).read_bytes() == (
            outputs[1] / relative
        ).read_bytes()
    first_snapshots = sorted((outputs[0] / "snapshots").glob("*.json"))
    second_snapshots = sorted((outputs[1] / "snapshots").glob("*.json"))
    assert [path.name for path in first_snapshots] == [
        path.name for path in second_snapshots
    ]
    assert [path.read_bytes() for path in first_snapshots] == [
        path.read_bytes() for path in second_snapshots
    ]
    assert replay_actions(scenario, outputs[0], strict=True, seed=42).matches
    assert replay_actions(scenario, outputs[1], strict=True, seed=42).matches


def test_movement_cut_off_by_lifecycle_boundary_strict_replay(
    tmp_path: Path,
) -> None:
    scenario = _scenario(lifecycle_remaining_ms=7_000)
    engine = NativeEngine(scenario)
    engine.reset(42)
    output = tmp_path / "movement-near-expiry"
    writer = ReplayWriter(
        output,
        run_id="loitering-movement-near-expiry",
        scenario=scenario,
        seed=42,
        agents={"red": "test", "blue": "test"},
        max_ticks=5_000,
        ruleset=load_ruleset(),
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    result = engine.step([_move([102])], delta_time_ms=8_000)
    writer.record_step(result, engine.snapshot())
    writer.finalize({"result": engine.result().to_dict()}, status="complete")

    loiter = engine._require_world().operators[1]
    assert loiter.current_hex == 101
    assert loiter.alive is False
    assert not any(
        event.event_type is EventType.ARRIVED_HEX and event.actor_id == 1
        for event in result.events
    )
    assert replay_actions(scenario, output, strict=True, seed=42).matches is True


def test_illegal_attack_at_expiry_boundary_strict_replay(tmp_path: Path) -> None:
    scenario = _scenario(lifecycle_remaining_ms=1_000)
    engine = NativeEngine(scenario)
    engine.reset(42)
    output = tmp_path / "illegal-attack-near-expiry"
    writer = ReplayWriter(
        output,
        run_id="loitering-illegal-attack-near-expiry",
        scenario=scenario,
        seed=42,
        agents={"red": "test", "blue": "test"},
        max_ticks=5_000,
        ruleset=load_ruleset(),
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    rng_before = engine.snapshot()["rng"]
    forged = EngineAction(
        "red",
        1,
        ActionType.SHOOT,
        target_id=2,
        parameters={"weapon_id": 76},
    )
    result = engine.step([forged], delta_time_ms=1_000)
    writer.record_step(result, engine.snapshot())
    writer.finalize({"result": engine.result().to_dict()}, status="complete")

    assert result.decisions[0].accepted is False
    assert engine.snapshot()["rng"] == rng_before
    death_facts = tuple(
        event for event in result.events
        if event.event_type is EventType.UNIT_DESTROYED
        and event.target_id == 1
    )
    assert len(death_facts) == 1
    assert death_facts[0].data["reason_code"] == "LIFECYCLE_EXPIRED"
    assert replay_actions(scenario, output, strict=True, seed=42).matches is True


def test_simultaneous_target_destruction_aggregates_and_strict_replays(
    tmp_path: Path,
) -> None:
    scenario = _scenario(target_blood=1)
    engine = NativeEngine(scenario)
    engine.reset(7)
    output = tmp_path / "simultaneous-target-destruction"
    writer = ReplayWriter(
        output,
        run_id="loitering-simultaneous-target-destruction",
        scenario=scenario,
        seed=7,
        agents={"red": "test", "blue": "test"},
        max_ticks=5_000,
        ruleset=load_ruleset(),
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    move = next(
        value.action for value in engine.valid_actions("red")
        if value.action.actor_id == 1
        and value.action.action_type is ActionType.MOVE
        and value.action.parameters["path"] == [102]
    )
    move_result = engine.step([move], delta_time_ms=8_000)
    writer.record_step(move_result, engine.snapshot())
    attacks = tuple(
        value.action for value in engine.valid_actions("red")
        if value.action.action_type is ActionType.SHOOT
        and value.action.target_id == 2
        and value.action.actor_id in {1, 3}
    )
    assert [action.actor_id for action in attacks] == [1, 3]
    attack_result = engine.step(attacks, delta_time_ms=1_000)
    writer.record_step(attack_result, engine.snapshot())
    writer.finalize({"result": engine.result().to_dict()}, status="complete")

    target_facts = tuple(
        event for event in attack_result.events
        if event.event_type is EventType.UNIT_DESTROYED
        and event.target_id == 2
    )
    assert len(target_facts) == 1
    assert len(target_facts[0].data["aggregate_adjudication_ids"]) == 2
    assert engine._require_world().operators[2].blood == 0
    assert replay_actions(scenario, output, strict=True, seed=7).matches is True


def test_legacy_operator_serialization_is_unchanged() -> None:
    document = _scenario_document()
    operator = scenario_from_dict(document).operators[0]

    assert "lifecycle_profile_id" not in operator.to_dict()
    assert "lifecycle_remaining_ms" not in operator.to_dict()
