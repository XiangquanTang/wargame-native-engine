from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from typing import Callable

import pytest

from tests.test_authoritative_timer_repair import _guided_engine, _indirect_engine
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.observation import (
    ObservedEnemyOperatorObservation,
    OperatorObservation,
    side_safe_enemy_observation,
)
from wargame_native_engine.core.operator import MovementTaskPauseReason, OperatorState
from wargame_native_engine.io.replay import ReplayWriter, replay_actions
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.projection.operator_lifecycle import (
    AuthoritativeOperatorLifecycleView,
    project_operator_lifecycle,
    project_world_operator_lifecycles,
)
from wargame_native_engine.projection.operator_perspective import (
    OPERATOR_LIFECYCLE_PERSPECTIVE_SCHEMA_VERSION,
    GreenOperatorLifecyclePayload,
    ObservedEnemyOperatorLifecyclePayload,
    ObservedMotionState,
    OperatorLifecyclePerspectiveKind,
    OwnOperatorLifecyclePayload,
    project_green_operator_lifecycle,
    project_green_operator_lifecycles,
    project_observed_enemy_operator_lifecycle,
    project_own_operator_lifecycle,
    project_side_operator_lifecycles,
)
from wargame_native_engine.rules.perspective import load_perspective_policy


ROOT = Path(__file__).resolve().parents[1]
DRONE_SCENARIO = ROOT / "examples/demos/drone_basic/scenario.json"
LOITERING_SCENARIO = (
    ROOT / "examples/demos/loitering_munition_basic/scenario.json"
)
SOURCE_MOVEMENT_SCENARIO = ROOT / "examples/source_movement_scenario.json"


def _engine(path: Path, *, seed: int = 42) -> NativeEngine:
    engine = NativeEngine(load_scenario(path))
    engine.reset(seed)
    return engine


def _contact_engine() -> NativeEngine:
    engine = _engine(SOURCE_MOVEMENT_SCENARIO)
    engine.step(
        [
            EngineAction(
                "red",
                1,
                ActionType.MOVE,
                parameters={
                    "path": [102, 103, 104],
                    "movement_mode": "NORMAL",
                },
            )
        ],
        delta_time_ms=60_000,
    )
    assert engine.observe("red").visible_enemies
    return engine


def _enemy_projection(engine: NativeEngine, side: str = "red"):
    evidence = engine.observe(side).visible_enemies[0]
    return project_observed_enemy_operator_lifecycle(evidence)


def _enemy(engine: NativeEngine) -> OperatorState:
    return engine._require_world().operators[2]  # noqa: SLF001


def _observed_air(subtype: str) -> ObservedEnemyOperatorObservation:
    rich_evidence = OperatorObservation(
        operator_id=9,
        side="blue",
        operator_type="AIR",
        subtype=subtype,
        position=(3.0, 2.0),
        altitude=200.0,
        heading=91.0,
        health=1.0,
        max_health=1.0,
        state="STOPPED",
        alive=True,
        deployed=True,
    )
    return side_safe_enemy_observation(rich_evidence)


def test_p1_green_payload_is_complete_versioned_and_immutable() -> None:
    engine = _engine(DRONE_SCENARIO)
    view = project_operator_lifecycle(engine._require_world(), 1)  # noqa: SLF001

    envelope = project_green_operator_lifecycle(view)

    assert envelope.schema_version == OPERATOR_LIFECYCLE_PERSPECTIVE_SCHEMA_VERSION
    assert envelope.perspective_kind is OperatorLifecyclePerspectiveKind.GREEN
    assert isinstance(envelope.payload, GreenOperatorLifecyclePayload)
    assert envelope.payload.authoritative_schema_version == view.schema_version
    assert envelope.payload.authoritative_semantic_hash == view.semantic_hash
    assert envelope.payload.integrity == view.integrity
    assert envelope.payload.limited_lifecycle == view.limited_lifecycle
    with pytest.raises(FrozenInstanceError):
        envelope.semantic_hash = "changed"  # type: ignore[misc]


def test_p2_p3_own_payload_has_full_operational_axes_without_integrity() -> None:
    engine = _engine(LOITERING_SCENARIO)
    operator = engine._require_world().operators[1]  # noqa: SLF001
    operator.transition_kind = "WEAPON_LOCK"
    operator.transition_remaining_ms = 11
    operator.suppression_remaining_ms = 12
    operator.weapon_cooldown_remaining_ms = 13
    operator.guided_fire_recovery_remaining_ms = 14
    operator.indirect_fire_planned_cooldown_remaining_ms = 15
    operator.fatigue_recovery_elapsed_ms = 16
    operator.lifecycle_remaining_ms = 17
    view = project_operator_lifecycle(engine._require_world(), 1)  # noqa: SLF001

    envelope = project_own_operator_lifecycle(view)
    payload = envelope.payload

    assert isinstance(payload, OwnOperatorLifecyclePayload)
    assert payload.operator_transition.remaining_ms == 11
    assert payload.suppression.remaining_ms == 12
    assert payload.weapon_lifecycle.cooldown_remaining_ms == 13
    assert payload.guided_recovery == view.guided_recovery
    assert payload.indirect_planning == view.indirect_planning
    assert payload.fatigue == view.fatigue
    assert payload.limited_lifecycle.remaining_ms == 17
    assert not hasattr(payload, "integrity")
    assert not hasattr(payload, "authoritative_semantic_hash")
    assert view.semantic_hash not in str(envelope.to_dict())


def test_own_applicable_guided_and_indirect_timers_are_full() -> None:
    guided_engine = _guided_engine()
    guided_operator = guided_engine._require_world().operators[10]  # noqa: SLF001
    guided_operator.guided_fire_recovery_remaining_ms = 14
    guided_view = project_operator_lifecycle(
        guided_engine._require_world(),  # noqa: SLF001
        10,
    )
    guided = project_own_operator_lifecycle(guided_view).payload
    assert isinstance(guided, OwnOperatorLifecyclePayload)
    assert guided.guided_recovery.remaining_ms == 14

    indirect_engine = _indirect_engine()
    indirect_operator = indirect_engine._require_world().operators[1]  # noqa: SLF001
    indirect_operator.indirect_fire_planned_cooldown_remaining_ms = 15
    indirect_view = project_operator_lifecycle(
        indirect_engine._require_world(),  # noqa: SLF001
        1,
    )
    indirect = project_own_operator_lifecycle(indirect_view).payload
    assert isinstance(indirect, OwnOperatorLifecyclePayload)
    assert indirect.indirect_planning.remaining_ms == 15

    fatigue_engine = _engine(
        ROOT / "examples/demos/boarding_basic/scenario.json"
    )
    fatigue_operator = fatigue_engine._require_world().operators[101]  # noqa: SLF001
    fatigue_operator.fatigue_level = 1
    fatigue_operator.fatigue_recovery_elapsed_ms = 16
    fatigue_view = project_operator_lifecycle(
        fatigue_engine._require_world(),  # noqa: SLF001
        101,
    )
    fatigue = project_own_operator_lifecycle(fatigue_view).payload
    assert isinstance(fatigue, OwnOperatorLifecyclePayload)
    assert fatigue.fatigue.recovery_elapsed_ms == 16


def test_p4_p8_enemy_payload_has_exact_positive_schema() -> None:
    envelope = project_observed_enemy_operator_lifecycle(
        _observed_air("source-3-7")
    )
    payload = envelope.payload

    assert isinstance(payload, ObservedEnemyOperatorLifecyclePayload)
    assert {item.name for item in fields(payload)} == {
        "operator_reference", "side", "identity", "observation",
        "survival", "motion",
    }
    serialized = envelope.to_dict()["payload"]
    assert isinstance(serialized, dict)
    forbidden = {
        "health", "max_health", "blood", "max_blood", "heading",
        "deployed", "suppression", "concealment", "boarding", "weapon",
        "weapon_lifecycle", "guided", "guided_recovery", "indirect",
        "indirect_planning", "limited_lifecycle", "integrity",
    }
    assert forbidden.isdisjoint(serialized)
    for name in forbidden:
        assert not hasattr(payload, name)


@pytest.mark.parametrize(
    ("motion", "expected"),
    [
        ("MOVING", ObservedMotionState.MOVING),
        ("STATIONARY", ObservedMotionState.STATIONARY),
        ("SUPPRESSED", ObservedMotionState.UNKNOWN),
        ("TRANSITIONING", ObservedMotionState.UNKNOWN),
        ("future", ObservedMotionState.UNKNOWN),
    ],
)
def test_p9_p10_coarse_motion_is_independent_and_fail_closed(
    motion: str,
    expected: ObservedMotionState,
) -> None:
    evidence = replace(_observed_air("source-3-7"), motion_state=motion)
    payload = project_observed_enemy_operator_lifecycle(evidence).payload
    assert isinstance(payload, ObservedEnemyOperatorLifecyclePayload)
    assert payload.motion is expected


def test_p11_p12_identity_uses_observation_evidence_without_green_backfill() -> None:
    known = project_observed_enemy_operator_lifecycle(
        _observed_air("source-3-7")
    ).payload
    missing = project_observed_enemy_operator_lifecycle(
        _observed_air("unmapped-contact")
    ).payload
    assert isinstance(known, ObservedEnemyOperatorLifecyclePayload)
    assert isinstance(missing, ObservedEnemyOperatorLifecyclePayload)
    assert known.identity.canonical_identity == "LOITERING_MUNITION"
    assert missing.identity.canonical_identity is None
    assert missing.identity.canonical_status.value == "UNKNOWN"


def test_p13_lost_contact_produces_no_enemy_payload() -> None:
    engine = _contact_engine()
    world = engine._require_world()  # noqa: SLF001
    own = {
        item.operator_id: item
        for item in project_world_operator_lifecycles(world)
        if item.identity.side == "red"
    }
    assert any(
        item.perspective_kind is OperatorLifecyclePerspectiveKind.OBSERVED_ENEMY
        for item in project_side_operator_lifecycles(engine.observe("red"), own)
    )
    engine.step([], delta_time_ms=75_000)
    engine.step(
        [
            EngineAction(
                "red",
                1,
                ActionType.MOVE,
                parameters={
                    "path": [103, 102, 101],
                    "movement_mode": "NORMAL",
                },
            )
        ],
        delta_time_ms=60_000,
    )
    own = {
        item.operator_id: item
        for item in project_world_operator_lifecycles(world)
        if item.identity.side == "red"
    }
    result = project_side_operator_lifecycles(engine.observe("red"), own)
    assert all(
        item.perspective_kind is OperatorLifecyclePerspectiveKind.OWN
        for item in result
    )


def test_p14_red_blue_projection_is_symmetric() -> None:
    engine = _contact_engine()
    red = _enemy_projection(engine, "red")
    blue = _enemy_projection(engine, "blue")
    assert type(red.payload) is type(blue.payload)
    assert {item.name for item in fields(red.payload)} == {
        item.name for item in fields(blue.payload)
    }
    assert red.payload.side == "blue"
    assert blue.payload.side == "red"


def test_p15_batch_projection_has_stable_operator_order() -> None:
    engine = _engine(DRONE_SCENARIO)
    world = engine._require_world()  # noqa: SLF001
    observation = engine.observe("red")
    reversed_observation = replace(
        observation,
        friendly_operators=tuple(reversed(observation.friendly_operators)),
        visible_enemies=tuple(reversed(observation.visible_enemies)),
    )
    own = {
        item.operator_id: item
        for item in project_world_operator_lifecycles(world)
        if item.identity.side == "red"
    }

    first = project_side_operator_lifecycles(observation, own)
    second = project_side_operator_lifecycles(reversed_observation, own)

    assert first == second
    assert [item.payload.operator_reference for item in first] == sorted(
        item.payload.operator_reference for item in first
    )


def test_p16_p17_purity_and_hash_are_deterministic_and_namespaced() -> None:
    engine = _contact_engine()
    world = engine._require_world()  # noqa: SLF001
    observation = engine.observe("red")
    view = project_operator_lifecycle(world, 1)
    before_world = canonical_hash(world.to_dict())
    before_observation = deepcopy(observation.to_dict())

    own_a = project_own_operator_lifecycle(view)
    own_b = project_own_operator_lifecycle(view)
    enemy_a = project_observed_enemy_operator_lifecycle(
        observation.visible_enemies[0]
    )
    enemy_b = project_observed_enemy_operator_lifecycle(
        observation.visible_enemies[0]
    )

    assert own_a == own_b
    assert enemy_a == enemy_b
    assert len(own_a.semantic_hash) == 64
    assert len(enemy_a.semantic_hash) == 64
    assert own_a.semantic_hash != view.semantic_hash
    assert view.semantic_hash not in str(own_a.to_dict())
    assert view.semantic_hash not in str(enemy_a.to_dict())
    assert canonical_hash(world.to_dict()) == before_world
    assert observation.to_dict() == before_observation


def test_p18_snapshot_restore_rebuilds_identical_green_and_own_payloads() -> None:
    engine = _engine(LOITERING_SCENARIO)
    engine.step([], delta_time_ms=1234)
    view = project_operator_lifecycle(engine._require_world(), 1)  # noqa: SLF001
    expected = (
        project_green_operator_lifecycle(view),
        project_own_operator_lifecycle(view),
    )
    snapshot = deepcopy(engine.snapshot())
    restored = _engine(LOITERING_SCENARIO, seed=7)
    restored.restore(snapshot)
    restored_view = project_operator_lifecycle(
        restored._require_world(),  # noqa: SLF001
        1,
    )
    actual = (
        project_green_operator_lifecycle(restored_view),
        project_own_operator_lifecycle(restored_view),
    )
    assert actual == expected


def test_p19_strict_replay_rebuilds_identical_perspective_payloads(
    tmp_path: Path,
) -> None:
    engine = _engine(DRONE_SCENARIO)
    action = next(
        item.action
        for item in engine.valid_actions("red")
        if item.action.actor_id == 1
        and item.action.action_type is ActionType.MOVE
        and item.action.parameters["path"] == [102]
    )
    writer = ReplayWriter(
        tmp_path / "perspective-replay",
        run_id="perspective-replay",
        scenario=engine.scenario,
        seed=42,
        agents={"red": "test", "blue": "test"},
        max_ticks=engine.terminal_rules.max_ticks,
        ruleset=engine.ruleset,
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    result = engine.step([action], delta_time_ms=8_000)
    writer.record_step(result, engine.snapshot())
    writer.finalize({"result": engine.result().to_dict()}, status="complete")
    final_view = project_operator_lifecycle(engine._require_world(), 1)  # noqa: SLF001
    expected = project_own_operator_lifecycle(final_view)

    verification = replay_actions(engine.scenario, writer.root, strict=True)
    replayed = _engine(DRONE_SCENARIO)
    replayed.step([action], delta_time_ms=8_000)
    actual = project_own_operator_lifecycle(
        project_operator_lifecycle(replayed._require_world(), 1)  # noqa: SLF001
    )
    assert verification.matches is True
    assert actual == expected
    assert actual.semantic_hash == expected.semantic_hash


@pytest.mark.parametrize(
    ("name", "mutation"),
    [
        ("E1", lambda value: setattr(value, "health", 67.0)),
        ("E2", lambda value: setattr(value, "suppression_remaining_ms", 45_000)),
        ("E3", lambda value: setattr(value, "weapon_cooldown_remaining_ms", 12_000)),
        (
            "E4",
            lambda value: setattr(
                value, "guided_fire_recovery_remaining_ms", 50_000
            ),
        ),
        (
            "E5",
            lambda value: setattr(
                value,
                "indirect_fire_planned_cooldown_remaining_ms",
                250_000,
            ),
        ),
        (
            "E6",
            lambda value: (
                setattr(value, "lifecycle_profile_id", "test-lifecycle"),
                setattr(value, "lifecycle_remaining_ms", 321_000),
            ),
        ),
        (
            "E7",
            lambda value: (
                setattr(value, "move_path", [105]),
                setattr(
                    value,
                    "movement_task_pause_reason",
                    MovementTaskPauseReason.SUPPRESSION,
                ),
            ),
        ),
        ("E8", lambda value: setattr(value, "passenger_id", 999)),
    ],
)
def test_p20_e1_e8_hidden_state_is_payload_and_hash_noninterfering(
    name: str,
    mutation: Callable[[OperatorState], None],
) -> None:
    del name
    engine = _contact_engine()
    before = _enemy_projection(engine)
    mutation(_enemy(engine))
    after = _enemy_projection(engine)
    assert after == before
    assert after.semantic_hash == before.semantic_hash


def test_p21_drone_and_loitering_have_fixed_enemy_shape() -> None:
    drone = project_observed_enemy_operator_lifecycle(
        _observed_air("source-3-5")
    )
    loitering = project_observed_enemy_operator_lifecycle(
        _observed_air("source-3-7")
    )
    drone_keys = set(drone.to_dict()["payload"])
    loitering_keys = set(loitering.to_dict()["payload"])
    assert drone_keys == loitering_keys
    assert "limited_lifecycle" not in drone_keys
    assert "limited_lifecycle" not in loitering_keys
    assert type(drone.payload) is type(loitering.payload)


def test_p22_authoritative_future_fields_cannot_auto_leak_enemy() -> None:
    engine = _engine(LOITERING_SCENARIO)
    authoritative = project_operator_lifecycle(
        engine._require_world(),  # noqa: SLF001
        1,
    )
    with pytest.raises(TypeError, match="safe contact evidence"):
        project_observed_enemy_operator_lifecycle(authoritative)  # type: ignore[arg-type]
    payload = project_observed_enemy_operator_lifecycle(
        _observed_air("source-3-7")
    ).to_dict()["payload"]
    assert isinstance(payload, dict)
    assert set(payload) == {
        "operator_reference", "side", "identity", "observation",
        "survival", "motion",
    }


def test_policy_metadata_is_reused_without_a_second_hash() -> None:
    policy = load_perspective_policy()
    envelope = project_observed_enemy_operator_lifecycle(
        _observed_air("source-3-7")
    )
    assert envelope.policy_id == policy.policy_id
    assert envelope.policy_version == policy.policy_version
    assert envelope.policy_hash == policy.content_hash


def test_green_batch_is_stable() -> None:
    engine = _engine(DRONE_SCENARIO)
    views = project_world_operator_lifecycles(engine._require_world())  # noqa: SLF001
    result = project_green_operator_lifecycles(reversed(views))
    assert [item.payload.operator_reference for item in result] == sorted(
        item.operator_id for item in views
    )


def test_missing_own_view_and_wrong_enemy_input_fail_closed() -> None:
    engine = _engine(DRONE_SCENARIO)
    with pytest.raises(ValueError, match="own lifecycle view is missing"):
        project_side_operator_lifecycles(engine.observe("red"), {})
    with pytest.raises(TypeError, match="safe contact evidence"):
        project_observed_enemy_operator_lifecycle(object())  # type: ignore[arg-type]
