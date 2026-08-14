from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash


BASE_SCENARIO = Path("examples/source_movement_scenario.json")


def _document() -> dict:
    value = json.loads(BASE_SCENARIO.read_text(encoding="utf-8"))
    cells = value["map"]["hexes"]
    cells[-1]["neighbors"][0] = 107
    cells.append({
        "pos": 107,
        "elev": 0,
        "cond": 0,
        "neighbors": [-1, -1, -1, 106, -1, -1],
        "roads": [0, 0, 0, 0, 0, 0],
        "rivers": [0, 0, 0, 0, 0, 0],
    })
    blue = next(item for item in value["operators"] if item["side"] == "blue")
    blue.update({"current_hex": 107, "x": 7, "y": 1})
    value["deployment"]["positions"]["blue"][str(blue["operator_id"])] = [7, 1]
    value["map"]["width"] = max(float(value["map"]["width"]), 1400)
    return value


def _engine(document: dict | None = None) -> NativeEngine:
    engine = NativeEngine(scenario_from_dict(document or _document()))
    engine.reset(42)
    return engine


def test_plans_five_segment_route_deterministically_without_state_or_rng_change() -> None:
    engine = _engine()
    before = deepcopy(engine.snapshot())
    first = engine.plan_move("red", 1, 106, "NORMAL")
    second = engine.plan_move("red", 1, 106, "NORMAL")

    assert first == second
    assert first.reachable is True
    assert first.ordered_path == (102, 103, 104, 105, 106)
    assert len(first.segments) == 5
    assert all(
        left.to_hex == right.from_hex
        for left, right in zip(first.segments, first.segments[1:])
    )
    assert first.estimated_duration_ms == sum(
        item.traversal_ms for item in first.segments
    )
    assert engine.snapshot() == before


def test_authoritative_estimate_matches_segment_execution() -> None:
    engine = _engine()
    route = engine.plan_move("red", 1, 106)
    assert route.estimated_duration_ms is not None
    action = EngineAction(
        "red", 1, ActionType.MOVE,
        parameters={"path": list(route.ordered_path), "movement_mode": "NORMAL"},
    )
    result = engine.step(
        [action], delta_time_seconds=route.estimated_duration_ms / 1000
    )

    world = engine.snapshot()["world"]
    unit = world["operators"]["1"]
    assert result.decisions[0].accepted is True
    assert unit["current_hex"] == 106
    assert unit["move_path"] == []
    assert len(world["movement_evidence"]) == 5


def test_roads_and_terrain_use_same_segment_cost_source() -> None:
    normal = _document()
    slower = deepcopy(normal)
    for cell in slower["map"]["hexes"]:
        if cell["pos"] in {102, 103, 104, 105, 106}:
            cell["cond"] = 2
    road = deepcopy(slower)
    by_pos = {cell["pos"]: cell for cell in road["map"]["hexes"]}
    for left, right in zip(range(101, 106), range(102, 107)):
        by_pos[left]["roads"][0] = 1
        by_pos[right]["roads"][3] = 1

    normal_plan = _engine(normal).plan_move("red", 1, 106)
    slow_plan = _engine(slower).plan_move("red", 1, 106)
    road_plan = _engine(road).plan_move("red", 1, 106)
    assert slow_plan.estimated_duration_ms > normal_plan.estimated_duration_ms
    assert road_plan.estimated_duration_ms < slow_plan.estimated_duration_ms
    assert all(segment.road_override for segment in road_plan.segments)


def test_unreachable_and_invalid_operator_states_are_structured() -> None:
    document = _document()
    cell = next(item for item in document["map"]["hexes"] if item["pos"] == 103)
    cell["cond"] = 5
    unreachable = _engine(document).plan_move("red", 1, 106)
    assert unreachable.reachable is False
    assert unreachable.reason_code == "DESTINATION_UNREACHABLE"
    assert unreachable.ordered_path == ()

    engine = _engine()
    engine._require_world().operators[1].state = "SUPPRESSED"
    suppressed = engine.plan_move("red", 1, 106)
    assert suppressed.reason_code == "ACTOR_SUPPRESSED"
    with pytest.raises(ValueError, match="not controlled"):
        engine.plan_move("blue", 1, 106)


def test_returned_route_is_immutable_and_hash_stable() -> None:
    plan = _engine().plan_move("red", 1, 106)
    record = plan.to_dict()
    expected = canonical_hash(record)
    record["ordered_path"].append(999)
    record["segment_details"][0]["to_hex"] = 999
    assert canonical_hash(plan.to_dict()) == expected
