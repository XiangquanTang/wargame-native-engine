from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from tests.test_realtime_dual_side_control import (
    Fixture,
    decoded,
    request,
    running_server,
)
from wargame_native_engine.data import DataProvenance, ScenarioBundle
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.fire_capability import (
    IndirectFireCapability,
    IndirectFireCapabilityStatus,
    IndirectFireTargetHexStatus,
)
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.rules.indirect_fire import (
    ArtilleryClass,
    IndirectRangePolicy,
    IndirectTargetDomain,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = (
    REPOSITORY_ROOT / "examples/demos/direct_fire_basic/scenario.json"
)
def _engine(
    *,
    red_weapons: list[int] | None = None,
    blue_weapons: list[int] | None = None,
) -> NativeEngine:
    payload = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    payload["scenario_id"] = "indirect-fire-readonly-capability-test"
    payload["operators"][0]["weapon_ids"] = list(
        [72] if red_weapons is None else red_weapons
    )
    payload["operators"][1]["weapon_ids"] = list(blue_weapons or [])
    engine = NativeEngine(scenario_from_dict(payload))
    engine.reset(42)
    return engine


def _bundle_with_red_weapon_72() -> ScenarioBundle:
    payload = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    payload["scenario_id"] = "indirect-fire-readonly-human-agent-test"
    payload["operators"][0]["weapon_ids"] = [72]
    return ScenarioBundle.from_record(
        payload,
        provenance=DataProvenance(
            source_sha256=canonical_hash(payload),
            source_kind="project_authored_indirect_fire_capability_test",
        ),
    )


def test_weapon_72_returns_frozen_available_capability() -> None:
    engine = _engine()

    capabilities = engine.get_indirect_fire_capabilities("red")

    assert capabilities == (IndirectFireCapability(
        shooter_id=1,
        weapon_id=72,
        artillery_class=ArtilleryClass.HEAVY,
        profile_id="INDIRECT_HEAVY_ARTILLERY_72",
        range_policy=IndirectRangePolicy.SCENARIO_DEFINED_GLOBAL_HEX,
        minimum_range=None,
        maximum_range=None,
        target_policy=IndirectTargetDomain.AREA,
        status=IndirectFireCapabilityStatus.AVAILABLE,
        blockers=(),
    ),)
    with pytest.raises(FrozenInstanceError):
        capabilities[0].status = IndirectFireCapabilityStatus.BLOCKED  # type: ignore[misc]


def test_global_hex_policy_preserves_null_numeric_ranges_without_blockers() -> None:
    capability = _engine().get_indirect_fire_capabilities("red")[0]

    assert capability.minimum_range is None
    assert capability.maximum_range is None
    assert capability.range_policy is IndirectRangePolicy.SCENARIO_DEFINED_GLOBAL_HEX
    assert capability.target_policy is IndirectTargetDomain.AREA
    assert "artillery_minimum_range" not in capability.blockers
    assert "artillery_maximum_range" not in capability.blockers
    assert "indirect_target_domain" not in capability.blockers
    assert "ammunition_consumption_per_mission" not in capability.blockers
    assert "scatter_direction" not in capability.blockers
    assert capability.status is IndirectFireCapabilityStatus.AVAILABLE


def test_non_authority_weapon_does_not_generate_capability() -> None:
    engine = _engine(red_weapons=[36])

    assert engine.get_indirect_fire_capabilities("red") == ()


@pytest.mark.parametrize("field", ["alive", "deployed"])
def test_non_operational_shooter_does_not_generate_capability(field: str) -> None:
    engine = _engine()
    world = engine._world  # noqa: SLF001
    assert world is not None
    setattr(world.operators[1], field, False)

    assert engine.get_indirect_fire_capabilities("red") == ()


def test_current_weapon_cooldown_is_reported_without_hiding_authority() -> None:
    engine = _engine()
    world = engine._world  # noqa: SLF001
    assert world is not None
    world.operators[1].weapon_cooldown_remaining_ms = 1

    capability = engine.get_indirect_fire_capabilities("red")[0]

    assert capability.status is IndirectFireCapabilityStatus.BLOCKED
    assert capability.blockers == ("weapon_cooldown_remaining",)


def test_query_is_strictly_side_bound() -> None:
    engine = _engine(red_weapons=[72], blue_weapons=[72])

    assert tuple(
        item.shooter_id for item in engine.get_indirect_fire_capabilities("red")
    ) == (1,)
    assert tuple(
        item.shooter_id for item in engine.get_indirect_fire_capabilities("blue")
    ) == (2,)


def test_capability_sorting_is_deterministic_and_deduplicates_weapon_ids() -> None:
    first = _engine(red_weapons=[89, 72, 72, 88])
    second = _engine(red_weapons=[88, 72, 89])

    first_values = first.get_indirect_fire_capabilities("red")
    second_values = second.get_indirect_fire_capabilities("red")

    assert tuple(value.weapon_id for value in first_values) == (72, 88, 89)
    assert first_values == second_values


def test_hex_preview_accepts_any_existing_scenario_hex_without_numeric_range() -> None:
    engine = _engine()
    capability = engine.get_indirect_fire_capabilities("red")[0]

    result = engine.validate_indirect_fire_target_hex(capability, 102)

    assert result.status is IndirectFireTargetHexStatus.VALID
    assert result.reason == "TARGET_HEX_VALID_SCENARIO_GLOBAL"
    assert result.target_hex == 102
    assert result.distance is None
    assert not hasattr(capability, "target_hex")
    assert all(
        action.action.action_type.value not in {"INDIRECT_FIRE", "FIRE_MISSION"}
        for action in engine.valid_actions("red")
    )


def test_hex_preview_rejects_missing_hex_before_authority_interpretation() -> None:
    engine = _engine()
    capability = engine.get_indirect_fire_capabilities("red")[0]

    result = engine.validate_indirect_fire_target_hex(capability, 999)

    assert result.status is IndirectFireTargetHexStatus.INVALID
    assert result.reason == "TARGET_HEX_NOT_FOUND"


def test_queries_do_not_modify_world_rng_or_events() -> None:
    engine = _engine()
    before_snapshot = deepcopy(engine.snapshot())
    before_events = tuple(engine._last_events)  # noqa: SLF001

    capability = engine.get_indirect_fire_capabilities("red")[0]
    preview = engine.validate_indirect_fire_target_hex(capability, 102)

    assert preview.status is IndirectFireTargetHexStatus.VALID
    assert engine.snapshot() == before_snapshot
    assert engine.snapshot()["rng"] == before_snapshot["rng"]
    assert engine._last_events == before_events  # noqa: SLF001


def test_human_and_agent_receive_available_indirect_fire_capability(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path, bundle=_bundle_with_red_weapon_72())
    try:
        agent_summary = fixture.reset.red.capabilities.operation_summary
        assert agent_summary["indirect_fire"]["capabilities"][0]["status"] == (
            "AVAILABLE"
        )
        assert "INDIRECT_FIRE" in fixture.reset.red.capabilities.operation_types

        with running_server(fixture.server) as address:
            status, payload, _ = request(
                address,
                "POST",
                "/api/product/capabilities",
                {"token": "red-token-123", "actor_ids": [1]},
            )
            assert status == 200, payload
            model = decoded(payload)
            assert model["indirect_fire"]["options"][0]["shooter_id"] == 1
            assert model["indirect_fire"]["options"][0]["weapon_id"] == 72
            assert any(
                item["capability_type"] == "INDIRECT_FIRE"
                for item in model["capabilities"]
            )
    finally:
        fixture.close()


def test_human_indirect_fire_preview_builds_explicit_fire_action(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path, bundle=_bundle_with_red_weapon_72())
    try:
        with running_server(fixture.server) as address:
            bootstrap = decoded(request(
                address,
                "GET",
                "/api/product/bootstrap?token=red-token-123",
            )[1])
            status, payload, _ = request(
                address,
                "POST",
                "/api/product/preview",
                {
                    "token": "red-token-123",
                    "request_id": "indirect-ui-preview",
                    "observation_version": bootstrap["observation_version"],
                    "actor_ids": [1],
                    "operation_type": "INDIRECT_FIRE",
                    "fire_mode": "INDIRECT",
                    "shooter_id": 1,
                    "weapon_id": 72,
                    "target_hex": 102,
                },
            )
            assert status == 200, payload
            preview = decoded(payload)
            assert preview["can_execute"] is True, preview
            assert "计划格102" in (
                preview["per_unit_results"][0]["human_readable_summary"]
            )

            status, payload, _ = request(
                address,
                "POST",
                "/api/product/execute",
                {
                    "token": "red-token-123",
                    "preview_token": preview["preview_token"],
                    "request_id": "indirect-ui-execute",
                },
            )
            assert status == 202, payload
            operation = fixture.session.command_events[0].decision.operations[0]
            assert operation.preview_arguments() == {
                "actor_ids": (1,),
                "operation_type": "INDIRECT_FIRE",
                "destination_hex": None,
                "target_id": None,
                "movement_mode": "NORMAL",
                "weapon_choices": {},
                "whole_side_wait": False,
                "target_hex": 102,
                "fire_mode": "INDIRECT",
                "shooter_id": 1,
                "weapon_id": 72,
            }
            tick = fixture.session.advance_tick()
            assert tick.red_command_sequences == (0,)
            applied = fixture.session.command_events[0]
            assert applied.status.value == "APPLIED"
    finally:
        fixture.close()


@pytest.mark.parametrize("side", ["RED", "unknown", ""])
def test_query_rejects_non_authoritative_side_names(side: str) -> None:
    engine = _engine()

    with pytest.raises(ValueError):
        engine.get_indirect_fire_capabilities(side)
