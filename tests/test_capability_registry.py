from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_capabilities import main, render_matrix
from wargame_native_engine.capability_registry import (
    CapabilityRegistry,
    CapabilityRegistryError,
    load_default_capability_registry,
)
from wargame_native_engine.demo_registry import load_default_demo_registry


EXPECTED_MATRIX = {
    "boarding": ("IMPLEMENTED", "PASS", True, True, True),
    "concealment": ("IMPLEMENTED", "PASS", True, True, True),
    "direct_fire": ("IMPLEMENTED", "PASS", True, True, True),
    "drone": ("IMPLEMENTED", "PASS", True, True, True),
    "guided_fire": ("IMPLEMENTED", "PASS", True, True, True),
    "indirect_fire": ("IMPLEMENTED", "PASS", True, True, True),
    "loitering_munition": ("IMPLEMENTED", "PASS", True, True, True),
    "objective_control": (
        "SIMPLIFIED_IMPLEMENTATION",
        "PASS",
        True,
        True,
        True,
    ),
    "visibility": ("IMPLEMENTED", "PASS", True, True, True),
}


def test_default_registry_matches_initial_capability_coverage() -> None:
    registry = load_default_capability_registry()

    assert tuple(item.capability_id for item in registry.descriptors) == tuple(
        EXPECTED_MATRIX
    )
    for capability_id, expected in EXPECTED_MATRIX.items():
        item = registry.resolve(capability_id)
        assert (
            item.engine_status,
            item.demo_status,
            item.replay_support,
            item.human_support,
            item.agent_support,
        ) == expected


def test_registry_demo_and_evidence_links_are_valid() -> None:
    registry = load_default_capability_registry()

    registry.validate_references()
    registry.validate_demo_traceability(load_default_demo_registry())


def test_capability_matrix_is_deterministic() -> None:
    registry = load_default_capability_registry()

    first = render_matrix(registry.descriptors)
    second = render_matrix(tuple(reversed(registry.descriptors)))

    assert first == second
    assert first.index("direct_fire") < first.index("guided_fire")
    assert first.index("guided_fire") < first.index("indirect_fire")
    assert "SIMPLIFIED_IMPLEMENTATION" in first


def test_cli_list_and_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list"]) == 0
    listed = capsys.readouterr().out
    assert "Capability Matrix" in listed
    assert "indirect_fire" in listed
    assert "IMPLEMENTED" in listed

    assert main(["check"]) == 0
    checked = capsys.readouterr().out
    assert "Capability traceability: PASS (9 capabilities)" in checked


def test_registry_rejects_demo_feature_mismatch(tmp_path: Path) -> None:
    default = Path("config/capabilities/native_capability_registry_v1.json")
    payload = json.loads(default.read_text(encoding="utf-8"))
    payload["capabilities"][0]["demo_feature"] = "NOT_A_DEMO_FEATURE"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    registry = CapabilityRegistry.load(registry_path, repository_root=Path.cwd())

    with pytest.raises(CapabilityRegistryError, match="absent from demo"):
        registry.validate_demo_traceability(load_default_demo_registry())


def test_registry_rejects_unsafe_reference(tmp_path: Path) -> None:
    default = Path("config/capabilities/native_capability_registry_v1.json")
    payload = json.loads(default.read_text(encoding="utf-8"))
    payload["capabilities"][0]["rule_refs"] = ["../private-rules.json"]
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CapabilityRegistryError, match="unsafe reference"):
        CapabilityRegistry.load(registry_path, repository_root=Path.cwd())


def test_replay_support_requires_demo_strict_replay_contract(
    tmp_path: Path,
) -> None:
    demo_root = tmp_path / "demos"
    demo_root.mkdir()
    default_demo_root = Path("examples/demos")
    registry_payload = json.loads(
        (default_demo_root / "registry.json").read_text(encoding="utf-8")
    )
    first = next(
        value for value in registry_payload["demos"]
        if value.get("id") == "direct_fire_basic"
    )
    first["scenario"] = "scenario.json"
    first["readme"] = "README.md"
    first["expected_behavior"] = "expected.json"
    registry_payload["demos"] = [first]
    (demo_root / "registry.json").write_text(
        json.dumps(registry_payload), encoding="utf-8"
    )
    (demo_root / "scenario.json").write_text("{}", encoding="utf-8")
    (demo_root / "README.md").write_text("demo", encoding="utf-8")
    (demo_root / "expected.json").write_text(
        json.dumps(
            {
                "schema": "native-demo-expected/1.0",
                "demo_id": "direct_fire_basic",
                "outcome": {"strict_replay_matches": False},
            }
        ),
        encoding="utf-8",
    )
    from wargame_native_engine.demo_registry import DemoRegistry

    demos = DemoRegistry.load(demo_root / "registry.json")
    capability_payload = json.loads(
        Path("config/capabilities/native_capability_registry_v1.json").read_text(
            encoding="utf-8"
        )
    )
    capability_payload["capabilities"] = [
        value for value in capability_payload["capabilities"]
        if value["capability_id"] == "direct_fire"
    ]
    capability_path = tmp_path / "capabilities.json"
    capability_path.write_text(json.dumps(capability_payload), encoding="utf-8")
    capabilities = CapabilityRegistry.load(
        capability_path,
        repository_root=Path.cwd(),
    )

    with pytest.raises(CapabilityRegistryError, match="strict replay expectation"):
        capabilities.validate_demo_traceability(demos)
