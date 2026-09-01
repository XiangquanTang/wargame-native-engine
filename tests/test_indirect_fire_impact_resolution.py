from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, replace
import json
from pathlib import Path

from wargame_native_engine.adjudication.indirect_fire import (
    IndirectFireObservationPolicy,
)
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.indirect_fire import FireMissionPhase
from wargame_native_engine.io.scenario import scenario_from_dict


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = (
    REPOSITORY_ROOT / "examples/demos/direct_fire_basic/scenario.json"
)


def _hexes(size: int = 9) -> list[dict[str, object]]:
    existing = {
        row * 100 + column
        for row in range(1, size + 1)
        for column in range(1, size + 1)
    }
    result = []
    for row in range(1, size + 1):
        for column in range(1, size + 1):
            candidates = (
                row * 100 + column + 1,
                (row + 1) * 100 + column,
                (row + 1) * 100 + column - 1,
                row * 100 + column - 1,
                (row - 1) * 100 + column - 1,
                (row - 1) * 100 + column,
            )
            result.append({
                "pos": row * 100 + column,
                "elev": 0,
                "cond": 0,
                "neighbors": [
                    value if value in existing else -1 for value in candidates
                ],
                "roads": [0, 0, 0, 0, 0, 0],
                "rivers": [0, 0, 0, 0, 0, 0],
            })
    return result


def _engine(
    *,
    seed: int = 42,
    planned_hex: int = 505,
    observation_policy: IndirectFireObservationPolicy = (
        IndirectFireObservationPolicy.UNOBSERVED
    ),
) -> tuple[NativeEngine, str]:
    payload = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    payload["scenario_id"] = "indirect-fire-impact-resolution-test"
    payload["map"].update({
        "map_id": "indirect-impact-9x9",
        "width": 9,
        "height": 9,
        "hexes": _hexes(),
    })
    payload["terminal_rules"]["max_ticks"] = 500
    payload["operators"][0]["weapon_ids"] = [72]
    payload["operators"][0].update({"current_hex": 505, "x": 5, "y": 5})
    payload["operators"][1].update({"current_hex": 506, "x": 6, "y": 5})
    payload["deployment"]["positions"]["red"]["1"] = [5, 5]
    payload["deployment"]["positions"]["blue"]["2"] = [6, 5]
    engine = NativeEngine(scenario_from_dict(payload))
    engine._indirect_fire_observation_policy = observation_policy  # noqa: SLF001
    engine.reset(seed)
    capability = engine.get_indirect_fire_capabilities("red")[0]
    plan = engine.build_indirect_fire_plan_request(capability, planned_hex)
    assert plan.valid is True and plan.request is not None
    mission = engine.create_indirect_fire_mission(plan.request)
    return engine, mission.mission_id


def _active_engine(**kwargs: object) -> tuple[NativeEngine, str]:
    engine, mission_id = _engine(**kwargs)
    engine.step([], delta_time_ms=150_000)
    mission = engine._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001
    assert mission.phase is FireMissionPhase.ACTIVE
    return engine, mission_id


def test_active_mission_produces_pure_resolution_contracts() -> None:
    engine, mission_id = _active_engine()

    result = engine.resolve_indirect_fire_impact(mission_id)

    assert result.valid is True
    assert result.reason == "IMPACT_RESOLUTION_VALID"
    assert result.request is not None
    assert result.resolution is not None
    assert result.effect_bundle is not None
    assert result.request.mission_id == mission_id
    assert result.resolution.mission_id == mission_id
    assert result.effect_bundle.mission_id == mission_id


def test_flying_mission_cannot_resolve() -> None:
    engine, mission_id = _engine()
    engine.step([], delta_time_ms=149_999)

    result = engine.resolve_indirect_fire_impact(mission_id)

    assert result.valid is False
    assert result.reason == "MISSION_NOT_ACTIVE"
    assert result.blockers == ("mission_not_active",)


def test_scatter_is_deterministic_for_same_seed() -> None:
    left, left_id = _active_engine(seed=42)
    right, right_id = _active_engine(seed=42)

    left_result = left.resolve_indirect_fire_impact(left_id)
    right_result = right.resolve_indirect_fire_impact(right_id)

    assert left_result == right_result
    assert left_result.request is not None
    assert left_result.request.rng_after.draws - left_result.request.rng_before.draws == 3


def test_different_seed_changes_rng_or_scatter_evidence() -> None:
    left, left_id = _active_engine(seed=42)
    right, right_id = _active_engine(seed=43)

    left_request = left.resolve_indirect_fire_impact(left_id).request
    right_request = right.resolve_indirect_fire_impact(right_id).request

    assert left_request is not None and right_request is not None
    assert (
        left_request.scatter_result != right_request.scatter_result
        or left_request.rng_after != right_request.rng_after
    )


def test_planned_and_scattered_impact_hex_are_recorded_separately() -> None:
    engine, mission_id = _active_engine(seed=13)

    request = engine.resolve_indirect_fire_impact(mission_id).request

    assert request is not None
    assert request.scatter_result.scatter_distance > 0
    assert request.planned_hex == 505
    assert request.impact_hex != request.planned_hex


def test_scatter_outside_scenario_map_is_rejected() -> None:
    engine, mission_id = _active_engine(seed=15, planned_hex=101)

    result = engine.resolve_indirect_fire_impact(mission_id)

    assert result.valid is False
    assert result.reason == "IMPACT_HEX_OUT_OF_MAP"
    assert result.blockers == ("impact_hex",)


def test_affected_operators_are_selected_only_from_impact_hex_in_id_order() -> None:
    engine, mission_id = _active_engine(seed=13)
    first = engine.resolve_indirect_fire_impact(mission_id)
    assert first.request is not None
    impact_hex = first.request.impact_hex
    world = engine._world  # noqa: SLF001
    assert world is not None
    world.operators[2].current_hex = impact_hex
    extra = deepcopy(world.operators[2])
    extra.operator_id = 9
    world.operators[9] = extra
    extra = deepcopy(world.operators[2])
    extra.operator_id = 4
    world.operators[4] = extra

    result = engine.resolve_indirect_fire_impact(mission_id)

    assert result.resolution is not None
    assert result.resolution.affected_operator_ids == (2, 4, 9)
    assert result.effect_bundle is not None
    assert result.effect_bundle.affected_units == (2, 4, 9)


def test_resolution_does_not_modify_world_or_authoritative_rng() -> None:
    engine, mission_id = _active_engine(seed=42)
    before = deepcopy(engine.snapshot())
    before_events = engine._last_events  # noqa: SLF001

    result = engine.resolve_indirect_fire_impact(mission_id)

    assert result.valid is True
    assert engine.snapshot() == before
    assert engine.snapshot()["rng"] == before["rng"]
    assert engine._last_events == before_events  # noqa: SLF001


def test_unresolved_observation_policy_fails_closed_without_rng_draw() -> None:
    engine, mission_id = _active_engine()
    engine._indirect_fire_observation_policy = (  # noqa: SLF001
        IndirectFireObservationPolicy.UNRESOLVED
    )
    before = deepcopy(engine.snapshot())

    result = engine.resolve_indirect_fire_impact(mission_id)

    assert result.valid is False
    assert result.reason == "UNRESOLVED"
    assert result.blockers == ("observation_policy",)
    assert engine.snapshot() == before


def test_impact_contracts_contain_no_damage_or_unit_state_effects() -> None:
    engine, mission_id = _active_engine()
    result = engine.resolve_indirect_fire_impact(mission_id)
    assert result.resolution is not None
    assert result.effect_bundle is not None

    resolution_fields = {item.name for item in fields(result.resolution)}
    effect_fields = {item.name for item in fields(result.effect_bundle)}
    prohibited = {"damage", "suppression", "destroyed", "health", "blood"}
    assert resolution_fields.isdisjoint(prohibited)
    assert effect_fields.isdisjoint(prohibited)
