from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path

import pytest

from wargame_native_engine.adjudication.boarding import BoardRequest
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.boarding import (
    BoardCapability,
    BoardingCapabilityStatus,
    BoardingTransitionStatus,
    BoardingTransitionType,
)
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.io.replay import ReplayWriter, replay_actions
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.rules.boarding import (
    default_boarding_profiles_path,
    default_boarding_type_assignments_path,
    load_boarding_type_assignment_authority,
)


PROFILE_ID = "GROUND_PERSONNEL_SINGLE_V1"


def test_boarding_type_assignment_resources_load_from_default_paths() -> None:
    assert default_boarding_profiles_path().is_file()
    assignments_path = default_boarding_type_assignments_path()
    assert assignments_path.is_file()

    authority = load_boarding_type_assignment_authority()

    assert authority.content_hash == canonical_hash(
        json.loads(assignments_path.read_text(encoding="utf-8"))
    )
    assert authority.profile_id_for(
        "PERSONNEL", "source-1-2", "PASSENGER"
    ) == PROFILE_ID
    assert authority.profile_id_for(
        "VEHICLE", "source-2-1", "CARRIER"
    ) == PROFILE_ID


def _type_derived_scenario_record() -> dict[str, object]:
    value = _scenario_record(carrier_profile=None)
    value["scenario_id"] = "type-derived-boarding-public-fixture"
    value["map"]["map_id"] = "type-derived-boarding-other-map"
    operators = value["operators"]
    assert isinstance(operators, list)
    operators[0]["subtype"] = "source-1-2"
    operators[1]["subtype"] = "source-2-1"
    return value


def _operator(
    operator_id: int,
    side: str,
    domain: str,
    current_hex: int,
    *,
    boarding_profile_id: str | None = None,
    stopped: bool = True,
    suppressed: bool = False,
) -> dict[str, object]:
    row, column = divmod(current_hex, 100)
    value: dict[str, object] = {
        "operator_id": operator_id,
        "side": side,
        "operator_type": domain,
        "subtype": "fixture",
        "x": column,
        "y": row,
        "altitude": 0,
        "heading": 0,
        "health": 100,
        "max_health": 100,
        "blood": 1,
        "max_blood": 1,
        "speed": 0 if stopped else 10,
        "max_speed": 36,
        "basic_speed": 36,
        "move_range": 0,
        "sensor_range": 10,
        "state": "SUPPRESSED" if suppressed else "STOPPED",
        "alive": True,
        "deployed": True,
        "weapon_ids": [],
        "current_hex": current_hex,
        "cur_pos_millionths": 0,
        "move_path": [],
        "movement_mode": "NORMAL",
        "stopped": stopped,
        "weapon_unfolded": True,
        "suppression_remaining_ms": 1_000 if suppressed else 0,
        "raw_attributes": {"armor_class": "LIGHT", "ammunition": {}},
    }
    if boarding_profile_id is not None:
        value["boarding_profile_id"] = boarding_profile_id
    if not stopped:
        value.update({
            "segment_from_hex": current_hex,
            "segment_to_hex": 102,
            "segment_elapsed_ms": 0,
            "segment_traversal_ms": 10_000,
            "segment_event_id": "moving-fixture",
        })
    return value


def _scenario_record(
    *,
    passenger_hex: int = 101,
    carrier_hex: int = 101,
    carrier_stopped: bool = True,
    passenger_suppressed: bool = False,
    carrier_suppressed: bool = False,
    carrier_profile: str | None = PROFILE_ID,
) -> dict[str, object]:
    operators = [
        _operator(
            1,
            "red",
            "PERSONNEL",
            passenger_hex,
            suppressed=passenger_suppressed,
        ),
        _operator(
            2,
            "red",
            "VEHICLE",
            carrier_hex,
            boarding_profile_id=carrier_profile,
            stopped=carrier_stopped,
            suppressed=carrier_suppressed,
        ),
        _operator(3, "blue", "VEHICLE", 102),
    ]
    deployment = {"red": {}, "blue": {}}
    for operator in operators:
        deployment[str(operator["side"])][str(operator["operator_id"])] = [
            operator["x"],
            operator["y"],
        ]
    return {
        "schema_version": "native-scenario/1.0",
        "scenario_id": "boarding-contract-test",
        "initial_stage": "combat",
        "map": {
            "map_id": "boarding-two-hex-map",
            "width": 2,
            "height": 2,
            "distance_unit": "hex",
            "blocked_positions": [],
            "hex_size_m": 200,
            "elevation_unit_m": 20,
            "elevation_kind": "LEVEL",
            "roadblocks": [],
            "hexes": [
                {
                    "pos": 101,
                    "elev": 0,
                    "cond": 0,
                    "neighbors": [102, -1, -1, -1, -1, -1],
                    "roads": [0, 0, 0, 0, 0, 0],
                    "rivers": [0, 0, 0, 0, 0, 0],
                },
                {
                    "pos": 102,
                    "elev": 0,
                    "cond": 0,
                    "neighbors": [-1, -1, -1, 101, -1, -1],
                    "roads": [0, 0, 0, 0, 0, 0],
                    "rivers": [0, 0, 0, 0, 0, 0],
                },
            ],
        },
        "sides": ["red", "blue"],
        "operators": operators,
        "objectives": [],
        "deployment": {"positions": deployment},
        "terminal_rules": {
            "max_ticks": 300,
            "elimination": True,
            "objective_score_to_win": None,
        },
        "metadata": {
            "fixture_type": "project_authored_boarding_contract",
            "source_compatible": {"weapons": {}},
        },
    }


def _scenario_record_with_passenger_runtime() -> dict[str, object]:
    value = _scenario_record()
    operators = value["operators"]
    assert isinstance(operators, list)
    passenger = next(
        item for item in operators
        if isinstance(item, dict) and item["operator_id"] == 1
    )
    passenger.update({
        "fatigue_level": 2,
        "fatigue_recovery_elapsed_ms": 10_000,
        "weapon_cooldown_remaining_ms": 200_000,
        "guided_fire_recovery_remaining_ms": 200_000,
        "indirect_fire_planned_cooldown_remaining_ms": 300_000,
    })
    return value


def _engine(**kwargs: object) -> NativeEngine:
    engine = NativeEngine(scenario_from_dict(_scenario_record(**kwargs)))
    engine.reset(42)
    return engine


def _board(engine: NativeEngine, *, delta_time_ms: int = 1_000):
    capability = engine.get_board_capabilities("red")[0]
    return engine.step([engine.board_action(capability)], delta_time_ms=delta_time_ms)


def test_same_hex_explicit_profile_generates_frozen_board_capability() -> None:
    engine = _engine()

    capabilities = engine.get_board_capabilities("red")

    assert len(capabilities) == 1
    assert capabilities[0].passenger_id == 1
    assert capabilities[0].carrier_id == 2
    assert capabilities[0].profile_id == PROFILE_ID
    with pytest.raises(FrozenInstanceError):
        capabilities[0].carrier_id = 9  # type: ignore[misc]


def test_exact_unit_types_enable_cross_scenario_board_and_unload_roundtrip() -> None:
    scenario = scenario_from_dict(_type_derived_scenario_record())
    original_hash = scenario.scenario_hash
    engine = NativeEngine(scenario)
    engine.reset(42)

    capabilities = engine.get_board_capabilities("red")
    assert [(value.passenger_id, value.carrier_id) for value in capabilities] == [
        (1, 2)
    ]
    assert scenario.scenario_hash == original_hash
    assert engine.snapshot()["boarding_type_assignment_authority"][
        "assignment_policy"
    ] == "EXPLICIT_PROFILE_THEN_EXACT_OPERATOR_TYPE"

    boarded = engine.step(
        [engine.board_action(capabilities[0])], delta_time_ms=75_000
    )
    assert boarded.decisions[0].accepted is True
    assert engine._world.operators[1].mounted_on == 2  # type: ignore[union-attr]  # noqa: SLF001
    assert engine._world.operators[2].passenger_id == 1  # type: ignore[union-attr]  # noqa: SLF001
    unloaded = engine.step(
        [engine.unload_action(engine.get_unload_capabilities("red")[0])],
        delta_time_ms=75_000,
    )
    assert unloaded.decisions[0].accepted is True
    assert engine._world.operators[1].mounted_on is None  # type: ignore[union-attr]  # noqa: SLF001
    assert engine._world.operators[1].current_hex == 101  # type: ignore[union-attr]  # noqa: SLF001


def test_type_assignment_does_not_turn_artillery_or_unknown_vehicle_into_carrier() -> None:
    value = _type_derived_scenario_record()
    operators = value["operators"]
    assert isinstance(operators, list)
    operators[1]["subtype"] = "source-2-3"
    engine = NativeEngine(scenario_from_dict(value))
    engine.reset(42)

    assert engine.get_board_capabilities("red") == ()
    assessment = next(
        item for item in engine.get_boarding_capability_assessments("red")
        if item.actor_id == 1 and item.operation_type is BoardingTransitionType.BOARD
    )
    assert assessment.status is BoardingCapabilityStatus.BLOCKED
    assert assessment.blockers == ("NO_COMPATIBLE_CARRIER",)


def test_explicit_disable_prevents_type_assignment_fallback() -> None:
    value = _type_derived_scenario_record()
    operators = value["operators"]
    assert isinstance(operators, list)
    operators[1]["boarding_profile_id"] = "DISABLED"
    engine = NativeEngine(scenario_from_dict(value))
    engine.reset(42)

    assert engine.get_board_capabilities("red") == ()


def test_full_type_derived_carrier_reports_known_blocker() -> None:
    value = _type_derived_scenario_record()
    operators = value["operators"]
    assert isinstance(operators, list)
    second = dict(operators[0])
    second["operator_id"] = 4
    operators.append(second)
    positions = value["deployment"]["positions"]
    positions["red"]["4"] = [1, 1]
    engine = NativeEngine(scenario_from_dict(value))
    engine.reset(42)
    engine.step(
        [engine.board_action(engine.get_board_capabilities("red")[0])],
        delta_time_ms=75_000,
    )

    assessment = next(
        item for item in engine.get_boarding_capability_assessments("red")
        if item.actor_id == 4 and item.operation_type is BoardingTransitionType.BOARD
    )
    assert assessment.status is BoardingCapabilityStatus.BLOCKED
    assert assessment.blockers == ("CARRIER_FULL",)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"passenger_hex": 102},
        {"carrier_stopped": False},
        {"passenger_suppressed": True},
        {"carrier_suppressed": True},
        {"carrier_profile": None},
    ],
)
def test_illegal_or_unknown_boarding_fails_closed(kwargs: dict[str, object]) -> None:
    assert _engine(**kwargs).get_board_capabilities("red") == ()


def test_board_immediately_mounts_and_locks_then_removes_passenger_actions() -> None:
    engine = _engine()

    result = _board(engine)
    world = engine._world
    assert world is not None

    assert result.decisions[0].accepted is True
    assert world.operators[1].state == "ON_BOARD"
    assert world.operators[1].mounted_on == 2
    assert world.operators[1].deployed is False
    assert world.operators[1].current_hex is None
    assert world.operators[2].passenger_id == 1
    assert world.operators[2].movement_locked is True
    assert len(world.boarding_transitions) == 1
    assert world.boarding_transitions[0].transition_type is (
        BoardingTransitionType.BOARD
    )
    active = tuple(
        value for value in world.boarding_transitions
        if value.status is BoardingTransitionStatus.ACTIVE
    )
    assert len(active) == 1
    assert active[0].remaining_ms == 74_000
    assert all(
        item.action.actor_id != 1
        for item in engine.valid_actions("red")
    )
    assert {
        item.action.action_type
        for item in engine.valid_actions("red")
        if item.action.actor_id == 2
    } == {ActionType.WAIT}


def test_unload_keeps_passenger_on_board_until_75_second_completion() -> None:
    engine = _engine()
    _board(engine)
    engine.step([], delta_time_ms=74_000)

    capabilities = engine.get_unload_capabilities("red")
    assert len(capabilities) == 1
    started = engine.step(
        [engine.unload_action(capabilities[0])],
        delta_time_ms=1_000,
    )
    world = engine._world
    assert world is not None
    assert started.decisions[0].accepted is True
    assert world.operators[1].mounted_on == 2
    assert world.operators[1].deployed is False
    active = tuple(
        value for value in world.boarding_transitions
        if value.status is BoardingTransitionStatus.ACTIVE
    )
    assert len(active) == 1
    assert active[0].remaining_ms == 74_000

    completed = engine.step([], delta_time_ms=74_000)
    assert world.operators[1].mounted_on is None
    assert world.operators[1].deployed is True
    assert world.operators[1].current_hex == world.operators[2].current_hex
    assert world.operators[1].state == "ACTIVE"
    assert world.operators[2].passenger_id is None
    assert world.operators[2].movement_locked is False
    assert len(world.boarding_transitions) == 2
    assert all(
        value.status is BoardingTransitionStatus.COMPLETED
        and value.remaining_ms == 0
        and value.completed_tick is not None
        for value in world.boarding_transitions
    )
    assert any(
        event.event_type is EventType.UNLOAD_COMPLETED
        for event in completed.events
    )


def test_snapshot_restore_preserves_relation_timer_and_hash() -> None:
    engine = _engine()
    _board(engine)
    snapshot = engine.snapshot()
    expected_hash = canonical_hash(snapshot["world"])

    restored = NativeEngine(engine.scenario)
    restored.restore(snapshot)

    assert restored.snapshot()["world"] == snapshot["world"]
    assert canonical_hash(restored.snapshot()["world"]) == expected_hash


def test_board_commit_preserves_and_advances_passenger_runtime_state() -> None:
    engine = NativeEngine(
        scenario_from_dict(_scenario_record_with_passenger_runtime())
    )
    engine.reset(42)

    _board(engine, delta_time_ms=1_000)
    world = engine._world
    assert world is not None
    passenger = world.operators[1]

    assert passenger.current_hex is None
    assert passenger.state == "ON_BOARD"
    assert passenger.fatigue_level == 2
    assert passenger.fatigue_recovery_elapsed_ms == 11_000
    assert passenger.weapon_cooldown_remaining_ms == 199_000
    assert passenger.guided_fire_recovery_remaining_ms == 199_000
    assert passenger.indirect_fire_planned_cooldown_remaining_ms == 299_000
    assert all(
        item.action.actor_id != passenger.operator_id
        for item in engine.valid_actions("red")
    )

    snapshot = engine.snapshot()
    serialized = snapshot["world"]["operators"]["1"]
    assert serialized["fatigue_level"] == 2
    assert serialized["fatigue_recovery_elapsed_ms"] == 11_000
    assert serialized["weapon_cooldown_remaining_ms"] == 199_000
    assert serialized["guided_fire_recovery_remaining_ms"] == 199_000
    assert serialized["indirect_fire_planned_cooldown_remaining_ms"] == 299_000

    restored = NativeEngine(engine.scenario)
    restored.restore(snapshot)
    assert restored.snapshot() == snapshot
    restored.step([], delta_time_ms=1_000)
    restored_passenger = restored._world.operators[1]  # type: ignore[union-attr]
    assert restored_passenger.fatigue_recovery_elapsed_ms == 12_000
    assert restored_passenger.weapon_cooldown_remaining_ms == 198_000
    assert restored_passenger.guided_fire_recovery_remaining_ms == 198_000
    assert (
        restored_passenger.indirect_fire_planned_cooldown_remaining_ms
        == 298_000
    )


def test_board_unload_roundtrip_preserves_state_except_authoritative_advance() -> None:
    engine = NativeEngine(
        scenario_from_dict(_scenario_record_with_passenger_runtime())
    )
    engine.reset(42)

    _board(engine, delta_time_ms=1_000)
    engine.step([], delta_time_ms=74_000)
    on_board = engine._world.operators[1]  # type: ignore[union-attr]
    assert on_board.state == "ON_BOARD"
    assert on_board.weapon_cooldown_remaining_ms == 125_000
    assert on_board.guided_fire_recovery_remaining_ms == 125_000
    assert on_board.indirect_fire_planned_cooldown_remaining_ms == 225_000
    assert on_board.fatigue_level == 1
    assert on_board.fatigue_recovery_elapsed_ms == 10_000

    unload = engine.unload_action(engine.get_unload_capabilities("red")[0])
    engine.step([unload], delta_time_ms=1_000)
    engine.step([], delta_time_ms=74_000)
    passenger = engine._world.operators[1]  # type: ignore[union-attr]
    carrier = engine._world.operators[2]  # type: ignore[union-attr]

    assert passenger.state == "ACTIVE"
    assert passenger.current_hex == carrier.current_hex
    assert passenger.mounted_on is None
    assert passenger.weapon_cooldown_remaining_ms == 50_000
    assert passenger.guided_fire_recovery_remaining_ms == 50_000
    assert passenger.indirect_fire_planned_cooldown_remaining_ms == 150_000
    assert passenger.fatigue_level == 0
    assert passenger.fatigue_recovery_elapsed_ms == 0


def test_forged_request_is_rejected_without_state_change() -> None:
    engine = _engine()
    capability = engine.get_board_capabilities("red")[0]
    built = engine.build_board_request(capability)
    assert built.request is not None
    forged: BoardRequest = replace(built.request, carrier_id=3)
    before = canonical_hash(engine.snapshot())

    result = engine.validate_board_request(forged)

    assert result.valid is False
    assert canonical_hash(engine.snapshot()) == before


def test_board_action_is_recorded_and_strictly_reexecuted(tmp_path: Path) -> None:
    scenario = scenario_from_dict(_scenario_record())
    engine = NativeEngine(scenario)
    engine.reset(42)
    writer = ReplayWriter(
        tmp_path / "boarding-replay",
        run_id="boarding-contract-replay",
        scenario=scenario,
        seed=42,
        agents={"red": "test", "blue": "test"},
        max_ticks=engine.terminal_rules.max_ticks,
        ruleset=engine.ruleset,
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    result = _board(engine)
    writer.record_step(result, engine.snapshot())
    result = engine.step([], delta_time_ms=74_000)
    writer.record_step(result, engine.snapshot())
    unload = engine.unload_action(engine.get_unload_capabilities("red")[0])
    result = engine.step([unload], delta_time_ms=1_000)
    writer.record_step(result, engine.snapshot())
    result = engine.step([], delta_time_ms=74_000)
    writer.record_step(result, engine.snapshot())
    expected_hash = canonical_hash(engine.snapshot()["world"])
    writer.finalize({"result": engine.result().to_dict()}, status="complete")

    verification = replay_actions(scenario, writer.root, strict=True)

    assert verification.matches is True
    assert verification.expected_world_hash == expected_hash


def test_board_runtime_state_strictly_reexecutes(tmp_path: Path) -> None:
    scenario = scenario_from_dict(_scenario_record_with_passenger_runtime())
    engine = NativeEngine(scenario)
    engine.reset(42)
    writer = ReplayWriter(
        tmp_path / "boarding-runtime-replay",
        run_id="boarding-runtime-replay",
        scenario=scenario,
        seed=42,
        agents={"red": "test", "blue": "test"},
        max_ticks=engine.terminal_rules.max_ticks,
        ruleset=engine.ruleset,
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    result = _board(engine, delta_time_ms=1_000)
    writer.record_step(result, engine.snapshot())
    result = engine.step([], delta_time_ms=74_000)
    writer.record_step(result, engine.snapshot())
    unload = engine.unload_action(engine.get_unload_capabilities("red")[0])
    result = engine.step([unload], delta_time_ms=1_000)
    writer.record_step(result, engine.snapshot())
    result = engine.step([], delta_time_ms=74_000)
    writer.record_step(result, engine.snapshot())
    expected_hash = canonical_hash(engine.snapshot()["world"])
    writer.finalize({"result": engine.result().to_dict()}, status="complete")

    verification = replay_actions(scenario, writer.root, strict=True)

    assert verification.matches is True
    assert verification.expected_world_hash == expected_hash


def test_type_derived_boarding_strict_replay_records_assignment_identity(
    tmp_path: Path,
) -> None:
    scenario = scenario_from_dict(_type_derived_scenario_record())
    engine = NativeEngine(scenario)
    engine.reset(42)
    writer = ReplayWriter(
        tmp_path / "type-derived-boarding-replay",
        run_id="type-derived-boarding-replay",
        scenario=scenario,
        seed=42,
        agents={"red": "test", "blue": "test"},
        max_ticks=engine.terminal_rules.max_ticks,
        ruleset=engine.ruleset,
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    writer.record_step(_board(engine), engine.snapshot())
    writer.record_step(engine.step([], delta_time_ms=74_000), engine.snapshot())
    writer.finalize({"result": engine.result().to_dict()}, status="complete")

    verification = replay_actions(scenario, writer.root, strict=True)
    assert verification.matches is True
    assert "boarding_type_assignment_authority" in engine.snapshot()


def test_legacy_explicit_only_replay_does_not_gain_type_assignments(
    tmp_path: Path,
) -> None:
    scenario = scenario_from_dict(_scenario_record())
    engine = NativeEngine(
        scenario,
        enable_boarding_type_assignments=False,
    )
    engine.reset(42)
    writer = ReplayWriter(
        tmp_path / "legacy-explicit-boarding-replay",
        run_id="legacy-explicit-boarding-replay",
        scenario=scenario,
        seed=42,
        agents={"red": "test", "blue": "test"},
        max_ticks=engine.terminal_rules.max_ticks,
        ruleset=engine.ruleset,
        project_commit=None,
    )
    initial = engine.snapshot()
    assert "boarding_type_assignment_authority" not in initial
    writer.record_initial_state(initial)
    writer.record_step(_board(engine), engine.snapshot())
    writer.finalize({"result": engine.result().to_dict()}, status="complete")

    assert replay_actions(scenario, writer.root, strict=True).matches is True


def test_board_and_unload_action_types_do_not_change_fire_contracts() -> None:
    assert ActionType.SHOOT.value == "SHOOT"
    assert ActionType.FIRE.value == "FIRE"
    assert ActionType.BOARD.value == "BOARD"
    assert ActionType.UNLOAD.value == "UNLOAD"
    assert EngineAction.from_dict({
        "side": "red",
        "actor_id": 1,
        "action_type": "SHOOT",
        "target_id": 3,
        "target_position": None,
        "parameters": {"weapon_id": 36},
    }).action_type is ActionType.SHOOT
