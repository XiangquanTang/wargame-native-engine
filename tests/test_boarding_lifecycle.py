from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.run_demo import run_registered_demo
from wargame_native_engine.core.boarding import BoardingTransitionStatus
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT / "examples/demos/boarding_basic/scenario.json"


def _scenario_record() -> dict[str, object]:
    return json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))


def _engine(record: dict[str, object] | None = None) -> NativeEngine:
    engine = NativeEngine(scenario_from_dict(record or _scenario_record()))
    engine.reset(42)
    return engine


def _board(engine: NativeEngine, milliseconds: int = 1_000):
    capability = engine.get_board_capabilities("red")[0]
    return engine.step(
        [engine.board_action(capability)], delta_time_ms=milliseconds
    )


def _complete_board(engine: NativeEngine) -> None:
    _board(engine)
    engine.step([], delta_time_ms=74_000)


def _armed_record(*, carrier_blood: int) -> dict[str, object]:
    value = deepcopy(_scenario_record())
    operators = value["operators"]
    assert isinstance(operators, list)
    for operator in operators:
        assert isinstance(operator, dict)
        if operator["operator_id"] == 102:
            operator["blood"] = carrier_blood
            operator["max_blood"] = 3
            operator["health"] = 100 * carrier_blood / 3
            operator["max_health"] = 100
        if operator["operator_id"] == 201:
            operator["weapon_ids"] = [36]
            raw = operator["raw_attributes"]
            assert isinstance(raw, dict)
            raw["ammunition"] = {"0": 20}
    metadata = value["metadata"]
    assert isinstance(metadata, dict)
    source = metadata["source_compatible"]
    assert isinstance(source, dict)
    source["weapons"] = {
        "36": {
            "weapon_class": "large_direct_fire_gun",
            "maximum_range": 10,
            "target_domains": ["PERSONNEL", "VEHICLE"],
            "ammunition_key": "0",
            "requires_unfolded": True,
            "can_fire_while_moving": False,
        }
    }
    return value


def _blue_shoot_carrier(engine: NativeEngine):
    action = next(
        value.action for value in engine.valid_actions("blue")
        if value.action.action_type.value == "SHOOT"
        and value.action.actor_id == 201
        and value.action.target_id == 102
    )
    return engine.step([action], delta_time_ms=1_000)


def test_board_transition_emits_started_and_completed_at_75_seconds() -> None:
    engine = _engine()

    started = _board(engine)
    transition = engine._world.boarding_transitions[0]  # type: ignore[union-attr]
    assert transition.status is BoardingTransitionStatus.ACTIVE
    assert transition.transition_start_tick == 0
    assert transition.completed_tick is None
    assert transition.remaining_ms == 74_000
    assert any(value.event_type is EventType.BOARD_STARTED for value in started.events)

    engine.step([], delta_time_ms=73_000)
    transition = engine._world.boarding_transitions[0]  # type: ignore[union-attr]
    assert transition.status is BoardingTransitionStatus.ACTIVE
    assert transition.remaining_ms == 1_000
    completed = engine.step([], delta_time_ms=1_000)
    transition = engine._world.boarding_transitions[0]  # type: ignore[union-attr]
    assert transition.status is BoardingTransitionStatus.COMPLETED
    assert transition.remaining_ms == 0
    assert transition.completed_tick == completed.tick_after
    assert any(
        value.event_type is EventType.BOARD_COMPLETED
        for value in completed.events
    )
    engine.close()


def test_unload_transition_inherits_position_and_refreshes_actions() -> None:
    engine = _engine()
    _complete_board(engine)
    capability = engine.get_unload_capabilities("red")[0]

    started = engine.step(
        [engine.unload_action(capability)], delta_time_ms=1_000
    )
    assert any(value.event_type is EventType.UNLOAD_STARTED for value in started.events)
    assert engine._world.operators[101].state == "ON_BOARD"  # type: ignore[union-attr]
    completed = engine.step([], delta_time_ms=74_000)
    world = engine._world
    assert world is not None
    passenger = world.operators[101]
    carrier = world.operators[102]
    assert passenger.state == "ACTIVE"
    assert passenger.mounted_on is None
    assert passenger.deployed is True
    assert passenger.current_hex == carrier.current_hex
    assert (passenger.x, passenger.y) == (carrier.x, carrier.y)
    assert carrier.passenger_id is None
    assert carrier.movement_locked is False
    assert any(
        value.event_type is EventType.UNLOAD_COMPLETED
        and value.data["hex"] == carrier.current_hex
        for value in completed.events
    )
    assert any(
        value.action.actor_id == passenger.operator_id
        for value in engine.valid_actions("red")
    )
    assert 201 in next(
        value for value in engine.observe("red").friendly_operators
        if value.operator_id == passenger.operator_id
    ).observed_enemy_ids
    engine.close()


def test_destroyed_carrier_destroys_mounted_passenger() -> None:
    engine = _engine(_armed_record(carrier_blood=1))
    _board(engine)

    result = _blue_shoot_carrier(engine)
    world = engine._world
    assert world is not None
    assert world.operators[102].alive is False
    assert world.operators[101].alive is False
    assert world.operators[101].state == "destroyed"
    assert any(
        value.event_type is EventType.UNIT_DESTROYED
        and value.target_id == 101
        and value.data.get("boarding_cascade") is True
        for value in result.events
    )
    engine.close()


def test_nonlethal_carrier_damage_does_not_damage_passenger() -> None:
    engine = _engine()
    _board(engine)
    assert engine._world is not None
    working = type(engine._world).from_dict(engine._world.to_dict())
    carrier = working.operators[102]
    passenger = working.operators[101]
    passenger_before = passenger.to_dict()
    carrier.max_blood = 3
    carrier.blood = 2
    carrier.health = carrier.max_health * 2 / 3
    carrier.alive = True

    events = engine._apply_boarding_carrier_destruction(working)

    assert events == ()
    assert passenger.to_dict() == passenger_before
    assert engine._world.operators[102].blood == 1
    assert engine._world.operators[101].alive is True
    engine.close()


def test_completed_transition_history_survives_snapshot_restore() -> None:
    engine = _engine()
    _complete_board(engine)
    snapshot = engine.snapshot()
    restored = NativeEngine(engine.scenario)

    restored.restore(snapshot)

    assert restored.snapshot() == snapshot
    assert canonical_hash(restored.snapshot()) == canonical_hash(snapshot)
    assert restored._world is not None
    assert restored._world.boarding_transitions[0].status is (
        BoardingTransitionStatus.COMPLETED
    )
    engine.close()
    restored.close()


def test_boarding_basic_demo_runs_and_strictly_replays(tmp_path: Path) -> None:
    output = tmp_path / "boarding-basic"

    report = run_registered_demo(
        "boarding_basic",
        output_dir=output,
        seed=42,
    )

    assert report["status"] == "PASS"
    assert report["board_command_status"] == "APPLIED"
    assert report["unload_command_status"] == "APPLIED"
    assert report["strict_replay_matches"] is True
    for name in (
        "replay.zip",
        "evidence.json",
        "summary.json",
        "validation_report.json",
    ):
        assert (output / name).is_file()


@pytest.mark.parametrize("event_type", [
    EventType.BOARD_STARTED,
    EventType.BOARD_COMPLETED,
    EventType.UNLOAD_STARTED,
    EventType.UNLOAD_COMPLETED,
])
def test_formal_boarding_events_have_stable_wire_names(event_type: EventType) -> None:
    assert event_type.value in {
        "board_started", "board_completed", "unload_started", "unload_completed"
    }
