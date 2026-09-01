from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, replace
import json
from pathlib import Path

import pytest

from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.core.indirect_fire import (
    FireMission,
    FireMissionPhase,
)
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.rules.indirect_fire import (
    IndirectFireCancellationReason,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = (
    REPOSITORY_ROOT / "examples/demos/direct_fire_basic/scenario.json"
)


def _engine() -> NativeEngine:
    payload = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    payload["scenario_id"] = "indirect-fire-mission-runtime-test"
    payload["terminal_rules"]["max_ticks"] = 500
    payload["operators"][0]["weapon_ids"] = [72]
    engine = NativeEngine(scenario_from_dict(payload))
    engine.reset(42)
    return engine


def _plan(engine: NativeEngine, planned_hex: int = 102):
    capability = engine.get_indirect_fire_capabilities("red")[0]
    result = engine.build_indirect_fire_plan_request(capability, planned_hex)
    assert result.valid is True
    assert result.request is not None
    return result.request


def _mission(engine: NativeEngine, planned_hex: int = 102) -> FireMission:
    return engine.create_indirect_fire_mission(_plan(engine, planned_hex))


def _ammo(engine: NativeEngine) -> int:
    world = engine._world  # noqa: SLF001
    assert world is not None
    return engine._ammunition(world.operators[1])["0"]  # noqa: SLF001


def test_plan_request_creates_initial_planned_mission_and_event() -> None:
    engine = _engine()

    mission = _mission(engine)

    assert mission.phase is FireMissionPhase.PLANNED
    assert mission.created_tick == 0
    assert mission.created_at == 0
    assert mission.shooter_id == 1
    assert mission.weapon_id == 72
    assert mission.planned_hex == 102
    assert mission.profile_id == "INDIRECT_HEAVY_ARTILLERY_72"
    assert mission.authority_version == "native-indirect-fire-authority/1.1"
    assert mission.remaining_flight_seconds == 150
    assert mission.ammo_consumed == 1
    assert mission.ammunition_key == "0"
    assert mission.cancelable is False
    assert engine._last_events[0].event_type is (  # noqa: SLF001
        EventType.INDIRECT_FIRE_MISSION_CREATED
    )


def test_successful_creation_consumes_ammunition_once() -> None:
    engine = _engine()
    assert _ammo(engine) == 20

    _mission(engine)

    assert _ammo(engine) == 19


def test_failed_creation_with_unresolved_ammo_mapping_is_atomic() -> None:
    engine = _engine()
    request = _plan(engine)
    engine._indirect_fire_ammunition_authority = replace(  # noqa: SLF001
        engine._indirect_fire_ammunition_authority,  # noqa: SLF001
        mappings=(),
    )
    before = deepcopy(engine.snapshot())
    before_events = engine._last_events  # noqa: SLF001

    with pytest.raises(ValueError, match="INDIRECT_FIRE_PLAN_REQUEST_STALE"):
        engine.create_indirect_fire_mission(request)

    assert engine.snapshot() == before
    assert engine._last_events == before_events  # noqa: SLF001


def test_mission_id_is_deterministic() -> None:
    left = _engine()
    right = _engine()

    left_mission = _mission(left)
    right_mission = _mission(right)

    assert left_mission.mission_id == right_mission.mission_id
    assert left_mission == right_mission


def test_multiple_missions_are_serialized_in_mission_id_order() -> None:
    engine = _engine()
    first = _mission(engine, 101)
    engine.step([], delta_time_ms=300_000)
    second = _mission(engine, 102)

    world = engine.snapshot()["world"]
    records = world["indirect_fire_missions"]
    assert [item["mission_id"] for item in records] == sorted(
        [first.mission_id, second.mission_id]
    )


def test_flight_reaches_active_after_exactly_150_seconds() -> None:
    engine = _engine()
    mission = _mission(engine)

    engine.step([], delta_time_ms=149_999)
    current = engine._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001
    assert current.phase is FireMissionPhase.FLYING
    assert current.remaining_flight_seconds == 1
    assert current.flight_elapsed_remainder_ms == 999
    assert engine.can_cancel_indirect_fire_mission(mission.mission_id) is True

    engine.step([], delta_time_ms=1)
    current = engine._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001
    assert current.phase is FireMissionPhase.ACTIVE
    assert current.remaining_flight_seconds == 0
    assert current.flight_elapsed_remainder_ms == 0


def test_flying_mission_can_be_cancelled_without_ammo_refund() -> None:
    engine = _engine()
    mission = _mission(engine)
    engine.step([], delta_time_ms=1_000)
    ammunition_after_create = _ammo(engine)

    cancelled = engine.cancel_indirect_fire_mission(
        mission.mission_id,
        IndirectFireCancellationReason.USER_CANCELLED,
    )

    assert cancelled.phase is FireMissionPhase.CANCELLED
    assert cancelled.cancelable is False
    assert cancelled.cancellation_reason is (
        IndirectFireCancellationReason.USER_CANCELLED
    )
    assert _ammo(engine) == ammunition_after_create
    assert engine._last_events[0].event_type is (  # noqa: SLF001
        EventType.INDIRECT_FIRE_MISSION_CANCELLED
    )
    assert engine._last_events[0].data["ammo_refunded"] == 0  # noqa: SLF001


def test_active_mission_cannot_be_cancelled() -> None:
    engine = _engine()
    mission = _mission(engine)
    engine.step([], delta_time_ms=150_000)
    before = deepcopy(engine.snapshot())

    assert engine.can_cancel_indirect_fire_mission(mission.mission_id) is False
    with pytest.raises(ValueError, match="INDIRECT_FIRE_MISSION_NOT_CANCELABLE"):
        engine.cancel_indirect_fire_mission(
            mission.mission_id,
            IndirectFireCancellationReason.USER_CANCELLED,
        )
    assert engine.snapshot() == before


def test_snapshot_restore_preserves_mission_exactly() -> None:
    source = _engine()
    _mission(source)
    source.step([], delta_time_ms=12_345)
    snapshot = source.snapshot()
    restored = _engine()

    restored.restore(snapshot)

    assert restored.snapshot() == snapshot
    assert restored._world.indirect_fire_missions == (  # type: ignore[union-attr]  # noqa: SLF001
        source._world.indirect_fire_missions  # type: ignore[union-attr]  # noqa: SLF001
    )


def test_world_hash_is_deterministic_with_mission_runtime() -> None:
    left = _engine()
    right = _engine()
    _mission(left)
    _mission(right)
    left.step([], delta_time_ms=10_250)
    right.step([], delta_time_ms=10_250)

    assert canonical_hash(left.snapshot()["world"]) == canonical_hash(
        right.snapshot()["world"]
    )


def test_mission_contract_contains_active_area_but_no_damage_bundle() -> None:
    names = {item.name for item in fields(_mission(_engine()))}

    assert "impact_hex" in names
    assert "active_remaining_seconds" in names
    assert "affected_operator_ids" in names
    assert "scatter_result" not in names
    assert "damage" not in names
    assert "effect_bundle" not in names
