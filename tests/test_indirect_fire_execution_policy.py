from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from examples.run_joint_fire import run_example as run_joint_fire_example
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.rules.indirect_fire import (
    INDIRECT_FIRE_EXECUTION_POLICY_SCHEMA_VERSION,
    IndirectAffectedHexPolicy,
    IndirectFireAmmoConsumeTiming,
    IndirectFireAmmoRefundPolicy,
    IndirectFireCancellationReason,
    IndirectFireMissionState,
    IndirectMultiTargetOrder,
    IndirectScatterDirectionMode,
    IndirectScatterDistancePolicy,
    default_indirect_fire_execution_policy_path,
    load_indirect_fire_execution_policy,
)


POLICY_V1_1_REPLAY_CONTENT_HASH = (
    "e258afb894723ab43a60761380afe6b84fa8bebc6a1124d59535890410a74933"
)
HISTORICAL_REPLAY_STEPS_HASH = (
    "977a3510e9371c77916e923f42dc0003984aba0c77277942abd4be187f245dd4"
)


def test_ammunition_is_consumed_at_create_and_never_refunded() -> None:
    policy = load_indirect_fire_execution_policy()

    assert policy.ammo_policy.consume_timing is (
        IndirectFireAmmoConsumeTiming.MISSION_CREATE
    )
    assert policy.ammo_policy.refund_policy is (
        IndirectFireAmmoRefundPolicy.NO_REFUND
    )
    assert policy.cancellation_policy.refund_policy is (
        IndirectFireAmmoRefundPolicy.NO_REFUND
    )


def test_scatter_preserves_planned_hex_and_resolves_separate_impact_hex() -> None:
    scatter = load_indirect_fire_execution_policy().scatter_policy

    assert scatter.direction_policy is (
        IndirectScatterDirectionMode.HEX_NEIGHBOR_6WAY_RANDOM
    )
    assert scatter.direction_draw_policy == "UNIFORM_NATIVE_RNG_D6"
    assert scatter.distance_policy is IndirectScatterDistancePolicy.TWO_D6_TABLE
    assert scatter.distance_table_reference == "indirect_fire.*_dispersion"
    assert scatter.planned_hex_field == "planned_hex"
    assert scatter.impact_hex_field == "impact_hex"
    assert scatter.planned_hex_field != scatter.impact_hex_field


def test_only_flying_missions_can_be_cancelled_for_frozen_reasons() -> None:
    cancellation = load_indirect_fire_execution_policy().cancellation_policy

    assert cancellation.allowed_phases == (IndirectFireMissionState.FLYING,)
    assert cancellation.reasons == (
        IndirectFireCancellationReason.USER_CANCELLED,
        IndirectFireCancellationReason.SHOOTER_DESTROYED,
        IndirectFireCancellationReason.MISSION_INVALIDATED,
    )


def test_impact_is_one_hit_per_unit_in_operator_id_order() -> None:
    impact = load_indirect_fire_execution_policy().impact_policy

    assert impact.affected_hex_policy is IndirectAffectedHexPolicy.IMPACT_HEX_ONLY
    assert impact.one_hit_per_unit is True
    assert impact.multi_target_order is IndirectMultiTargetOrder.OPERATOR_ID_ASCENDING


def test_execution_policy_is_immutable_and_loads_deterministically() -> None:
    first = load_indirect_fire_execution_policy()
    second = load_indirect_fire_execution_policy()
    payload = json.loads(
        default_indirect_fire_execution_policy_path().read_text(encoding="utf-8")
    )

    assert first == second
    assert first.content_hash == canonical_hash(payload)
    assert payload["schema_version"] == INDIRECT_FIRE_EXECUTION_POLICY_SCHEMA_VERSION
    with pytest.raises(FrozenInstanceError):
        first.policy_version = "changed"  # type: ignore[misc]


def test_execution_policy_module_remains_rules_only() -> None:
    source = Path(
        "src/wargame_native_engine/rules/indirect_fire.py"
    ).read_text(encoding="utf-8")
    imported = "\n".join(
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    ).lower()

    assert "core.engine" not in imported
    assert "core.world" not in imported
    assert "core.action" not in imported
    assert "replay" not in imported


def test_policy_versioned_direct_replay_preserves_steps_and_strict_replay(
    tmp_path: Path,
) -> None:
    result = run_joint_fire_example(tmp_path / "indirect-c0-replay", seed=1)

    assert result["strict_replay_matches"] is True
    with ZipFile(result["replay"]) as archive:
        manifest = json.loads(
            archive.read("replay_manifest.native.jsonl").decode("utf-8")
        )
    assert manifest["deterministic_content_hash"] == (
        POLICY_V1_1_REPLAY_CONTENT_HASH
    )
    assert manifest["steps_sha256"] == HISTORICAL_REPLAY_STEPS_HASH
