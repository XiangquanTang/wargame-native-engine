from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from typing import Callable

import pytest

from scripts.run_minimal_source_replay import _template
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.observation import (
    OperatorObservation,
    side_safe_enemy_observation,
)
from wargame_native_engine.core.operator import MovementTaskPauseReason, OperatorState
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.gateway.models import ControllerView
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.projection import project_operator_lifecycle
from wargame_native_engine.rules.perspective import load_perspective_policy
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.training.observation_encoder import SideObservationEncoder
from wargame_native_engine.viewer.live_control import controller_view_record


def _environment() -> SourceCompatibleEnvironment:
    scenario = load_scenario("examples/source_movement_scenario.json")
    environment = SourceCompatibleEnvironment(
        GameSession(NativeEngineBackend(scenario)),
        template=_template(scenario),
    )
    environment.setup_views(42)
    action = EngineAction(
        "red",
        1,
        ActionType.MOVE,
        parameters={"path": [102, 103, 104], "movement_mode": "NORMAL"},
    )
    environment.step_views([action], delta_time_seconds=60)
    assert environment.session.observe("red").visible_enemies
    return environment


def _side_outputs(environment: SourceCompatibleEnvironment, side: str = "red"):
    session = environment.session
    observation = session.observe(side)
    world = WorldState.from_dict(session.snapshot()["world"])
    green = environment._export_green(  # noqa: SLF001
        world,
        actions=[],
        judge_info=[],
    )
    bundle = environment._project(green)  # noqa: SLF001
    source = bundle.red if side == "red" else bundle.blue
    result = session.result()
    controller = ControllerView(
        session.session_id,
        side.upper(),
        0,
        observation.to_dict(),
        source,
        tuple(item.to_dict() for item in session.valid_actions(side)),
        session.is_terminal(),
        result.winner,
        result.outcome,
        frame_index=0,
        sim_time_ms=world.elapsed_time_ms,
        terminated=result.terminal,
        truncated=result.truncated,
    )
    return {
        "agent": deepcopy(observation).to_dict(),
        "side_hash": observation.observation_hash,
        "source": deepcopy(source),
        "controller": controller.to_record(),
        "human_viewer": controller_view_record(controller),
        "training": SideObservationEncoder().encode(controller),
    }


def _enemy(environment: SourceCompatibleEnvironment) -> OperatorState:
    return environment.session.backend.engine._require_world().operators[2]  # noqa: SLF001


def _assert_hidden_noninterference(
    mutation: Callable[[OperatorState], None],
) -> None:
    environment = _environment()
    before_world_hash = canonical_hash(environment.session.snapshot()["world"])
    before = _side_outputs(environment)
    mutation(_enemy(environment))
    after_world_hash = canonical_hash(environment.session.snapshot()["world"])
    after = _side_outputs(environment)
    assert before_world_hash != after_world_hash
    assert after == before


def test_policy_is_versioned_hashed_and_fail_closed() -> None:
    policy = load_perspective_policy()
    assert policy.policy_id == "native-source-perspectives"
    assert policy.policy_version == "1.1.0"
    assert len(policy.content_hash) == 64
    assert policy.document["visible_enemy_field_policy"] == (
        "ALLOWLIST_FROM_SIDE_OBSERVATION_EVIDENCE_ONLY"
    )
    assert policy.document["own_integrity_visibility"] == "GREEN_ONLY"
    assert "health" in policy.enemy_hidden_fields
    assert "authoritative_semantic_hash" in policy.enemy_hidden_fields


def test_enemy_observation_and_all_normal_consumers_share_one_allowlist() -> None:
    environment = _environment()
    policy = load_perspective_policy()
    outputs = _side_outputs(environment)
    enemy = outputs["agent"]["visible_enemies"][0]
    typed_enemy = environment.session.observe("red").visible_enemies[0]
    assert not hasattr(typed_enemy, "health")
    assert not hasattr(typed_enemy, "heading")
    assert not hasattr(typed_enemy, "deployed")
    assert set(enemy) == set(policy.enemy_observation_allowlist)
    assert not {
        "health", "max_health", "blood", "max_blood", "heading",
        "deployed", "state", "raw_attributes",
    } & set(enemy)

    source_enemy = next(
        value for value in outputs["source"]["operators"]
        if value["obj_id"] == enemy["operator_id"]
    )
    assert source_enemy["blood"] is None
    assert source_enemy["max_blood"] is None
    assert source_enemy["state"] in {"MOVING", "STATIONARY", "UNKNOWN"}
    assert "health" not in source_enemy["extra"]["observation"]
    assert "max_health" not in source_enemy["extra"]["observation"]
    assert outputs["controller"]["observation"] == outputs["agent"]
    assert outputs["training"] == outputs["agent"]
    assert outputs["human_viewer"]["observation"] == outputs["agent"]
    assert outputs["human_viewer"]["source_frame"] == outputs["source"]


@pytest.mark.parametrize(
    "name,mutation",
    (
        ("E1-health", lambda value: setattr(value, "health", 67.0)),
        (
            "E2-suppression",
            lambda value: setattr(value, "suppression_remaining_ms", 45_000),
        ),
        (
            "E3-weapon",
            lambda value: setattr(value, "weapon_cooldown_remaining_ms", 12_000),
        ),
        (
            "E4-guided",
            lambda value: setattr(
                value, "guided_fire_recovery_remaining_ms", 50_000
            ),
        ),
        (
            "E5-indirect",
            lambda value: setattr(
                value, "indirect_fire_planned_cooldown_remaining_ms", 250_000
            ),
        ),
        (
            "E6-lifecycle",
            lambda value: (
                setattr(
                    value,
                    "lifecycle_profile_id",
                    "LOITERING_MUNITION_PREDEPLOYED_V1",
                ),
                setattr(value, "lifecycle_remaining_ms", 321_000),
            ),
        ),
        (
            "E7-movement-task",
            lambda value: (
                setattr(value, "move_path", [105]),
                setattr(
                    value,
                    "movement_task_pause_reason",
                    MovementTaskPauseReason.SUPPRESSION,
                ),
            ),
        ),
        (
            "E8-boarding-relation",
            lambda value: setattr(value, "passenger_id", 999),
        ),
    ),
)
def test_e1_e8_hidden_fields_are_noninterfering(
    name: str,
    mutation: Callable[[OperatorState], None],
) -> None:
    del name
    _assert_hidden_noninterference(mutation)


def test_enemy_raw_future_field_is_fail_closed() -> None:
    environment = _environment()
    before = _side_outputs(environment)
    _enemy(environment).raw_attributes["future_secret_lifecycle_field"] = {
        "remaining_ms": 1,
    }
    assert _side_outputs(environment) == before


def test_exact_identity_is_observation_derived_without_green_backfill() -> None:
    evidence = OperatorObservation(
        77,
        "blue",
        "AIR",
        "source-3-7",
        (3.0, 2.0),
        200.0,
        91.0,
        1.0,
        1.0,
        "STOPPED",
        True,
        True,
    )
    observed = side_safe_enemy_observation(evidence)
    assert observed.source_type_code == 3
    assert observed.source_subtype_code == 7
    assert observed.canonical_identity == "LOITERING_MUNITION"
    assert observed.domain == "AIR"

    missing = side_safe_enemy_observation(replace(evidence, subtype=None))
    assert missing.source_type_code is None
    assert missing.source_subtype_code is None
    assert missing.canonical_identity is None
    assert missing.canonical_status == "UNKNOWN"


@pytest.mark.parametrize(
    "legacy,expected",
    (
        ("MOVING", "MOVING"),
        ("STOPPED", "STATIONARY"),
        ("SUPPRESSED", "UNKNOWN"),
        ("TRANSITIONING", "UNKNOWN"),
        ("BLOCKED", "UNKNOWN"),
        ("ON_BOARD", "UNKNOWN"),
        ("future-state", "UNKNOWN"),
    ),
)
def test_enemy_motion_is_versioned_coarse_and_fail_closed(
    legacy: str,
    expected: str,
) -> None:
    evidence = OperatorObservation(
        2, "blue", "VEHICLE", "observed", (2.0, 1.0), 0.0, 0.0,
        50.0, 100.0, legacy, True, True,
    )
    assert side_safe_enemy_observation(evidence).motion_state == expected


def test_v3_v8_visible_fields_change_side_observation() -> None:
    environment = _environment()
    baseline = environment.session.observe("red").to_dict()["visible_enemies"][0]
    enemy = _enemy(environment)

    enemy.x = 5.0
    positioned = environment.session.observe("red").to_dict()["visible_enemies"][0]
    assert positioned["position"] != baseline["position"]

    enemy.altitude = 20.0
    elevated = environment.session.observe("red").to_dict()["visible_enemies"][0]
    assert elevated["altitude"] != positioned["altitude"]

    enemy.state = "MOVING"
    moving = environment.session.observe("red").to_dict()["visible_enemies"][0]
    assert moving["motion_state"] == "MOVING"

    enemy.alive = False
    destroyed = environment.session.observe("red").to_dict()["visible_enemies"][0]
    assert destroyed["alive"] is False
    assert destroyed["destroyed"] is True


def test_own_health_timers_and_movement_lifecycle_are_full_without_integrity() -> None:
    environment = _environment()
    own = environment.session.backend.engine._require_world().operators[1]  # noqa: SLF001
    own.suppression_remaining_ms = 11
    own.weapon_cooldown_remaining_ms = 12
    own.guided_fire_recovery_remaining_ms = 13
    own.indirect_fire_planned_cooldown_remaining_ms = 14
    own.transition_kind = "TEST_TRANSITION"
    own.transition_remaining_ms = 15
    own.fatigue_recovery_elapsed_ms = 16
    own.lifecycle_profile_id = "test-profile"
    own.lifecycle_remaining_ms = 17
    own.move_path = [105]
    own.movement_task_pause_reason = MovementTaskPauseReason.SUPPRESSION

    record = environment.session.observe("red").to_dict()[
        "friendly_operators"
    ][0]
    assert record["health"] == own.health
    assert record["max_health"] == own.max_health
    assert record["blood"] == own.blood
    assert record["max_blood"] == own.max_blood
    assert record["suppression_remaining_ms"] == 11
    assert record["weapon_cooldown_remaining_ms"] == 12
    assert record["guided_fire_recovery_remaining_ms"] == 13
    assert record["indirect_fire_planned_cooldown_remaining_ms"] == 14
    assert record["transition_remaining_ms"] == 15
    assert record["fatigue_recovery_elapsed_ms"] == 16
    assert record["lifecycle_remaining_ms"] == 17
    assert record["has_remaining_movement_task"] is True
    assert record["movement_task_pause_reason"] == "SUPPRESSION"
    assert "raw_attributes" not in record
    assert "integrity" not in record
    assert "semantic_hash" not in record


def test_own_boarding_transition_timer_is_full() -> None:
    record = json.loads(
        Path("examples/demos/boarding_basic/scenario.json").read_text(
            encoding="utf-8"
        )
    )
    engine = NativeEngineBackend(scenario_from_dict(record)).engine
    engine.reset(42)
    capability = engine.get_board_capabilities("red")[0]
    engine.step([engine.board_action(capability)], delta_time_ms=1_000)
    friendlies = {
        value.operator_id: value
        for value in engine.observe("red").friendly_operators
    }
    for operator_id in (capability.passenger_id, capability.carrier_id):
        value = friendlies[operator_id]
        assert value.boarding_transition_type == "BOARD"
        assert value.boarding_transition_remaining_ms == 74_000


def test_green_and_authoritative_lifecycle_remain_complete() -> None:
    environment = _environment()
    world = environment.session.backend.engine._require_world()  # noqa: SLF001
    enemy = world.operators[2]
    enemy.health = 67.0
    enemy.weapon_cooldown_remaining_ms = 12_000
    authoritative = project_operator_lifecycle(world, 2)
    assert authoritative.survival.health == 67.0
    assert authoritative.weapon_lifecycle.cooldown_remaining_ms == 12_000

    green = environment._export_green(  # noqa: SLF001
        world,
        actions=[],
        judge_info=[],
    )
    green_enemy = next(value for value in green["operators"] if value["obj_id"] == 2)
    assert green_enemy["blood"] == enemy.blood
    assert green_enemy["weapon_cool_time"] == 12.0


def test_red_blue_policy_is_symmetric() -> None:
    environment = _environment()
    red = environment.session.observe("red").to_dict()["visible_enemies"][0]
    blue = environment.session.observe("blue").to_dict()["visible_enemies"][0]
    assert set(red) == set(blue)
    assert red["side"] == "blue"
    assert blue["side"] == "red"
    assert not {"health", "heading", "deployed", "state"} & set(red)
    assert not {"health", "heading", "deployed", "state"} & set(blue)


def test_side_hash_and_metadata_are_policy_safe() -> None:
    environment = _environment()
    outputs = _side_outputs(environment)
    native = outputs["source"]["extra"]["native"]
    policy = load_perspective_policy()
    assert native["perspective_policy_id"] == policy.policy_id
    assert native["perspective_policy_version"] == policy.policy_version
    assert native["perspective_policy_hash"] == policy.content_hash
    assert "semantic_hash" not in str(outputs)
    assert "native-operator-lifecycle-view/1.0" not in str(outputs)


def test_lost_contact_removes_current_enemy_payload() -> None:
    environment = _environment()
    assert environment.session.observe("red").visible_enemies
    environment.step_views([], delta_time_seconds=75)
    action = EngineAction(
        "red",
        1,
        ActionType.MOVE,
        parameters={"path": [103, 102, 101], "movement_mode": "NORMAL"},
    )
    environment.step_views([action], delta_time_seconds=60)
    assert environment.session.observe("red").visible_enemies == ()
