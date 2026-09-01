from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from tests.test_guided_capability import _scenario_record
from tests.test_indirect_fire_action_replay import _scenario as indirect_scenario
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.core.fire_capability import (
    IndirectFireCapabilityStatus,
)
from wargame_native_engine.core.indirect_fire import FireMissionPhase
from wargame_native_engine.io.replay import ReplayWriter, replay_actions
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash


def _guided_scenario():
    payload = _scenario_record(
        target_hexes=(115, 116),
        target_concealed=False,
    )
    payload["scenario_id"] = "guided-authoritative-recovery-test"
    payload["terminal_rules"]["max_ticks"] = 1_000
    return scenario_from_dict(payload)


def _guided_engine(*, seed: int = 42) -> NativeEngine:
    engine = NativeEngine(_guided_scenario())
    engine.reset(seed)
    return engine


def _guided_action(engine: NativeEngine) -> EngineAction:
    return engine.guided_fire_action(
        engine.get_guided_fire_capabilities("red")[0]
    )


def _indirect_engine(*, seed: int = 5) -> NativeEngine:
    engine = NativeEngine(indirect_scenario())
    engine.reset(seed)
    return engine


def _create_indirect_mission(engine: NativeEngine, planned_hex: int = 505):
    capability = next(
        value
        for value in engine.get_indirect_fire_capabilities("red")
        if value.status is IndirectFireCapabilityStatus.AVAILABLE
    )
    plan = engine.build_indirect_fire_plan_request(capability, planned_hex)
    assert plan.valid is True
    assert plan.request is not None
    return engine.create_indirect_fire_mission(plan.request)


def _write_replay(
    path: Path,
    engine: NativeEngine,
    steps: tuple[tuple[list[EngineAction], int], ...],
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    writer = ReplayWriter(
        path,
        run_id=path.name,
        scenario=engine.scenario,
        seed=int(engine.snapshot()["seed"]),
        agents={"red": "test", "blue": "test"},
        max_ticks=engine.terminal_rules.max_ticks,
        ruleset=engine.ruleset,
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    events: list[dict[str, object]] = []
    for actions, delta_time_ms in steps:
        result = engine.step(actions, delta_time_ms=delta_time_ms)
        writer.record_step(result, engine.snapshot())
        events.extend(event.to_dict() for event in result.events)
    final_snapshot = deepcopy(engine.snapshot())
    writer.finalize({"result": engine.result().to_dict()}, status="complete")
    verification = replay_actions(engine.scenario, writer.root, strict=True)
    assert verification.matches is True
    assert verification.expected_world_hash == canonical_hash(
        final_snapshot["world"]
    )
    return final_snapshot, tuple(events)


def test_guided_recovery_uses_exact_authoritative_milliseconds() -> None:
    engine = _guided_engine()
    action = _guided_action(engine)
    intent = engine._action_router.fire_intent(action)  # noqa: SLF001
    staged = engine.stage_guided_fire_intent(intent)
    recovery = staged.effect_bundle.resource_effects[0].recovery_updates
    assert tuple(value.recovery_remaining_ms for value in recovery) == (
        75_000,
        75_000,
    )

    accepted = engine.step([action], delta_time_ms=1)
    assert accepted.decisions[0].accepted is True
    world = engine._world  # noqa: SLF001
    assert world is not None
    assert world.operators[10].guided_fire_recovery_remaining_ms == 74_999
    assert world.operators[20].guided_fire_recovery_remaining_ms == 74_999

    engine.step([], delta_time_ms=74_998)
    assert world.operators[10].guided_fire_recovery_remaining_ms == 1
    assert world.operators[20].guided_fire_recovery_remaining_ms == 1
    assert engine.get_guided_fire_capabilities("red") == ()

    engine.step([], delta_time_ms=1)
    assert world.operators[10].guided_fire_recovery_remaining_ms == 0
    assert world.operators[20].guided_fire_recovery_remaining_ms == 0
    assert engine.get_guided_fire_capabilities("red")


def test_guided_recovery_exact_and_overshoot_clamp_to_zero() -> None:
    exact = _guided_engine()
    exact.step([_guided_action(exact)], delta_time_ms=75_000)
    exact_world = exact._world  # noqa: SLF001
    assert exact_world is not None
    assert exact_world.operators[10].guided_fire_recovery_remaining_ms == 0
    assert exact_world.operators[20].guided_fire_recovery_remaining_ms == 0
    assert exact.get_guided_fire_capabilities("red")

    overshoot = _guided_engine()
    overshoot.step([_guided_action(overshoot)], delta_time_ms=80_000)
    overshoot_world = overshoot._world  # noqa: SLF001
    assert overshoot_world is not None
    assert overshoot_world.operators[10].guided_fire_recovery_remaining_ms == 0
    assert overshoot_world.operators[20].guided_fire_recovery_remaining_ms == 0
    assert overshoot.get_guided_fire_capabilities("red")


def test_forged_guided_action_during_recovery_is_atomic_and_does_not_reset() -> None:
    engine = _guided_engine()
    engine.step([_guided_action(engine)], delta_time_ms=1)
    forged = EngineAction(
        side="red",
        actor_id=20,
        action_type=ActionType.FIRE,
        target_id=31,
        parameters={
            "fire_mode": "GUIDED",
            "guider_id": 20,
            "shooter_id": 10,
            "weapon_id": 73,
        },
    )
    rng_before = deepcopy(engine.snapshot()["rng"])

    result = engine.step([forged], delta_time_ms=1)

    assert result.decisions[0].accepted is False
    assert result.decisions[0].code == "GUIDED_FIRE_CAPABILITY_NOT_AVAILABLE"
    assert engine.snapshot()["rng"] == rng_before
    world = engine._world  # noqa: SLF001
    assert world is not None
    assert world.operators[10].guided_fire_recovery_remaining_ms == 74_998
    assert world.operators[20].guided_fire_recovery_remaining_ms == 74_998


def test_guided_recovery_snapshot_restore_and_strict_replay(tmp_path: Path) -> None:
    engine = _guided_engine()
    action = _guided_action(engine)
    engine.step([action], delta_time_ms=1)
    engine.step([], delta_time_ms=74_998)
    snapshot = deepcopy(engine.snapshot())
    restored = _guided_engine()
    restored.restore(snapshot)
    assert restored.snapshot() == snapshot
    restored.step([], delta_time_ms=1)
    assert restored.get_guided_fire_capabilities("red")

    replay_engine = _guided_engine()
    replay_action = _guided_action(replay_engine)
    final_snapshot, _ = _write_replay(
        tmp_path / "guided-recovery",
        replay_engine,
        (([replay_action], 1), ([], 74_998), ([], 1)),
    )
    operators = final_snapshot["world"]["operators"]
    assert operators["10"].get("guided_fire_recovery_remaining_ms", 0) == 0
    assert operators["20"].get("guided_fire_recovery_remaining_ms", 0) == 0


def test_indirect_planned_cooldown_set_partial_exact_and_overshoot() -> None:
    engine = _indirect_engine()
    _create_indirect_mission(engine)
    world = engine._world  # noqa: SLF001
    assert world is not None
    shooter = world.operators[1]
    assert shooter.indirect_fire_planned_cooldown_remaining_ms == 300_000
    blocked = engine.get_indirect_fire_capabilities("red")[0]
    assert blocked.status is IndirectFireCapabilityStatus.BLOCKED
    assert blocked.blockers == (
        "indirect_fire_planned_cooldown_remaining",
    )

    engine.step([], delta_time_ms=299_999)
    assert shooter.indirect_fire_planned_cooldown_remaining_ms == 1
    assert engine.get_indirect_fire_capabilities("red")[0].status is (
        IndirectFireCapabilityStatus.BLOCKED
    )
    engine.step([], delta_time_ms=1)
    assert shooter.indirect_fire_planned_cooldown_remaining_ms == 0
    assert engine.get_indirect_fire_capabilities("red")[0].status is (
        IndirectFireCapabilityStatus.AVAILABLE
    )

    overshoot = _indirect_engine()
    _create_indirect_mission(overshoot)
    overshoot.step([], delta_time_ms=400_000)
    assert (
        overshoot._world.operators[1]  # type: ignore[union-attr]  # noqa: SLF001
        .indirect_fire_planned_cooldown_remaining_ms
        == 0
    )


def test_indirect_cooldown_forged_action_rejected_without_resource_or_rng_use() -> None:
    engine = _indirect_engine()
    capability = engine.get_indirect_fire_capabilities("red")[0]
    forged = engine.indirect_fire_action(capability, 505)
    _create_indirect_mission(engine)
    before = deepcopy(engine.snapshot())

    result = engine.step([forged], delta_time_ms=1)

    assert result.decisions[0].accepted is False
    assert result.decisions[0].code == "INDIRECT_FIRE_PLANNED_COOLDOWN_ACTIVE"
    after = engine.snapshot()
    assert after["rng"] == before["rng"]
    assert len(after["world"]["indirect_fire_missions"]) == 1
    assert (
        after["world"]["operators"]["1"]["raw_attributes"]["ammunition"]
        == before["world"]["operators"]["1"]["raw_attributes"]["ammunition"]
    )
    assert (
        engine._world.operators[1]  # type: ignore[union-attr]  # noqa: SLF001
        .indirect_fire_planned_cooldown_remaining_ms
        == 299_999
    )


def test_indirect_planned_cooldown_is_per_operator() -> None:
    engine = _indirect_engine()
    world = engine._world  # noqa: SLF001
    assert world is not None
    second = type(world.operators[1]).from_dict(world.operators[1].to_dict())
    second.operator_id = 3
    second.current_hex = 102
    second.x = 2.0
    world.operators[3] = second

    capabilities = engine.get_indirect_fire_capabilities("red")
    first = next(value for value in capabilities if value.shooter_id == 1)
    plan = engine.build_indirect_fire_plan_request(first, 505)
    assert plan.request is not None
    engine.create_indirect_fire_mission(plan.request)

    current = {
        value.shooter_id: value
        for value in engine.get_indirect_fire_capabilities("red")
    }
    assert current[1].status is IndirectFireCapabilityStatus.BLOCKED
    assert current[3].status is IndirectFireCapabilityStatus.AVAILABLE
    assert world.operators[1].indirect_fire_planned_cooldown_remaining_ms == 300_000
    assert world.operators[3].indirect_fire_planned_cooldown_remaining_ms == 0


def test_indirect_cooldown_snapshot_restore_and_strict_replay(tmp_path: Path) -> None:
    engine = _indirect_engine()
    _create_indirect_mission(engine)
    engine.step([], delta_time_ms=299_999)
    snapshot = deepcopy(engine.snapshot())
    restored = _indirect_engine()
    restored.restore(snapshot)
    assert restored.snapshot() == snapshot
    restored.step([], delta_time_ms=1)
    assert restored.get_indirect_fire_capabilities("red")[0].status is (
        IndirectFireCapabilityStatus.AVAILABLE
    )

    replay_engine = _indirect_engine()
    action = replay_engine.indirect_fire_action(
        replay_engine.get_indirect_fire_capabilities("red")[0],
        505,
    )
    final_snapshot, _ = _write_replay(
        tmp_path / "indirect-cooldown",
        replay_engine,
        (([action], 1), ([], 299_998), ([], 1)),
    )
    shooter = final_snapshot["world"]["operators"]["1"]
    assert shooter.get("indirect_fire_planned_cooldown_remaining_ms", 0) == 0


def test_indirect_phase_exact_overshoot_and_subsecond_residual() -> None:
    staying = _indirect_engine()
    action = staying.indirect_fire_action(
        staying.get_indirect_fire_capabilities("red")[0], 505
    )
    staying.step([action], delta_time_ms=149_999)
    mission = staying._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001
    assert mission.phase is FireMissionPhase.FLYING
    assert mission.remaining_flight_seconds == 1
    assert mission.flight_elapsed_remainder_ms == 999

    exact = _indirect_engine()
    action = exact.indirect_fire_action(
        exact.get_indirect_fire_capabilities("red")[0], 505
    )
    exact.step([action], delta_time_ms=150_000)
    mission = exact._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001
    assert mission.phase is FireMissionPhase.ACTIVE
    assert mission.active_remaining_seconds == 300
    assert mission.active_elapsed_remainder_ms == 0

    overshoot = _indirect_engine()
    action = overshoot.indirect_fire_action(
        overshoot.get_indirect_fire_capabilities("red")[0], 505
    )
    overshoot.step([action], delta_time_ms=150_500)
    mission = overshoot._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001
    assert mission.phase is FireMissionPhase.ACTIVE
    assert mission.active_remaining_seconds == 300
    assert mission.active_elapsed_remainder_ms == 500

    subsecond = _indirect_engine()
    action = subsecond.indirect_fire_action(
        subsecond.get_indirect_fire_capabilities("red")[0], 505
    )
    subsecond.step([action], delta_time_ms=149_500)
    subsecond.step([], delta_time_ms=1_000)
    mission = subsecond._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001
    assert mission.phase is FireMissionPhase.ACTIVE
    assert mission.active_remaining_seconds == 300
    assert mission.active_elapsed_remainder_ms == 500


def test_indirect_phase_can_cross_flying_active_and_resolved_in_one_step() -> None:
    engine = _indirect_engine()
    action = engine.indirect_fire_action(
        engine.get_indirect_fire_capabilities("red")[0], 505
    )

    result = engine.step([action], delta_time_ms=450_000)

    mission = engine._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001
    assert mission.phase is FireMissionPhase.RESOLVED
    assert mission.active_remaining_seconds == 0
    assert {
        EventType.INDIRECT_FIRE_MISSION_CREATED,
        EventType.INDIRECT_FIRE_ACTIVE_STARTED,
        EventType.INDIRECT_FIRE_AREA_TRIGGERED,
        EventType.INDIRECT_FIRE_RESOLVED,
    }.issubset({event.event_type for event in result.events})


def test_indirect_phase_residual_is_deterministic_and_strict_replayable(
    tmp_path: Path,
) -> None:
    left = _indirect_engine()
    right = _indirect_engine()
    left_action = left.indirect_fire_action(
        left.get_indirect_fire_capabilities("red")[0], 505
    )
    right_action = right.indirect_fire_action(
        right.get_indirect_fire_capabilities("red")[0], 505
    )
    left_result = left.step([left_action], delta_time_ms=150_500)
    right_result = right.step([right_action], delta_time_ms=150_500)
    assert left.snapshot() == right.snapshot()
    assert tuple(value.to_dict() for value in left_result.events) == tuple(
        value.to_dict() for value in right_result.events
    )

    replay_engine = _indirect_engine()
    action = replay_engine.indirect_fire_action(
        replay_engine.get_indirect_fire_capabilities("red")[0], 505
    )
    final_snapshot, events = _write_replay(
        tmp_path / "indirect-residual",
        replay_engine,
        (([action], 450_000),),
    )
    mission = final_snapshot["world"]["indirect_fire_missions"][0]
    assert mission["phase"] == FireMissionPhase.RESOLVED.value
    assert any(
        event["event_type"] == EventType.INDIRECT_FIRE_RESOLVED.value
        for event in events
    )
