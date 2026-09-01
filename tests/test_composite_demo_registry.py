from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from wargame_native_engine.demo_registry import (
    DEMO_EXPECTED_SCHEMA,
    DEMO_REGISTRY_SCHEMA,
    DemoRegistry,
    DemoRegistryError,
    load_default_demo_registry,
)


CAPABILITIES = (
    "boarding",
    "direct_fire",
    "objective_control",
    "visibility",
)


def test_composite_descriptor_has_deterministic_capabilities_and_phases() -> None:
    descriptor = load_default_demo_registry().resolve(
        "mechanized_assault_basic"
    )

    assert descriptor.capability_ids == CAPABILITIES
    assert descriptor.composite is True
    assert tuple(phase.name for phase in descriptor.phases) == (
        "boarding_started",
        "boarding_completed",
        "advance_to_objective",
        "unload_started",
        "unload_completed",
        "objective_controlled",
        "contact_confirmed",
        "direct_fire_resolved",
    )
    assert all(
        phase.required_capability in descriptor.capability_ids
        for phase in descriptor.phases
    )
    with pytest.raises(FrozenInstanceError):
        descriptor.phases[0].name = "changed"  # type: ignore[misc]


def test_legacy_singular_capability_id_remains_readable(tmp_path: Path) -> None:
    registry_path = _registry(
        tmp_path,
        {
            "id": "legacy",
            "capability_id": "direct_fire",
            "features": ["DIRECT_FIRE"],
        },
    )

    descriptor = DemoRegistry.load(
        registry_path,
        known_capability_ids=("direct_fire",),
    ).resolve("legacy")

    assert descriptor.capability_ids == ("direct_fire",)
    assert descriptor.composite is False


@pytest.mark.parametrize(
    ("capability_ids", "message"),
    [
        (["boarding", "boarding"], "must not contain duplicates"),
        (["boarding", "unknown"], "unknown capabilities"),
        (["visibility", "boarding"], "deterministically sorted"),
    ],
)
def test_composite_registry_rejects_invalid_capability_sets(
    tmp_path: Path,
    capability_ids: list[str],
    message: str,
) -> None:
    registry_path = _registry(
        tmp_path,
        {
            "id": "invalid",
            "capability_ids": capability_ids,
            "features": ["COMPOSITE_SCENARIO"],
            "phases": [{
                "name": "phase",
                "required_capability": capability_ids[0],
                "validator": "BOARD",
            }],
        },
    )

    with pytest.raises(DemoRegistryError, match=message):
        DemoRegistry.load(
            registry_path,
            known_capability_ids=CAPABILITIES,
        )


def _registry(
    root: Path, overrides: dict[str, object],
) -> Path:
    demo_id = str(overrides["id"])
    (root / "scenario.json").write_text("{}", encoding="utf-8")
    (root / "README.md").write_text("demo", encoding="utf-8")
    (root / "expected.json").write_text(json.dumps({
        "schema": DEMO_EXPECTED_SCHEMA,
        "demo_id": demo_id,
    }), encoding="utf-8")
    descriptor = {
        "id": demo_id,
        "name": demo_id,
        "purpose": demo_id,
        "scenario": "scenario.json",
        "expected_behavior": "expected.json",
        "readme": "README.md",
        "features": ["DIRECT_FIRE"],
        "expected": {},
        **overrides,
    }
    path = root / "registry.json"
    path.write_text(json.dumps({
        "schema": DEMO_REGISTRY_SCHEMA,
        "demos": [descriptor],
    }), encoding="utf-8")
    return path
