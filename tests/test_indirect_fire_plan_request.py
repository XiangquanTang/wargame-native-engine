from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, replace
import json
from pathlib import Path

import pytest

from wargame_native_engine.adjudication.indirect_fire import (
    IndirectFirePlanRequest,
)
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.fire_capability import (
    IndirectFireCapabilityStatus,
)
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.rules.indirect_fire import (
    INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION,
    IndirectRangePolicy,
    load_indirect_fire_execution_policy,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = (
    REPOSITORY_ROOT / "examples/demos/direct_fire_basic/scenario.json"
)
EXECUTION_POLICY_HASH = load_indirect_fire_execution_policy().content_hash


def _engine(*, missing_ammo_mapping: bool = False) -> NativeEngine:
    payload = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    payload["scenario_id"] = "indirect-fire-plan-request-test"
    payload["operators"][0]["weapon_ids"] = [72]
    engine = NativeEngine(scenario_from_dict(payload))
    if missing_ammo_mapping:
        engine._indirect_fire_ammunition_authority = replace(  # noqa: SLF001
            engine._indirect_fire_ammunition_authority,  # noqa: SLF001
            mappings=(),
        )
    engine.reset(42)
    return engine


def _available_capability(engine: NativeEngine):
    capability = engine.get_indirect_fire_capabilities("red")[0]
    assert capability.status is IndirectFireCapabilityStatus.AVAILABLE
    return capability


def test_available_capability_and_map_hex_build_plan_request() -> None:
    engine = _engine()
    capability = _available_capability(engine)

    result = engine.build_indirect_fire_plan_request(capability, 102)

    assert result.valid is True
    assert result.reason == "PLAN_REQUEST_VALID"
    assert result.blockers == ()
    request = result.request
    assert request is not None
    assert request.tick == 0
    assert request.action_id.startswith("indirect-plan-")
    assert request.shooter_id == 1
    assert request.weapon_id == 72
    assert request.planned_hex == 102
    assert request.artillery_profile_id == "INDIRECT_HEAVY_ARTILLERY_72"
    assert request.execution_policy_id == "native-indirect-fire-execution-policy"
    assert request.execution_policy_hash == EXECUTION_POLICY_HASH
    assert request.ammo_before == (("0", 20),)
    assert request.authority_version == INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION
    assert request.validation_evidence.checked_profile_id == request.artillery_profile_id
    assert request.validation_evidence.checked_policy_hash == EXECUTION_POLICY_HASH
    assert request.validation_evidence.planned_hex_validated is True
    assert request.validation_evidence.range_policy is (
        IndirectRangePolicy.SCENARIO_DEFINED_GLOBAL_HEX
    )


def test_map_external_planned_hex_is_rejected() -> None:
    engine = _engine()

    result = engine.build_indirect_fire_plan_request(
        _available_capability(engine),
        999,
    )

    assert result.valid is False
    assert result.reason == "TARGET_HEX_NOT_FOUND"
    assert result.blockers == ("target_hex_not_found",)
    assert result.request is None


def test_blocked_capability_cannot_build_request() -> None:
    engine = _engine(missing_ammo_mapping=True)
    capability = engine.get_indirect_fire_capabilities("red")[0]
    assert capability.status is IndirectFireCapabilityStatus.BLOCKED

    result = engine.build_indirect_fire_plan_request(capability, 102)

    assert result.valid is False
    assert result.reason == "CAPABILITY_BLOCKED"
    assert result.blockers == ("ammunition_consumption_per_mission",)
    assert result.request is None


def test_unavailable_execution_policy_fails_closed() -> None:
    engine = _engine()
    capability = _available_capability(engine)
    engine._indirect_fire_execution_policy = None  # type: ignore[assignment]  # noqa: SLF001

    result = engine.build_indirect_fire_plan_request(capability, 102)

    assert result.valid is False
    assert result.reason == "POLICY_UNAVAILABLE"
    assert result.blockers == ("execution_policy_unavailable",)
    assert result.request is None


def test_unknown_execution_policy_identity_fails_closed() -> None:
    engine = _engine()
    capability = _available_capability(engine)
    policy = engine._indirect_fire_execution_policy  # noqa: SLF001
    engine._indirect_fire_execution_policy = replace(  # noqa: SLF001
        policy,
        policy_id="unknown-indirect-policy",
    )

    result = engine.build_indirect_fire_plan_request(capability, 102)

    assert result.valid is False
    assert result.reason == "POLICY_UNAVAILABLE"
    assert result.blockers == ("execution_policy_unavailable",)
    assert result.request is None


def test_request_construction_is_read_only_for_all_authoritative_evidence() -> None:
    engine = _engine()
    capability = _available_capability(engine)
    before_snapshot = deepcopy(engine.snapshot())
    before_events = tuple(engine._last_events)  # noqa: SLF001
    before_valid_actions = tuple(
        item.to_dict() for item in engine.valid_actions("red")
    )

    result = engine.build_indirect_fire_plan_request(capability, 102)

    assert result.valid is True
    assert engine.snapshot() == before_snapshot
    assert engine.snapshot()["rng"] == before_snapshot["rng"]
    assert engine._last_events == before_events  # noqa: SLF001
    assert tuple(
        item.to_dict() for item in engine.valid_actions("red")
    ) == before_valid_actions


def test_request_contract_contains_no_execution_or_impact_fields() -> None:
    engine = _engine()
    result = engine.build_indirect_fire_plan_request(
        _available_capability(engine),
        102,
    )
    request = result.request
    assert request is not None

    names = {item.name for item in fields(request)}
    assert "impact_hex" not in names
    assert "scatter_result" not in names
    assert "damage" not in names
    record = request.to_record()
    assert "impact_hex" not in record
    assert "scatter_result" not in record
    assert "damage" not in record
    assert record["status"] == "PLAN_ONLY"
    assert record["executable"] is False


def test_plan_request_id_and_policy_hash_are_stable() -> None:
    first = _engine()
    second = _engine()

    first_result = first.build_indirect_fire_plan_request(
        _available_capability(first),
        102,
    )
    second_result = second.build_indirect_fire_plan_request(
        _available_capability(second),
        102,
    )

    assert first_result.request == second_result.request
    assert first_result.request is not None
    assert first_result.request.execution_policy_hash == EXECUTION_POLICY_HASH


def test_external_callers_cannot_construct_plan_request() -> None:
    with pytest.raises(
        TypeError,
        match="indirect_fire_plan_request_requires_engine_validation",
    ):
        IndirectFirePlanRequest()
