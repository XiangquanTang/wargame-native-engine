from __future__ import annotations

import json
from pathlib import Path

import pytest

from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.demo_registry import (
    DEMO_EXPECTED_SCHEMA,
    DEMO_REGISTRY_SCHEMA,
    DemoRegistry,
    DemoRegistryError,
    load_default_demo_registry,
)


def test_default_demo_registry_loads_in_deterministic_order() -> None:
    registry = load_default_demo_registry()

    assert [value.demo_id for value in registry.descriptors] == [
        "boarding_basic",
        "combined_arms_test",
        "concealment_basic",
        "direct_fire_basic",
        "drone_basic",
        "guided_fire_basic",
        "indirect_fire_basic",
        "loitering_munition_basic",
        "loitering_munition_expiry",
        "mechanized_assault_basic",
        "objective_control_basic",
        "visibility_blocking",
    ]
    descriptor = registry.resolve("guided_fire_basic")
    assert descriptor.features == (
        "GUIDED_FIRE",
        "MULTI_OPERATOR_COORDINATION",
    )
    assert descriptor.expected == {
        "requires_guider": True,
        "requires_no_shooter_los": True,
    }
    composite = registry.resolve("mechanized_assault_basic")
    assert composite.capability_ids == (
        "boarding",
        "direct_fire",
        "objective_control",
        "visibility",
    )
    assert composite.composite is True


def test_registry_and_expected_behavior_schemas_are_explicit() -> None:
    registry = load_default_demo_registry()
    raw_registry = json.loads(
        (registry.root / "registry.json").read_text(encoding="utf-8")
    )
    expected = registry.expected_behavior("guided_fire_basic")

    assert raw_registry["schema"] == DEMO_REGISTRY_SCHEMA
    assert expected["schema"] == DEMO_EXPECTED_SCHEMA
    assert expected["preconditions"]["human_capability_available"] is True
    assert expected["outcome"]["strict_replay_matches"] is True


def test_registered_scenario_loads_and_engine_resets() -> None:
    registry = load_default_demo_registry()
    for descriptor in registry.descriptors:
        path = registry.scenario_path(descriptor)
        bundle = ScenarioBundle.from_path(path)
        engine = NativeEngine(bundle.scenario)
        try:
            world = engine.reset(42)
            assert len(world.operators) >= 2
            assert bundle.scenario.map.hexes
        finally:
            engine.close()
    guided = ScenarioBundle.from_path(
        registry.scenario_path("guided_fire_basic")
    )
    assert guided.scenario.terminal_rules.max_ticks >= 300


def test_registry_rejects_paths_outside_demo_library(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "schema": DEMO_REGISTRY_SCHEMA,
        "demos": [{
            "id": "unsafe",
            "name": "unsafe",
            "purpose": "unsafe",
            "scenario": "../scenario.json",
            "expected_behavior": "expected.json",
            "readme": "README.md",
            "features": ["GUIDED_FIRE"],
            "capability_id": "guided_fire",
            "expected": {},
        }],
    }), encoding="utf-8")

    with pytest.raises(DemoRegistryError, match="safe relative path"):
        DemoRegistry.load(registry)
