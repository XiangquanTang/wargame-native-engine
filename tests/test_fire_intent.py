from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from examples.run_joint_fire import run_example as run_joint_fire_example
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.action_router import ActionRouter
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.fire_intent import (
    FireIntent,
    FireMode,
    HexTarget,
    UnitTarget,
)
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash


SCENARIO = Path("examples/joint_fire_scenario.json")


def _shoot(side: str = "red", actor_id: int = 1,
           target_id: int = 2, weapon_id: int = 36) -> EngineAction:
    return EngineAction(
        side,
        actor_id,
        ActionType.SHOOT,
        target_id=target_id,
        parameters={"weapon_id": weapon_id},
    )


def _engine(seed: int = 1) -> NativeEngine:
    engine = NativeEngine(load_scenario(SCENARIO))
    engine.reset(seed)
    return engine


def test_fire_contract_is_frozen_typed_and_has_no_free_form_fields() -> None:
    intent = FireIntent(
        FireMode.DIRECT,
        shooter_id=1,
        weapon_id=36,
        target=UnitTarget(2),
    )

    assert intent == FireIntent(
        FireMode.DIRECT, 1, 36, UnitTarget(2), None, None)
    with pytest.raises(FrozenInstanceError):
        intent.weapon_id = 43  # type: ignore[misc]
    with pytest.raises(TypeError, match="mode must be FireMode"):
        FireIntent("DIRECT", 1, 36, UnitTarget(2))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="target must be"):
        FireIntent(FireMode.DIRECT, 1, 36, {"unit_id": 2})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exact int"):
        UnitTarget(True)  # type: ignore[arg-type]


def test_legacy_shoot_converts_to_direct_intent_without_mutating_action() -> None:
    action = _shoot()
    record_before = action.to_dict()
    action_id_before = action.action_id

    intent = ActionRouter().route_fire(action)

    assert intent == FireIntent(
        FireMode.DIRECT,
        shooter_id=1,
        weapon_id=36,
        target=UnitTarget(2),
    )
    assert action.to_dict() == record_before == {
        "side": "red",
        "actor_id": 1,
        "action_type": "SHOOT",
        "target_id": 2,
        "target_position": None,
        "parameters": {"weapon_id": 36},
    }
    assert action.action_id == action_id_before


def test_router_requires_native_stager_for_guided_and_routes_indirect_contract() -> None:
    router = ActionRouter()

    with pytest.raises(ValueError, match="GUIDED_FIRE_STAGER_REQUIRED"):
        router.route_fire_intent(FireIntent(
            FireMode.GUIDED,
            shooter_id=1,
            weapon_id=36,
            target=UnitTarget(2),
            guider_id=3,
        ))
    indirect = FireIntent(
        FireMode.INDIRECT,
        shooter_id=1,
        weapon_id=72,
        target=HexTarget(205),
        observer_id=3,
    )
    assert router.route_fire_intent(indirect) is indirect


def test_explicit_direct_contract_matches_legacy_shoot_world_events_and_rng(
    monkeypatch,
) -> None:
    legacy_engine = _engine(seed=17)
    explicit_engine = _engine(seed=17)
    action = _shoot()
    explicit_intent = FireIntent(
        FireMode.DIRECT,
        shooter_id=1,
        weapon_id=36,
        target=UnitTarget(2),
    )
    router = explicit_engine._action_router
    assert router is not None
    monkeypatch.setattr(router, "fire_intent", lambda _action: explicit_intent)

    legacy_result = legacy_engine.step([action], delta_time_seconds=1)
    explicit_result = explicit_engine.step([action], delta_time_seconds=1)
    legacy_snapshot = legacy_engine.snapshot()
    explicit_snapshot = explicit_engine.snapshot()

    assert legacy_result.decisions[0].accepted is True
    assert legacy_result == explicit_result
    assert canonical_hash(legacy_snapshot["world"]) == canonical_hash(
        explicit_snapshot["world"])
    assert legacy_snapshot["rng"] == explicit_snapshot["rng"]
    assert legacy_snapshot == explicit_snapshot
    assert any(
        event.data.get("weapon_id") == 36
        for event in legacy_result.events
    )


def test_legacy_shoot_replay_shape_and_strict_replay_are_unchanged(
    tmp_path: Path,
) -> None:
    result = run_joint_fire_example(tmp_path / "fire-contract-replay", seed=1)

    assert result["strict_replay_matches"] is True
    with ZipFile(result["replay"]) as archive:
        steps = [
            json.loads(line)
            for line in archive.read("steps.jsonl").decode("utf-8").splitlines()
        ]
    shoot_records = [
        action
        for step in steps
        for action in step["ordered_actions"]
        if action["action_type"] == "SHOOT"
    ]

    assert len(shoot_records) == 2
    assert {action["parameters"]["weapon_id"] for action in shoot_records} == {36}
    assert all(set(action) == {
        "side", "actor_id", "action_type", "target_id",
        "target_position", "parameters",
    } for action in shoot_records)
    assert all("mode" not in action["parameters"] for action in shoot_records)
