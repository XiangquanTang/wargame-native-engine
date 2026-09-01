from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_guided_capability import _engine
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import (
    source_frame_template_for_scenario,
)
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.event import EventType
from wargame_native_engine.core.fire_intent import FireMode
from wargame_native_engine.io.replay import (
    ReplayWriter,
    replay_actions,
    replay_source_archive,
)
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.replay import SourceFrameRecorder
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)
from wargame_native_engine.version import ENGINE_VERSION


def _guided_action(engine):
    capability = engine.get_guided_fire_capabilities("red")[0]
    return engine.guided_fire_action(capability)


def test_guided_commit_succeeds_when_only_guider_observes_target() -> None:
    engine = _engine(target_concealed=True)
    world = engine._world
    assert world is not None
    assert 30 not in world.operators[10].raw_attributes["see_enemy_bop_ids"]
    assert 30 in world.operators[20].raw_attributes["see_enemy_bop_ids"]

    action = _guided_action(engine)
    assert action in tuple(item.action for item in engine.valid_actions("red"))
    result = engine.step([action], delta_time_seconds=1)

    assert result.decisions[0].accepted is True
    assert world.operators[30].blood == 0
    assert world.operators[30].alive is False
    guided = tuple(
        event
        for event in result.events
        if event.event_type is EventType.GUIDED_FIRE_RESOLVED
    )
    assert len(guided) == 1
    assert guided[0].data["fire_mode"] == FireMode.GUIDED.value
    assert guided[0].data["guider_id"] == 20
    assert guided[0].data["shooter_id"] == 10
    assert guided[0].data["weapon_id"] == 73
    assert guided[0].data["target_id"] == 30


def test_guided_commit_rejects_when_guider_does_not_observe_target() -> None:
    engine = _engine(
        shooter_hex=114,
        guider_hex=101,
        target_concealed=True,
    )
    action = EngineAction(
        side="red",
        actor_id=20,
        action_type=ActionType.FIRE,
        target_id=30,
        parameters={
            "fire_mode": FireMode.GUIDED.value,
            "guider_id": 20,
            "shooter_id": 10,
            "weapon_id": 73,
        },
    )
    snapshot_before = engine.snapshot()

    result = engine.step([action], delta_time_seconds=1)

    assert result.decisions[0].accepted is False
    assert result.decisions[0].code == "GUIDED_FIRE_CAPABILITY_NOT_AVAILABLE"
    snapshot_after = engine.snapshot()
    assert snapshot_after["rng"] == snapshot_before["rng"]
    assert snapshot_after["world"]["operators"] == (
        snapshot_before["world"]["operators"]
    )


def test_guided_commit_consumes_weapon_73_ammunition() -> None:
    engine = _engine(target_concealed=True)
    action = _guided_action(engine)

    engine.step([action], delta_time_seconds=1)

    world = engine._world
    assert world is not None
    assert world.operators[10].raw_attributes["ammunition"]["100"] == 9


def test_guided_commit_publishes_the_staged_rng_transition_without_reroll() -> None:
    engine = _engine(target_concealed=True)
    capability = engine.get_guided_fire_capabilities("red")[0]
    intent = engine._action_router.fire_intent(
        engine.guided_fire_action(capability)
    )
    staged = engine.stage_guided_fire_intent(intent)
    expected = staged.effect_bundle.rng_effect

    engine.step([engine.guided_fire_action(capability)], delta_time_seconds=1)

    assert engine.snapshot()["rng"] == {
        "seed": expected.seed,
        "state": expected.state_after,
        "draws": expected.draws_after,
    }


def test_guided_commit_sets_and_advances_shooter_and_guider_recovery() -> None:
    engine = _engine(target_concealed=True)

    engine.step([_guided_action(engine)], delta_time_seconds=1)

    world = engine._world
    assert world is not None
    # Guided effects set 75 seconds at the action boundary.  The same Engine
    # step then consumes its authoritative one-second delta, just like weapon
    # cooldown and every other Operator timer.
    assert world.operators[10].guided_fire_recovery_remaining_ms == 74_000
    assert world.operators[20].guided_fire_recovery_remaining_ms == 74_000


def test_guided_and_direct_weapon_73_share_damage_rng_model() -> None:
    direct = _engine(target_concealed=False)
    guided = _engine(target_concealed=False)
    direct.reset(5)
    guided.reset(5)
    direct_action = EngineAction(
        "red",
        10,
        ActionType.SHOOT,
        target_id=30,
        parameters={"weapon_id": 73},
    )

    direct_result = direct.step([direct_action], delta_time_seconds=1)
    guided_result = guided.step([_guided_action(guided)], delta_time_seconds=1)

    assert direct_result.decisions[0].accepted is True
    assert guided_result.decisions[0].accepted is True
    assert direct.snapshot()["rng"] == guided.snapshot()["rng"]
    direct_world = direct._world
    guided_world = guided._world
    assert direct_world is not None and guided_world is not None
    assert direct_world.operators[30].blood == guided_world.operators[30].blood
    assert direct_world.operators[30].alive == guided_world.operators[30].alive


def test_direct_and_guided_same_tick_share_prestate_and_aggregate_resources() -> None:
    engine = _engine(target_hexes=(115, 116), target_concealed=False)
    guided = _guided_action(engine)
    direct = EngineAction(
        "red",
        10,
        ActionType.SHOOT,
        target_id=31,
        parameters={"weapon_id": 73},
    )

    result = engine.step([guided, direct], delta_time_seconds=1)

    assert tuple(item.accepted for item in result.decisions) == (True, True)
    world = engine._world
    assert world is not None
    assert world.operators[30].alive is False
    assert world.operators[31].alive is False
    assert world.operators[10].raw_attributes["ammunition"]["100"] == 8
    assert engine.snapshot()["rng"]["draws"] == 8


def test_guided_commit_fault_rolls_back_world_rng_and_events(monkeypatch) -> None:
    engine = _engine(target_concealed=True)
    action = _guided_action(engine)
    snapshot_before = engine.snapshot()
    events_before = engine._last_events

    def fail(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("guided-commit-injected-failure")

    monkeypatch.setattr(engine, "_apply_source_fire_intents", fail)
    with pytest.raises(RuntimeError, match="guided-commit-injected-failure"):
        engine.step([action], delta_time_seconds=1)

    assert engine.snapshot() == snapshot_before
    assert engine._last_events == events_before


def test_guided_fire_round_trips_through_strict_replay(tmp_path: Path) -> None:
    engine = _engine(target_concealed=True)
    scenario = engine.scenario
    output = tmp_path / "guided-fire-replay"
    writer = ReplayWriter(
        output,
        run_id="guided-fire",
        scenario=scenario,
        seed=42,
        agents={"red": "test", "blue": "test"},
        max_ticks=engine.terminal_rules.max_ticks,
        ruleset=engine.ruleset,
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    action = _guided_action(engine)
    result = engine.step([action], delta_time_seconds=1)
    writer.record_step(result, engine.snapshot())
    writer.finalize({"result": engine.result().to_dict()}, status="complete")

    verification = replay_actions(scenario, output)

    assert verification.matches is True
    action_record = result.actions[0].to_dict()
    assert action_record == {
        "side": "red",
        "actor_id": 20,
        "action_type": "FIRE",
        "target_id": 30,
        "target_position": None,
        "parameters": {
            "fire_mode": "GUIDED",
            "guider_id": 20,
            "shooter_id": 10,
            "weapon_id": 73,
        },
    }


def test_guided_fire_is_recorded_in_green_source_replay(tmp_path: Path) -> None:
    source = _engine(target_concealed=True)
    scenario = source.scenario
    session = GameSession(NativeEngineBackend(scenario))
    environment = SourceCompatibleEnvironment(
        session,
        template=source_frame_template_for_scenario(scenario),
    )
    bundle = environment.setup_views(42)
    native = bundle.green["extra"]["native"]
    recorder = SourceFrameRecorder(tmp_path / "guided-source-facts")
    recorder.start({
        "scenario_id": scenario.scenario_id,
        "scenario_hash": scenario.scenario_hash,
        "terrain_id": bundle.green["terrain_id"],
        "episode_id": session.session_id,
        "engine_version": ENGINE_VERSION,
        "ruleset_id": native["ruleset_id"],
        "ruleset_version": native["ruleset_version"],
        "ruleset_hash": native["ruleset_hash"],
        "adjudication_table_set_id": native["adjudication_table_set_id"],
        "adjudication_table_set_version": native[
            "adjudication_table_set_version"
        ],
        "adjudication_table_set_hash": native[
            "adjudication_table_set_hash"
        ],
        "movement_policy_id": native["movement_policy_id"],
        "movement_policy_version": native["movement_policy_version"],
        "movement_policy_hash": native["movement_policy_hash"],
        "perspective_policy_id": native["perspective_policy_id"],
        "perspective_policy_version": native["perspective_policy_version"],
        "perspective_policy_hash": native["perspective_policy_hash"],
        "episode_seed": 42,
        "max_ticks": session.max_ticks,
    })
    recorder.record_initial(
        bundle.green,
        initial_state_hash=environment.initial_state_hash,
    )
    engine = session.backend.engine
    action = _guided_action(engine)

    bundle, _ = environment.step_views([action], delta_time_seconds=1)
    record = environment.last_step_record
    assert record is not None
    recorder.record_step(bundle.green, record)
    outcome = session.result().to_dict()
    recorder.finalize(outcome, final_state_hash=outcome["world_hash"])
    archive = recorder.package_zip(tmp_path / "guided-source-replay.zip")

    verification = replay_source_archive(scenario, archive, strict=True)
    assert verification.matches is True
    assert bundle.green["actions"][0]["action_type"] == "FIRE"
    judge_events = bundle.green["judge_info"][0]["result"]["events"]
    assert any(
        item.get("event_type") == "guided_fire_resolved"
        for item in judge_events
    )


def test_legacy_weapon_73_shoot_remains_direct() -> None:
    engine = _engine(target_concealed=False)
    action = EngineAction(
        "red",
        10,
        ActionType.SHOOT,
        target_id=30,
        parameters={"weapon_id": 73},
    )

    result = engine.step([action], delta_time_seconds=1)

    assert result.decisions[0].accepted is True
    assert all(
        event.event_type is not EventType.GUIDED_FIRE_RESOLVED
        for event in result.events
    )
    assert action.to_dict()["action_type"] == "SHOOT"
    assert "fire_mode" not in action.parameters


def test_guided_commit_world_hash_is_seed_deterministic() -> None:
    left = _engine(target_concealed=True)
    right = _engine(target_concealed=True)
    before = canonical_hash(left.snapshot()["world"])

    left.step([_guided_action(left)], delta_time_seconds=1)
    right.step([_guided_action(right)], delta_time_seconds=1)

    left_hash = canonical_hash(left.snapshot()["world"])
    right_hash = canonical_hash(right.snapshot()["world"])
    assert left_hash != before
    assert left_hash == right_hash
