from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tests.test_authoritative_timer_repair import (
    _guided_action,
    _guided_engine,
    _indirect_engine,
)
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.operator import MovementTaskPauseReason
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.io.replay import ReplayWriter, replay_actions
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.projection.operator_lifecycle import (
    OPERATOR_LIFECYCLE_VIEW_SCHEMA_VERSION,
    Applicability,
    BoardingLifecycleState,
    BoardingRole,
    IntegrityStatus,
    LimitedLifecycleState,
    MovementLifecycleState,
    SpatialStatus,
    project_operator_lifecycle,
    project_world_operator_lifecycles,
)


ROOT = Path(__file__).resolve().parents[1]
BOARDING_SCENARIO = ROOT / "examples/demos/boarding_basic/scenario.json"
DRONE_SCENARIO = ROOT / "examples/demos/drone_basic/scenario.json"
LOITERING_SCENARIO = (
    ROOT / "examples/demos/loitering_munition_basic/scenario.json"
)


def _engine(path: Path, *, seed: int = 42) -> NativeEngine:
    engine = NativeEngine(load_scenario(path))
    engine.reset(seed)
    return engine


def _board(engine: NativeEngine) -> None:
    capability = engine.get_board_capabilities("red")[0]
    result = engine.step(
        [engine.board_action(capability)],
        delta_time_ms=1_000,
    )
    assert result.decisions[0].accepted is True


def test_v1_v2_same_state_is_stable_and_deeply_immutable() -> None:
    engine = _engine(DRONE_SCENARIO)
    world = engine._require_world()  # noqa: SLF001

    first = project_operator_lifecycle(world, 1)
    second = project_operator_lifecycle(world, 1)

    assert first == second
    assert first.schema_version == OPERATOR_LIFECYCLE_VIEW_SCHEMA_VERSION
    assert first.semantic_hash == second.semantic_hash
    with pytest.raises(FrozenInstanceError):
        first.operator_id = 999  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        first.movement.speed = 99  # type: ignore[misc]


def test_v3_v4_projection_does_not_mutate_world_or_advance_timers() -> None:
    engine = _engine(LOITERING_SCENARIO)
    world = engine._require_world()  # noqa: SLF001
    operator = world.operators[1]
    before_world = canonical_hash(world.to_dict())
    before_operator = deepcopy(operator.to_dict())
    before_remaining = operator.lifecycle_remaining_ms

    project_operator_lifecycle(world, 1)

    assert canonical_hash(world.to_dict()) == before_world
    assert operator.to_dict() == before_operator
    assert operator.lifecycle_remaining_ms == before_remaining


def test_v5_snapshot_restore_produces_same_view_and_hash() -> None:
    engine = _engine(DRONE_SCENARIO)
    move = next(
        value.action
        for value in engine.valid_actions("red")
        if value.action.actor_id == 1
        and value.action.action_type.value == "MOVE"
        and value.action.parameters["path"] == [102]
    )
    engine.step([move], delta_time_ms=4_000)
    before = project_operator_lifecycle(engine._require_world(), 1)  # noqa: SLF001
    snapshot = deepcopy(engine.snapshot())
    restored = _engine(DRONE_SCENARIO, seed=7)
    restored.restore(snapshot)

    after = project_operator_lifecycle(restored._require_world(), 1)  # noqa: SLF001

    assert after == before
    assert after.semantic_hash == before.semantic_hash


def test_v6_strict_replay_reexecution_produces_same_final_view(
    tmp_path: Path,
) -> None:
    engine = _engine(DRONE_SCENARIO)
    action = next(
        value.action
        for value in engine.valid_actions("red")
        if value.action.actor_id == 1
        and value.action.action_type.value == "MOVE"
        and value.action.parameters["path"] == [102]
    )
    output = tmp_path / "lifecycle-view-replay"
    writer = ReplayWriter(
        output,
        run_id="lifecycle-view-replay",
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
    expected = project_operator_lifecycle(engine._require_world(), 1)  # noqa: SLF001

    verification = replay_actions(engine.scenario, writer.root, strict=True)
    replayed = _engine(DRONE_SCENARIO)
    replayed.step([action], delta_time_ms=8_000)
    actual = project_operator_lifecycle(replayed._require_world(), 1)  # noqa: SLF001

    assert verification.matches is True
    assert actual == expected
    assert actual.semantic_hash == expected.semantic_hash


def test_v7_stale_raw_suppression_is_not_a_view_authority() -> None:
    engine = _engine(BOARDING_SCENARIO)
    operator = engine._require_world().operators[101]  # noqa: SLF001
    operator.suppression_remaining_ms = 12_345
    operator.state = "SUPPRESSED"
    operator.raw_attributes["suppression_remaining_seconds"] = 999
    first = project_operator_lifecycle(engine._require_world(), 101)  # noqa: SLF001

    operator.raw_attributes["suppression_remaining_seconds"] = 0
    second = project_operator_lifecycle(engine._require_world(), 101)  # noqa: SLF001

    assert first == second
    assert first.suppression.active is True
    assert first.suppression.remaining_ms == 12_345


def test_runtime_raw_concealment_and_irrelevant_metadata_do_not_change_view() -> None:
    engine = _engine(BOARDING_SCENARIO)
    operator = engine._require_world().operators[101]  # noqa: SLF001
    first = project_operator_lifecycle(engine._require_world(), 101)  # noqa: SLF001

    operator.raw_attributes["concealed"] = not operator.concealed
    operator.raw_attributes["unrelated_projection_poison"] = {"z": 1}
    second = project_operator_lifecycle(engine._require_world(), 101)  # noqa: SLF001

    assert second == first
    assert second.semantic_hash == first.semantic_hash
    assert second.concealment.posture == operator.concealment_posture.value


def test_v8_on_board_current_hex_none_has_complete_semantics() -> None:
    engine = _engine(BOARDING_SCENARIO)
    _board(engine)

    view = project_operator_lifecycle(engine._require_world(), 101)  # noqa: SLF001

    assert view.deployment.current_hex is None
    assert view.deployment.spatially_present is False
    assert view.deployment.spatial_status is SpatialStatus.ON_BOARD
    assert view.deployment.on_board is True
    assert view.boarding.role is BoardingRole.PASSENGER
    assert view.boarding.state is BoardingLifecycleState.BOARDING


def test_v9_suppression_pause_and_pause_pending_are_distinct() -> None:
    engine = _engine(BOARDING_SCENARIO)
    world = engine._require_world()  # noqa: SLF001
    operator = world.operators[101]
    operator.move_path = [102]
    operator.movement_task_pause_reason = MovementTaskPauseReason.SUPPRESSION
    operator.suppression_remaining_ms = 1_000
    operator.state = "SUPPRESSED"

    paused = project_operator_lifecycle(world, 101)
    assert paused.movement.state is MovementLifecycleState.PAUSED
    assert paused.movement.paused is True
    assert paused.movement.pause_pending is False

    operator.segment_from_hex = 101
    operator.segment_to_hex = 102
    operator.segment_traversal_ms = 1_000
    operator.segment_elapsed_ms = 500
    operator.cur_pos_millionths = 500_000
    operator.speed = 5
    operator.stopped = False
    moving = project_operator_lifecycle(world, 101)
    assert moving.movement.state is MovementLifecycleState.MOVING
    assert moving.movement.pause_pending is True
    assert moving.movement.paused is False


def test_v10_guided_recovery_exact_boundary_semantics() -> None:
    engine = _guided_engine()
    engine.step([_guided_action(engine)], delta_time_ms=74_999)
    world = engine._require_world()  # noqa: SLF001

    blocked = project_operator_lifecycle(world, 10)
    assert blocked.guided_recovery.applicability is Applicability.APPLICABLE
    assert blocked.guided_recovery.remaining_ms == 1
    assert blocked.guided_recovery.active is True
    assert blocked.guided_recovery.ready is False

    engine.step([], delta_time_ms=1)
    ready = project_operator_lifecycle(world, 10)
    assert ready.guided_recovery.remaining_ms == 0
    assert ready.guided_recovery.active is False
    assert ready.guided_recovery.ready is True


def test_v11_indirect_cooldown_exact_boundary_semantics() -> None:
    engine = _indirect_engine()
    world = engine._require_world()  # noqa: SLF001
    operator = world.operators[1]
    operator.indirect_fire_planned_cooldown_remaining_ms = 1

    blocked = project_operator_lifecycle(world, 1)
    assert blocked.indirect_planning.applicability is Applicability.APPLICABLE
    assert blocked.indirect_planning.remaining_ms == 1
    assert blocked.indirect_planning.ready is False

    engine.step([], delta_time_ms=1)
    ready = project_operator_lifecycle(world, 1)
    assert ready.indirect_planning.remaining_ms == 0
    assert ready.indirect_planning.ready is True


def test_v12_v13_loitering_finite_and_drone_no_lifecycle() -> None:
    loitering = _engine(LOITERING_SCENARIO)
    loitering_view = project_operator_lifecycle(
        loitering._require_world(),  # noqa: SLF001
        1,
    )
    assert loitering_view.limited_lifecycle.applicability is Applicability.APPLICABLE
    assert loitering_view.limited_lifecycle.enabled is True
    assert loitering_view.limited_lifecycle.state is LimitedLifecycleState.ACTIVE
    assert loitering_view.limited_lifecycle.remaining_ms == 1_200_000

    drone = _engine(DRONE_SCENARIO)
    drone_view = project_operator_lifecycle(drone._require_world(), 1)  # noqa: SLF001
    assert drone_view.limited_lifecycle.applicability is Applicability.NOT_APPLICABLE
    assert drone_view.limited_lifecycle.enabled is False
    assert drone_view.limited_lifecycle.state is LimitedLifecycleState.NOT_APPLICABLE
    assert drone_view.limited_lifecycle.remaining_ms is None


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda op: setattr(op, "movement_mode", "TELEPORT"), "UNKNOWN_MOVEMENT_MODE"),
        (lambda op: setattr(op, "speed", 1), "UNSTABLE_STOPPED_STATE"),
        (lambda op: setattr(op, "transition_kind", "WEAPON_LOCK"), "TRANSITION_KIND_TIME_MISMATCH"),
        (lambda op: setattr(op, "lifecycle_profile_id", "missing-profile"), "LIFECYCLE_PROFILE_TIME_MISMATCH"),
    ],
)
def test_v14_v19_v20_v21_invalid_combinations_fail_closed(
    mutation,
    expected_code: str,
) -> None:
    engine = _engine(DRONE_SCENARIO)
    operator = engine._require_world().operators[1]  # noqa: SLF001
    mutation(operator)

    view = project_operator_lifecycle(engine._require_world(), 1)  # noqa: SLF001

    assert expected_code in view.integrity.codes
    assert view.integrity.status in {
        IntegrityStatus.UNKNOWN,
        IntegrityStatus.MISSING,
        IntegrityStatus.INCONSISTENT,
    }


def test_v15_serialization_default_omission_does_not_drift_semantics() -> None:
    engine = _engine(DRONE_SCENARIO)
    world = engine._require_world()  # noqa: SLF001
    before = project_world_operator_lifecycles(world)

    restored = WorldState.from_dict(world.to_dict())
    after = project_world_operator_lifecycles(restored)

    assert after == before


def test_v16_semantic_hash_is_stable_and_not_world_hash() -> None:
    engine = _engine(DRONE_SCENARIO)
    world = engine._require_world()  # noqa: SLF001
    hashes = {
        project_operator_lifecycle(world, 1).semantic_hash
        for _ in range(5)
    }

    assert len(hashes) == 1
    assert next(iter(hashes)) != canonical_hash(world.to_dict())
    assert len(next(iter(hashes))) == 64


def test_v17_destroyed_operator_with_movement_is_inconsistent() -> None:
    engine = _engine(DRONE_SCENARIO)
    operator = engine._require_world().operators[1]  # noqa: SLF001
    operator.alive = False
    operator.health = 0
    operator.blood = 0
    operator.move_path = [102]

    view = project_operator_lifecycle(engine._require_world(), 1)  # noqa: SLF001

    assert view.movement.state is MovementLifecycleState.INCONSISTENT
    assert "DESTROYED_WITH_ACTIVE_MOVEMENT" in view.integrity.codes


def test_v18_boarding_reciprocal_relation_failure_is_reported_only() -> None:
    engine = _engine(BOARDING_SCENARIO)
    _board(engine)
    world = engine._require_world()  # noqa: SLF001
    carrier = world.operators[102]
    carrier.passenger_id = None
    before = canonical_hash(world.to_dict())

    view = project_operator_lifecycle(world, 101)

    assert view.integrity.status is IntegrityStatus.INCONSISTENT
    assert "BOARDING_RELATION_NOT_RECIPROCAL" in view.integrity.codes
    assert canonical_hash(world.to_dict()) == before


def test_v22_unrelated_raw_metadata_does_not_change_hash() -> None:
    engine = _engine(DRONE_SCENARIO)
    world = engine._require_world()  # noqa: SLF001
    before = project_operator_lifecycle(world, 1)

    world.operators[1].raw_attributes["debug_only"] = {
        "unordered": [3, 1, 2],
        "timestamp": "ignored",
    }
    after = project_operator_lifecycle(world, 1)

    assert after == before
    assert after.semantic_hash == before.semantic_hash


def test_missing_operator_fails_closed() -> None:
    engine = _engine(DRONE_SCENARIO)
    with pytest.raises(ValueError, match="OPERATOR_NOT_FOUND"):
        project_operator_lifecycle(engine._require_world(), 999)  # noqa: SLF001
