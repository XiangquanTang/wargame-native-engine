from __future__ import annotations

from copy import deepcopy

import pytest

from test_indirect_fire_action_replay import _scenario
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.core.indirect_fire import FireMissionPhase
from wargame_native_engine.io.serialization import canonical_hash


def _active_engine(*, seed: int = 5) -> NativeEngine:
    engine = NativeEngine(_scenario())
    engine.reset(seed)
    action = engine.indirect_fire_action(
        engine.get_indirect_fire_capabilities("red")[0], 505
    )
    engine.step([action], delta_time_ms=1_000)
    result = engine.step([], delta_time_ms=149_000)
    assert EventType.INDIRECT_FIRE_ACTIVE_STARTED in {
        event.event_type for event in result.events
    }
    return engine


def _add_blue_operator(engine: NativeEngine, operator_id: int, hex_id: int) -> None:
    world = engine._world  # noqa: SLF001
    assert world is not None
    source = world.operators[2]
    clone = type(source).from_dict(source.to_dict())
    clone.operator_id = operator_id
    clone.current_hex = hex_id
    clone.x = float(hex_id // 100)
    clone.y = float(hex_id % 100)
    world.operators[operator_id] = clone


def test_active_area_persists_impact_and_full_300_seconds() -> None:
    engine = _active_engine()
    mission = engine._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001

    assert mission.phase is FireMissionPhase.ACTIVE
    assert mission.impact_hex == 505
    assert mission.active_remaining_seconds == 300
    assert mission.active_started_tick == 150

    engine.step([], delta_time_ms=299_999)
    mission = engine._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001
    assert mission.phase is FireMissionPhase.ACTIVE
    assert mission.active_remaining_seconds == 1
    assert mission.active_elapsed_remainder_ms == 999

    result = engine.step([], delta_time_ms=1)
    mission = engine._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001
    assert mission.phase is FireMissionPhase.RESOLVED
    assert mission.active_remaining_seconds == 0
    assert [event.event_type for event in result.events] == [
        EventType.INDIRECT_FIRE_RESOLVED
    ]


def test_first_entry_triggers_once_and_reentry_never_retriggers() -> None:
    engine = _active_engine()
    mission = engine._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001
    assert mission.affected_operator_ids == (2,)
    rng_after_initial = deepcopy(engine.snapshot()["rng"])

    engine._world.operators[2].current_hex = 404  # type: ignore[union-attr]  # noqa: SLF001
    engine.step([], delta_time_ms=1_000)
    engine._world.operators[2].current_hex = 505  # type: ignore[union-attr]  # noqa: SLF001
    result = engine.step([], delta_time_ms=1_000)

    assert not any(
        event.event_type is EventType.INDIRECT_FIRE_AREA_TRIGGERED
        for event in result.events
    )
    assert engine.snapshot()["rng"] == rng_after_initial
    assert engine._world.indirect_fire_missions[0].affected_operator_ids == (2,)  # type: ignore[union-attr]  # noqa: SLF001


def test_new_entrant_triggers_and_is_added_to_ledger() -> None:
    engine = _active_engine()
    _add_blue_operator(engine, 3, 404)
    before_rng = deepcopy(engine.snapshot()["rng"])
    engine._world.operators[3].current_hex = 505  # type: ignore[union-attr]  # noqa: SLF001

    result = engine.step([], delta_time_ms=1_000)

    triggered = [event for event in result.events
                 if event.event_type is EventType.INDIRECT_FIRE_AREA_TRIGGERED]
    assert [event.target_id for event in triggered] == [3]
    assert engine._world.indirect_fire_missions[0].affected_operator_ids == (2, 3)  # type: ignore[union-attr]  # noqa: SLF001
    assert engine.snapshot()["rng"]["draws"] == before_rng["draws"] + 3


def test_same_tick_operators_use_id_order_and_deterministic_rng() -> None:
    def run() -> tuple[list[int | None], dict[str, int], str]:
        engine = NativeEngine(_scenario())
        engine.reset(5)
        _add_blue_operator(engine, 3, 505)
        action = engine.indirect_fire_action(
            engine.get_indirect_fire_capabilities("red")[0], 505
        )
        engine.step([action], delta_time_ms=1_000)
        result = engine.step([], delta_time_ms=149_000)
        targets = [event.target_id for event in result.events
                   if event.event_type is EventType.INDIRECT_FIRE_AREA_TRIGGERED]
        return targets, engine.snapshot()["rng"], canonical_hash(
            engine.snapshot()["world"]
        )

    left = run()
    right = run()
    assert left == right
    assert left[0] == [2, 3]
    assert left[1]["draws"] == 9


def test_snapshot_restore_preserves_active_area_and_ledger() -> None:
    engine = _active_engine()
    snapshot = deepcopy(engine.snapshot())
    restored = NativeEngine(_scenario())
    restored.reset(5)

    restored.restore(snapshot)

    assert restored.snapshot() == snapshot
    mission = restored._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001
    assert mission.impact_hex == 505
    assert mission.affected_operator_ids == (2,)


def test_viewer_projection_is_read_only_and_side_filtered() -> None:
    engine = _active_engine()
    before = deepcopy(engine.snapshot())

    red = engine.get_indirect_fire_active_areas("red")
    blue = engine.get_indirect_fire_active_areas("blue")

    assert [value.impact_hex for value in red] == [505]
    assert red[0].active_remaining_seconds == 300
    assert red[0].affected_operator_ids == (2,)
    assert blue == ()
    assert engine.snapshot() == before


def test_waiting_and_active_mission_area_phases_are_side_filtered() -> None:
    engine = NativeEngine(_scenario())
    engine.reset(5)
    action = engine.indirect_fire_action(
        engine.get_indirect_fire_capabilities("red")[0], 505
    )
    engine.step([action], delta_time_ms=1_000)

    waiting = engine.get_indirect_fire_mission_areas("red")
    assert len(waiting) == 1
    assert waiting[0].phase is FireMissionPhase.FLYING
    assert waiting[0].planned_hex == 505
    assert waiting[0].display_hex == 505
    assert engine.get_indirect_fire_mission_areas("blue") == ()

    engine.step([], delta_time_ms=149_000)
    active = engine.get_indirect_fire_mission_areas("red")
    assert len(active) == 1
    assert active[0].phase is FireMissionPhase.ACTIVE
    assert active[0].display_hex == 505
    assert active[0].active_remaining_seconds == 300


def test_active_trigger_failure_rolls_back_world_rng_events_and_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _active_engine()
    _add_blue_operator(engine, 3, 505)
    before = deepcopy(engine.snapshot())
    events_before = engine._last_events  # noqa: SLF001

    def fail(working, bundle):
        working.operators[3].blood = 0
        raise RuntimeError("active-area-injected-failure")

    monkeypatch.setattr(engine, "_apply_indirect_fire_damage_effects", fail)
    with pytest.raises(RuntimeError, match="active-area-injected-failure"):
        engine.step([], delta_time_ms=1_000)

    assert engine.snapshot() == before
    assert engine._last_events == events_before  # noqa: SLF001
