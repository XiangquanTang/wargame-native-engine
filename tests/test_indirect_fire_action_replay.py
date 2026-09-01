from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from test_indirect_fire_effect_resolution import _hexes
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.core.indirect_fire import FireMissionPhase
from wargame_native_engine.io.replay import (
    ReplayReader,
    ReplayWriter,
    UnsupportedReplaySemanticsError,
    replay_actions,
)
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = REPOSITORY_ROOT / "examples/demos/direct_fire_basic/scenario.json"


def _scenario():
    payload = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    payload["scenario_id"] = "indirect-fire-action-replay-test"
    payload["map"].update({
        "map_id": "indirect-action-9x9",
        "width": 9,
        "height": 9,
        "hexes": _hexes(),
    })
    payload["terminal_rules"]["max_ticks"] = 500
    shooter = payload["operators"][0]
    shooter["weapon_ids"] = [72]
    shooter.update({"current_hex": 101, "x": 1, "y": 1})
    target = payload["operators"][1]
    target.update({"current_hex": 505, "x": 5, "y": 5})
    target["raw_attributes"]["armor_class"] = "LIGHT"
    payload["deployment"]["positions"]["red"]["1"] = [1, 1]
    payload["deployment"]["positions"]["blue"]["2"] = [5, 5]
    return scenario_from_dict(payload)


def _engine_and_action(*, seed: int = 5):
    engine = NativeEngine(_scenario())
    engine.reset(seed)
    capability = engine.get_indirect_fire_capabilities("red")[0]
    action = engine.indirect_fire_action(capability, 505)
    return engine, action


def _run_complete(engine: NativeEngine, action: EngineAction):
    created = engine.step([action], delta_time_ms=1_000)
    assert engine._world is not None  # noqa: SLF001
    assert engine._world.indirect_fire_missions[0].phase is FireMissionPhase.FLYING  # noqa: SLF001
    active = engine.step([], delta_time_ms=149_000)
    assert engine._world.indirect_fire_missions[0].phase is FireMissionPhase.ACTIVE  # noqa: SLF001
    resolved = engine.step([], delta_time_ms=300_000)
    assert engine._world.indirect_fire_missions[0].phase is FireMissionPhase.RESOLVED  # noqa: SLF001
    return created, active, resolved


def test_explicit_fire_indirect_action_creates_and_resolves_mission() -> None:
    engine, action = _engine_and_action()

    assert action.action_type is ActionType.FIRE
    assert action.parameters["fire_mode"] == "INDIRECT"
    created, active, resolved = _run_complete(engine, action)

    assert created.decisions[0].accepted is True
    assert [event.event_type for event in created.events] == [
        EventType.INDIRECT_FIRE_MISSION_CREATED,
    ]
    assert EventType.INDIRECT_FIRE_ACTIVE_STARTED in {
        event.event_type for event in active.events
    }
    assert EventType.INDIRECT_FIRE_AREA_TRIGGERED in {
        event.event_type for event in active.events
    }
    assert EventType.INDIRECT_FIRE_RESOLVED in {
        event.event_type for event in resolved.events
    }
    assert engine._world.operators[1].raw_attributes["ammunition"]["0"] == 19  # noqa: SLF001
    assert engine._world.operators[2].blood == 2  # noqa: SLF001


def test_invalid_hex_and_policy_identity_are_rejected_atomically() -> None:
    engine, action = _engine_and_action()
    before = deepcopy(engine.snapshot())
    events_before = engine._last_events  # noqa: SLF001
    bad_hex = EngineAction.from_dict({
        **action.to_dict(),
        "parameters": {**action.parameters, "target_hex": 9999},
    })
    result = engine.step([bad_hex], delta_time_ms=1_000)
    assert result.decisions[0].accepted is False
    assert result.decisions[0].code == "TARGET_HEX_NOT_FOUND"
    # A rejected tick still advances logical time, but it cannot create a
    # mission, consume ammo/RNG, or emit an indirect-fire event.
    assert engine._world is not None  # noqa: SLF001
    assert engine._world.indirect_fire_missions == ()  # noqa: SLF001
    assert engine._world.operators[1].raw_attributes["ammunition"]["0"] == 20  # noqa: SLF001
    assert engine.snapshot()["rng"] == before["rng"]
    assert not any(
        event.event_type is EventType.INDIRECT_FIRE_MISSION_CREATED
        for event in engine._last_events  # noqa: SLF001
    )
    assert tuple(
        event.event_type for event in events_before
    ) == (EventType.ENGINE_RESET,)

    engine, action = _engine_and_action()
    mismatch = EngineAction.from_dict({
        **action.to_dict(),
        "parameters": {
            **action.parameters,
            "execution_policy_hash": "0" * 64,
        },
    })
    result = engine.step([mismatch], delta_time_ms=1_000)
    assert result.decisions[0].accepted is False
    assert result.decisions[0].code == "POLICY_HASH_MISMATCH"
    assert engine._world is not None  # noqa: SLF001
    assert engine._world.indirect_fire_missions == ()  # noqa: SLF001


def test_indirect_action_strict_replay_reexecutes_world_rng_and_events(
    tmp_path: Path,
) -> None:
    scenario = _scenario()
    engine = NativeEngine(scenario)
    engine.reset(5)
    writer = ReplayWriter(
        tmp_path / "indirect-replay",
        run_id="indirect-action-replay",
        scenario=scenario,
        seed=5,
        agents={"red": "test", "blue": "test"},
        max_ticks=engine.terminal_rules.max_ticks,
        ruleset=engine.ruleset,
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    capability = engine.get_indirect_fire_capabilities("red")[0]
    action = engine.indirect_fire_action(capability, 505)
    created = engine.step([action], delta_time_ms=1_000)
    writer.record_step(created, engine.snapshot())
    active = engine.step([], delta_time_ms=149_000)
    writer.record_step(active, engine.snapshot())
    resolved = engine.step([], delta_time_ms=300_000)
    writer.record_step(resolved, engine.snapshot())
    final_snapshot = deepcopy(engine.snapshot())
    final_events = tuple(
        event.to_dict()
        for event in (*created.events, *active.events, *resolved.events)
    )
    writer.finalize({"result": engine.result().to_dict()}, status="complete")

    bundle = ReplayReader().read(writer.root)
    assert bundle["manifest"]["indirect_fire_authority_version"]
    assert tuple(bundle["events"]) == final_events
    verification = replay_actions(scenario, writer.root, strict=True)
    assert verification.matches is True
    assert verification.expected_world_hash == canonical_hash(final_snapshot["world"])


def test_indirect_replay_authority_mismatch_fails_before_engine_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario()
    engine = NativeEngine(scenario)
    engine.reset(5)
    writer = ReplayWriter(
        tmp_path / "indirect-mismatch",
        run_id="indirect-authority-mismatch",
        scenario=scenario,
        seed=5,
        agents={"red": "test", "blue": "test"},
        max_ticks=engine.terminal_rules.max_ticks,
        ruleset=engine.ruleset,
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    action = engine.indirect_fire_action(
        engine.get_indirect_fire_capabilities("red")[0], 505
    )
    writer.record_step(engine.step([action], delta_time_ms=1_000), engine.snapshot())
    writer.finalize({"result": engine.result().to_dict()}, status="complete")
    manifest_path = writer.root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["damage_policy_hash"] = "0" * 64
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    def constructed(*args, **kwargs):
        raise AssertionError("engine must not be constructed")

    monkeypatch.setattr("wargame_native_engine.io.replay.NativeEngine", constructed)
    with pytest.raises(
        UnsupportedReplaySemanticsError,
        match="damage_policy_hash",
    ):
        replay_actions(scenario, writer.root, strict=True)
