from __future__ import annotations

from copy import deepcopy

from wargame_native_engine.agents.authorization import (
    freeze_valid_actions,
    resolve_agent_selection,
)
from wargame_native_engine.agents.protocol import AgentAction
from wargame_native_engine.compat.source_frame.mapping import SourceMappings
from wargame_native_engine.compat.source_frame.template import (
    source_frame_template_for_scenario,
)
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.io.scenario import load_scenario, scenario_from_dict


SCENARIO_PATH = "examples/demos/drone_basic/scenario.json"


def _scenario():
    return load_scenario(SCENARIO_PATH)


def _engine() -> NativeEngine:
    engine = NativeEngine(_scenario())
    engine.reset(42)
    return engine


def _drone_actions(engine: NativeEngine):
    return tuple(
        value for value in engine.valid_actions("red")
        if value.action.actor_id == 1
    )


def _move_to_102(engine: NativeEngine):
    return next(
        value for value in _drone_actions(engine)
        if value.action.action_type is ActionType.MOVE
        and value.action.parameters["path"] == [102]
    )


def test_exact_source_identity_is_authoritative() -> None:
    mapping = SourceMappings().canonical_identity(3, 5)

    assert mapping.status == "DIRECT"
    assert mapping.native_effective_value == "DRONE"
    drone = _scenario().operators[0]
    assert drone.subtype == "source-3-5"
    assert drone.operator_type == "AIR"
    assert "canonical_type" not in drone.raw_attributes


def test_drone_uses_no_limited_lifecycle_and_never_expires() -> None:
    engine = _engine()
    drone = engine._require_world().operators[1]
    assert drone.lifecycle_profile_id is None
    assert drone.lifecycle_remaining_ms is None

    result = engine.step([], delta_time_ms=1_200_000)

    drone = engine._require_world().operators[1]
    assert drone.alive is True
    assert drone.lifecycle_profile_id is None
    assert drone.lifecycle_remaining_ms is None
    assert not any(
        event.event_type is EventType.UNIT_DESTROYED
        and event.actor_id == 1
        and event.data.get("reason_code") == "LIFECYCLE_EXPIRED"
        for event in result.events
    )


def test_unarmed_drone_naturally_has_only_move_and_wait_when_idle() -> None:
    engine = _engine()
    actions = _drone_actions(engine)

    assert {value.action.action_type for value in actions} == {
        ActionType.MOVE,
        ActionType.WAIT,
    }
    assert all(value.action.parameters.get("weapon_id") is None for value in actions)
    assert engine.get_guided_fire_capabilities("red") == ()
    assert engine.get_indirect_fire_capabilities("red") == ()


def test_forged_shoot_fails_closed_without_rng_or_operator_effect() -> None:
    engine = _engine()
    before = engine.snapshot()
    forged = EngineAction(
        "red",
        1,
        ActionType.SHOOT,
        target_id=2,
        parameters={"weapon_id": 76},
    )

    result = engine.step([forged], delta_time_ms=1_000)

    after = engine.snapshot()
    assert result.decisions[0].accepted is False
    assert after["rng"] == before["rng"]
    assert after["world"]["operators"]["1"]["alive"] is True
    assert after["world"]["operators"]["1"]["blood"] == 3
    assert after["world"]["operators"]["2"]["blood"] == 3
    assert not any(
        event.event_type in {
            EventType.DICE_ROLLED,
            EventType.COMBAT_RESULT_LOOKED_UP,
            EventType.LOSSES_APPLIED,
        }
        for event in result.events
    )


def test_agent_uses_frozen_generic_move_and_visibility_refreshes() -> None:
    engine = _engine()
    assert 2 not in engine._require_world().operators[1].raw_attributes.get(
        "see_enemy_bop_ids", ()
    )
    selected = _move_to_102(engine)
    frozen = freeze_valid_actions("red", engine.valid_actions("red"))
    resolved = resolve_agent_selection(
        "red",
        AgentAction.from_valid_action(selected),
        frozen,
    )

    result = engine.step([resolved], delta_time_ms=8_000)

    drone = engine._require_world().operators[1]
    assert result.decisions[0].accepted is True
    assert drone.current_hex == 102
    assert 2 in drone.raw_attributes["see_enemy_bop_ids"]
    assert any(event.event_type is EventType.ARRIVED_HEX for event in result.events)


def test_snapshot_restore_preserves_drone_without_lifecycle_pollution() -> None:
    engine = _engine()
    engine.step([_move_to_102(engine).action], delta_time_ms=4_000)
    snapshot = engine.snapshot()

    restored = NativeEngine(_scenario())
    restored.reset(7)
    restored.restore(deepcopy(snapshot))

    assert restored.snapshot() == snapshot
    drone = restored._require_world().operators[1]
    assert drone.lifecycle_profile_id is None
    assert drone.lifecycle_remaining_ms is None


def test_source_projection_preserves_exact_drone_codes() -> None:
    template = source_frame_template_for_scenario(_scenario())
    drone = next(value for value in template.operators if value.obj_id == 1)

    assert drone.type_code == 3
    assert drone.sub_type_code == 5


def test_raw_canonical_metadata_cannot_override_exact_mapping() -> None:
    payload = _scenario().to_dict()
    drone = next(value for value in payload["operators"] if value["operator_id"] == 1)
    drone["raw_attributes"]["canonical_type"] = "LOITERING_MUNITION"
    poisoned = NativeEngine(scenario_from_dict(payload))
    poisoned.reset(42)

    assert poisoned._require_world().operators[1].lifecycle_profile_id is None
    assert {
        value.action.action_type for value in poisoned.valid_actions("red")
        if value.action.actor_id == 1
    } == {ActionType.MOVE, ActionType.WAIT}
