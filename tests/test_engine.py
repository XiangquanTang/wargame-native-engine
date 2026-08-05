from __future__ import annotations

from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.core.world import Stage
from wargame_native_engine.systems.visibility import can_see

from conftest import action_of, advance_to_combat


def test_engine_reset(scenario) -> None:
    engine = NativeEngine(scenario)
    world = engine.reset(seed=42)
    assert world.tick == 0
    assert world.stage is Stage.DEPLOYMENT
    assert not any(item.deployed for item in world.operators.values())
    assert engine.result().seed == 42


def test_deployment_transition(scenario) -> None:
    engine = NativeEngine(scenario)
    engine.reset()
    deploy = engine.step({
        side: action_of(engine, side, ActionType.DEPLOY).action
        for side in scenario.side_order})
    assert deploy.stage_after is Stage.DEPLOYMENT
    finish = engine.step({
        side: action_of(engine, side, ActionType.END_DEPLOYMENT).action
        for side in scenario.side_order})
    assert finish.stage_after is Stage.COMBAT
    assert EventType.COMBAT_STARTED in {item.event_type for item in finish.events}


def test_move_validation(scenario) -> None:
    engine = NativeEngine(scenario)
    engine.reset()
    advance_to_combat(engine)
    before = engine.snapshot()["world"]["operators"]
    result = engine.step({
        side: action_of(engine, side, ActionType.MOVE).action
        for side in scenario.side_order})
    after = engine.snapshot()["world"]["operators"]
    assert all(item.accepted for item in result.decisions)
    assert before["101"]["x"] < after["101"]["x"]
    assert before["201"]["x"] > after["201"]["x"]
    assert EventType.OPERATOR_MOVED in {item.event_type for item in result.events}


def test_invalid_move_is_rejected(scenario) -> None:
    engine = NativeEngine(scenario)
    engine.reset()
    advance_to_combat(engine)
    invalid = EngineAction("red", 101, ActionType.MOVE,
                           target_position=(10.0, 3.0))
    blue_wait = action_of(engine, "blue", ActionType.WAIT).action
    result = engine.step({"red": invalid, "blue": blue_wait})
    red = next(item for item in result.decisions
               if item.action_id == invalid.action_id)
    assert not red.accepted
    assert red.code == "action_not_in_valid_actions"
    assert engine.result().error_counts["red"] == 1


def test_visibility_boundary(scenario) -> None:
    engine = NativeEngine(scenario)
    engine.reset()
    engine.step({
        side: action_of(engine, side, ActionType.DEPLOY).action
        for side in scenario.side_order})
    snapshot = engine.snapshot()["world"]["operators"]
    red = engine._require_world().operators[101]
    blue = engine._require_world().operators[201]
    red.sensor_range = 5.0
    assert snapshot["201"]["x"] - snapshot["101"]["x"] == 5.0
    assert can_see(red, blue)
    assert [item.operator_id for item in engine.observe("red").visible_enemies] == [201]


def test_attack_damage(scenario) -> None:
    engine = NativeEngine(scenario)
    engine.reset()
    advance_to_combat(engine)
    engine.step({side: action_of(engine, side, ActionType.MOVE).action
                 for side in scenario.side_order})
    result = engine.step({side: action_of(engine, side, ActionType.ATTACK).action
                          for side in scenario.side_order})
    world = engine.snapshot()["world"]
    assert world["operators"]["101"]["health"] == 30.0
    assert world["operators"]["201"]["health"] == 20.0
    assert EventType.TARGET_HIT in {item.event_type for item in result.events}


def test_operator_destroyed(scenario) -> None:
    engine = NativeEngine(scenario)
    engine.reset()
    advance_to_combat(engine)
    engine.step({side: action_of(engine, side, ActionType.MOVE).action
                 for side in scenario.side_order})
    events = set()
    for _ in range(3):
        result = engine.step({
            side: action_of(engine, side, ActionType.ATTACK).action
            for side in scenario.side_order})
        events.update(item.event_type for item in result.events)
    assert EventType.OPERATOR_DESTROYED in events
    assert not engine.snapshot()["world"]["operators"]["201"]["alive"]


def test_terminal_when_side_eliminated(scenario) -> None:
    engine = NativeEngine(scenario)
    engine.reset()
    advance_to_combat(engine)
    while not engine.is_terminal():
        batch = {side: engine.valid_actions(side)[0].action
                 for side in scenario.side_order}
        engine.step(batch)
    result = engine.result()
    assert result.terminal and not result.truncated
    assert result.winner == "red"
    assert result.reason == "side_eliminated"


def test_max_tick_is_truncation_not_terminal(scenario) -> None:
    engine = NativeEngine(scenario, max_ticks=3)
    engine.reset()
    advance_to_combat(engine)
    engine.step({side: action_of(engine, side, ActionType.WAIT).action
                 for side in scenario.side_order})
    result = engine.result()
    assert result.truncated and not result.terminal
    assert result.winner is None and result.reason == "max_ticks"


def test_seed_reproducibility(scenario) -> None:
    snapshots = []
    for _ in range(2):
        engine = NativeEngine(scenario)
        engine.reset(seed=123)
        advance_to_combat(engine)
        engine.step({side: engine.valid_actions(side)[0].action
                     for side in scenario.side_order})
        snapshots.append(engine.snapshot())
    assert snapshots[0] == snapshots[1]


def test_raw_observation_does_not_mutate_world(scenario) -> None:
    engine = NativeEngine(scenario)
    engine.reset()
    observation = engine.observe("red")
    observation.friendly_operators[0].raw_attributes["source"] = "mutated"
    assert engine.observe("red").friendly_operators[0].raw_attributes["source"] == (
        "synthetic_example")


def test_visible_enemy_observation_does_not_expose_raw_attributes(scenario) -> None:
    engine = NativeEngine(scenario)
    engine.reset(42)
    advance_to_combat(engine)
    visible = engine.observe("red").visible_enemies
    assert visible
    assert all(item.raw_attributes == {} for item in visible)
