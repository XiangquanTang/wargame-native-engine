from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import (
    source_frame_template_for_scenario,
)
from wargame_native_engine.core.action import ACTION_PRIORITY, ActionType, EngineAction, ValidAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.gateway import DecisionWindow
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)


ROOT = Path(__file__).resolve().parents[1]


def _joint_document(*, red_id: int = 1, blue_id: int = 2) -> dict:
    value = json.loads(
        (ROOT / "examples/source_movement_scenario.json").read_text(
            encoding="utf-8"))
    value["scenario_id"] = f"joint-fire-{red_id}-{blue_id}"
    red, blue = value["operators"]
    for operator, operator_id, side, current_hex in (
        (red, red_id, "red", 101),
        (blue, blue_id, "blue", 102),
    ):
        operator.update({
            "operator_id": operator_id,
            "side": side,
            "operator_type": "VEHICLE",
            "subtype": "synthetic-joint-fire-vehicle",
            "x": current_hex % 100,
            "y": current_hex // 100,
            "health": 100,
            "max_health": 100,
            "blood": 1,
            "max_blood": 1,
            "state": "STOPPED",
            "alive": True,
            "weapon_ids": [36],
            "current_hex": current_hex,
            "sensor_range": 25,
            "weapon_unfolded": True,
        })
        operator["raw_attributes"] = {
            "armor_class": "LIGHT",
            "ammunition": {"0": 20},
        }
    value["deployment"]["positions"] = {
        "red": {str(red_id): [1, 1]},
        "blue": {str(blue_id): [2, 1]},
    }
    return value


def _shoot(side: str, actor_id: int, target_id: int) -> EngineAction:
    return EngineAction(
        side,
        actor_id,
        ActionType.SHOOT,
        target_id=target_id,
        parameters={"weapon_id": 36},
    )


def _engine(*, red_id: int = 1, blue_id: int = 2) -> NativeEngine:
    engine = NativeEngine(scenario_from_dict(
        _joint_document(red_id=red_id, blue_id=blue_id)))
    engine.reset(1)
    return engine


def _joint_batch(engine: NativeEngine, order: tuple[str, str]) -> tuple[EngineAction, ...]:
    red_id = next(item.operator_id for item in engine._require_world().operators.values()
                  if item.side == "red")
    blue_id = next(item.operator_id for item in engine._require_world().operators.values()
                   if item.side == "blue")
    actions = {
        "red": _shoot("red", red_id, blue_id),
        "blue": _shoot("blue", blue_id, red_id),
    }
    valid = {
        side: ValidAction(action, ACTION_PRIORITY[action.action_type])
        for side, action in actions.items()
    }
    window = DecisionWindow.open(
        session_id="joint",
        decision_window_index=0,
        opened_at_sim_time_ms=0,
        red_valid_actions=(valid["red"],),
        blue_valid_actions=(valid["blue"],),
    )
    for side in order:
        window.submit(
            side=side,
            request_id=f"{side}-request",
            ordered_action_ids=[actions[side].action_id],
        )
    window.close()
    return window.merged_action_batch


def _semantic_result(engine: NativeEngine) -> dict:
    return {
        item.side: {"blood": item.blood, "alive": item.alive, "state": item.state}
        for item in engine._require_world().operators.values()
    }


def test_mutual_lethal_fire_uses_one_pre_state() -> None:
    engine = _engine()
    assert engine.ruleset.combat.simultaneous_damage is True
    assert engine.ruleset.combat.destroyed_actor_executes_submitted_action is True
    assert engine.ruleset.combat.multi_attack_resolution == "aggregate_then_apply"
    result = engine.step(_joint_batch(engine, ("red", "blue")),
                         delta_time_seconds=1)

    assert all(item.accepted for item in result.decisions)
    assert _semantic_result(engine) == {
        "red": {"blood": 0, "alive": False, "state": "destroyed"},
        "blue": {"blood": 0, "alive": False, "state": "destroyed"},
    }
    assert engine.result().outcome == "draw"


def test_submission_order_and_actor_id_order_do_not_create_initiative() -> None:
    results = []
    for red_id, blue_id, order in (
        (1, 2, ("red", "blue")),
        (1, 2, ("blue", "red")),
        (20, 10, ("red", "blue")),
    ):
        engine = _engine(red_id=red_id, blue_id=blue_id)
        engine.step(_joint_batch(engine, order), delta_time_seconds=1)
        results.append((_semantic_result(engine), engine.result().outcome))
    assert results[0] == results[1] == results[2]


def test_single_sided_fire_still_consumes_ammunition_and_applies_damage() -> None:
    engine = _engine()
    result = engine.step([_shoot("red", 1, 2)], delta_time_seconds=1)

    assert result.decisions[0].accepted
    assert engine._require_world().operators[2].alive is False
    assert engine._require_world().operators[1].raw_attributes[
        "ammunition"]["0"] == 19


def test_single_sided_no_effect_fire_preserves_suppression_timer_rule() -> None:
    engine = NativeEngine(scenario_from_dict(_joint_document()))
    engine.reset(8)
    target = engine._require_world().operators[2]
    target.state = "SUPPRESSED"
    target.suppression_remaining_ms = 30_000

    result = engine.step([
        EngineAction("red", 1, ActionType.SHOOT, target_id=2,
                     parameters={"weapon_id": 36}),
    ], delta_time_seconds=1)

    assert not any(item.event_type is EventType.LOSSES_APPLIED
                   for item in result.events)
    assert target.state == "SUPPRESSED"
    assert target.suppression_remaining_ms == 149_000


def test_multiple_attacks_aggregate_once_against_pre_state() -> None:
    document = _joint_document(red_id=1, blue_id=2)
    second = deepcopy(document["operators"][0])
    second.update({"operator_id": 3, "current_hex": 103, "x": 3})
    document["operators"].append(second)
    document["deployment"]["positions"]["red"]["3"] = [3, 1]
    engine = NativeEngine(scenario_from_dict(document))
    engine.reset(2)

    result = engine.step([
        _shoot("red", 1, 2),
        _shoot("red", 3, 2),
    ], delta_time_seconds=1)

    assert all(item.accepted for item in result.decisions)
    losses = [item for item in result.events
              if item.event_type is EventType.LOSSES_APPLIED
              and item.target_id == 2]
    assert len(losses) == 1
    assert losses[0].data["resolution_policy"] == "aggregate_then_apply"
    assert losses[0].data["aggregate_requested_losses"] == 2
    assert len(losses[0].data["aggregate_adjudication_ids"]) == 2
    assert engine._require_world().operators[2].blood == 0


def _assert_transaction_rollback(engine: NativeEngine, operation) -> None:
    before = deepcopy(engine.snapshot())
    before_events = engine._last_events
    with pytest.raises(RuntimeError, match="injected"):
        operation()
    assert engine.snapshot() == before
    assert engine._last_events == before_events


def test_second_action_exception_rolls_back_world_rng_and_sequences(monkeypatch) -> None:
    engine = _engine()
    original = engine._stage_source_shoot
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected-second-action")
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "_stage_source_shoot", fail_second)
    _assert_transaction_rollback(
        engine,
        lambda: engine.step([
            _shoot("red", 1, 2),
            _shoot("blue", 2, 1),
        ], delta_time_seconds=1),
    )


def test_movement_exception_rolls_back_tick(monkeypatch) -> None:
    engine = _engine()

    def fail(*args, **kwargs):
        raise RuntimeError("injected-movement")

    monkeypatch.setattr(engine._movement, "advance", fail)
    _assert_transaction_rollback(
        engine, lambda: engine.step([], delta_time_seconds=1))


def test_visibility_exception_rolls_back_tick(monkeypatch) -> None:
    engine = _engine()

    def fail(*args, **kwargs):
        raise RuntimeError("injected-visibility")

    monkeypatch.setattr(engine, "_refresh_source_observation", fail)
    _assert_transaction_rollback(
        engine, lambda: engine.step([], delta_time_seconds=1))


def test_termination_exception_rolls_back_tick(monkeypatch) -> None:
    engine = _engine()

    def fail(*args, **kwargs):
        raise RuntimeError("injected-termination")

    monkeypatch.setattr(
        "wargame_native_engine.core.engine.evaluate_terminal", fail)
    _assert_transaction_rollback(
        engine, lambda: engine.step([], delta_time_seconds=1))


def test_failed_tick_exposes_no_green_frame_or_step_record(monkeypatch) -> None:
    scenario = scenario_from_dict(_joint_document())
    environment = SourceCompatibleEnvironment(
        GameSession(NativeEngineBackend(scenario)),
        template=source_frame_template_for_scenario(scenario),
    )
    initial = environment.setup_views(1)
    before = deepcopy(environment.session.snapshot())

    def fail(*args, **kwargs):
        raise RuntimeError("injected-movement")

    engine = environment.session.backend.engine
    monkeypatch.setattr(engine._movement, "advance", fail)
    with pytest.raises(RuntimeError, match="injected-movement"):
        environment.step_views_ms([], delta_time_ms=1_000)

    assert environment.session.snapshot() == before
    assert environment.last_step_record is None
    assert environment.last_bundle.green == initial.green
