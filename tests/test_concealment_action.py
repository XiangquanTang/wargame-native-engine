from __future__ import annotations

from pathlib import Path

from scripts.run_demo import run_registered_demo
from tests.test_guided_capability import _engine as _guided_engine
from tests.test_source_movement import (
    _engine as _movement_engine,
    _move,
    _scenario_document,
)
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.concealment import ConcealmentPosture
from wargame_native_engine.core.event import EventType
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.core.engine import NativeEngine


def _conceal_action(engine: NativeEngine, actor_id: int) -> EngineAction:
    return next(
        value.action
        for value in engine.valid_actions("red")
        if value.action.actor_id == actor_id
        and value.action.action_type is ActionType.ENTER_CONCEALMENT
    )


def _capability(engine: NativeEngine, actor_id: int):
    return next(
        value
        for value in engine.get_concealment_capabilities("red")
        if value.operator_id == actor_id
    )


def test_ground_unit_enters_concealment_atomically() -> None:
    engine = _movement_engine(mover_domain="PERSONNEL")
    before = engine.snapshot()

    result = engine.step([_conceal_action(engine, 1)], delta_time_ms=1_000)

    operator = engine._require_world().operators[1]
    assert result.decisions[0].accepted is True
    assert operator.concealment_posture is ConcealmentPosture.CONCEALED
    assert operator.raw_attributes["concealed"] is True
    event = next(
        value for value in result.events
        if value.event_type is EventType.UNIT_CONCEALED
    )
    assert event.data["previous_posture"] == "EXPOSED"
    assert event.data["new_posture"] == "CONCEALED"
    assert engine.snapshot()["rng"] == before["rng"]


def test_air_unit_is_rejected_without_mutation() -> None:
    engine = _movement_engine(mover_domain="AIR")
    capability = _capability(engine, 1)
    before_world = canonical_hash(engine._require_world().to_dict())
    before_rng = engine.snapshot()["rng"]
    action = EngineAction("red", 1, ActionType.ENTER_CONCEALMENT)

    result = engine.step([action], delta_time_ms=1_000)

    assert capability.available is False
    assert capability.reason == "AIR_UNIT_NOT_SUPPORTED"
    assert result.decisions[0].accepted is False
    assert result.decisions[0].code == "AIR_UNIT_NOT_SUPPORTED"
    # A rejected action still advances the fixed tick, but posture and RNG do
    # not change and no concealment event is published.
    assert engine._require_world().operators[1].concealed is False
    assert engine.snapshot()["rng"] == before_rng
    assert not any(
        value.event_type is EventType.UNIT_CONCEALED
        for value in result.events
    )
    assert canonical_hash(engine._require_world().to_dict()) != before_world


def test_moving_unit_is_blocked() -> None:
    engine = _movement_engine(mover_domain="PERSONNEL")
    engine.step([_move([102])], delta_time_ms=1_000)

    capability = _capability(engine, 1)

    assert capability.available is False
    assert capability.reason == "MOVING"


def test_suppressed_unit_is_blocked() -> None:
    record = _scenario_document(mover_domain="PERSONNEL")
    record["operators"][0]["suppression_remaining_ms"] = 1_000
    record["operators"][0]["state"] = "SUPPRESSED"
    engine = NativeEngine(scenario_from_dict(record))
    engine.reset(42)

    capability = _capability(engine, 1)

    assert capability.available is False
    assert capability.reason == "SUPPRESSED"


def test_on_board_unit_is_blocked() -> None:
    engine = _movement_engine(mover_domain="PERSONNEL")
    world = engine._require_world()
    passenger = world.operators[1]
    carrier = world.operators[3]
    passenger.mounted_on = carrier.operator_id
    passenger.deployed = False
    passenger.state = "ON_BOARD"
    carrier.passenger_id = passenger.operator_id

    capability = _capability(engine, 1)

    assert capability.available is False
    assert capability.reason == "ON_BOARD"


def test_successful_move_exposes_concealed_unit() -> None:
    engine = _movement_engine(mover_domain="PERSONNEL")
    engine.step([_conceal_action(engine, 1)], delta_time_ms=1_000)

    result = engine.step([_move([102])], delta_time_ms=1_000)

    assert engine._require_world().operators[1].concealed is False
    exposed = next(
        value for value in result.events
        if value.event_type is EventType.UNIT_EXPOSED
    )
    assert exposed.data["reason"] == "MOVE"


def test_successful_direct_fire_exposes_shooter() -> None:
    engine = _movement_engine(mover_domain="PERSONNEL")
    engine.step([_conceal_action(engine, 1)], delta_time_ms=1_000)
    shoot = next(
        value.action for value in engine.valid_actions("red")
        if value.action.actor_id == 1
        and value.action.action_type is ActionType.SHOOT
    )

    result = engine.step([shoot], delta_time_ms=1_000)

    assert engine._require_world().operators[1].concealed is False
    assert any(
        value.event_type is EventType.UNIT_EXPOSED
        and value.data["reason"] == "DIRECT_FIRE"
        for value in result.events
    )


def test_guided_fire_exposes_shooter_but_not_guider() -> None:
    engine = _guided_engine(target_concealed=True)
    engine.step([_conceal_action(engine, 20)], delta_time_ms=1_000)
    engine.step([_conceal_action(engine, 10)], delta_time_ms=1_000)
    capability = engine.get_guided_fire_capabilities("red")[0]

    result = engine.step(
        [engine.guided_fire_action(capability)],
        delta_time_ms=1_000,
    )

    world = engine._require_world()
    assert world.operators[10].concealed is False
    assert world.operators[20].concealed is True
    assert any(
        value.event_type is EventType.UNIT_EXPOSED
        and value.actor_id == 10
        and value.data["reason"] == "GUIDED_FIRE_SHOOTER"
        for value in result.events
    )
    assert not any(
        value.event_type is EventType.UNIT_EXPOSED
        and value.actor_id == 20
        for value in result.events
    )


def test_concealment_demo_and_strict_replay(tmp_path: Path) -> None:
    report = run_registered_demo(
        "concealment_basic",
        output_dir=tmp_path / "concealment",
        overwrite=True,
    )

    assert report["status"] == "PASS"
    assert report["strict_replay_matches"] is True
    assert (tmp_path / "concealment" / "replay.zip").is_file()
