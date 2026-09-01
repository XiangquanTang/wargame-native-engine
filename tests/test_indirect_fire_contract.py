from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.rules.indirect_fire import (
    ArtilleryClass,
    INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION,
    IndirectFireMissionState,
    IndirectRangePolicy,
    IndirectTargetDomain,
    ScatterDirectionPolicy,
    default_indirect_fire_profiles_path,
    load_indirect_fire_authority,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_weapon_72_profile_loads_as_executable_authority() -> None:
    authority = load_indirect_fire_authority()

    profile = authority.profile_for_weapon(72)
    assert profile is not None
    assert profile.profile_id == "INDIRECT_HEAVY_ARTILLERY_72"
    assert profile.supported_fire_mode == "INDIRECT"
    assert profile.flight_time_seconds == 150
    assert profile.active_duration_seconds == 300
    assert profile.planned_cooldown_seconds == 300
    assert authority.profile_for_weapon(True) is None
    assert authority.profile_for_weapon(999) is None


def test_light_medium_heavy_artillery_classification_is_stable() -> None:
    authority = load_indirect_fire_authority()

    assert tuple(
        (profile.weapon_id, profile.artillery_class)
        for profile in authority.profiles
    ) == (
        (72, ArtilleryClass.HEAVY),
        (88, ArtilleryClass.MEDIUM),
        (89, ArtilleryClass.LIGHT),
    )


def test_global_hex_policy_is_not_interpreted_as_numeric_range() -> None:
    authority = load_indirect_fire_authority()
    profile = authority.profile_for_weapon(72)
    assert profile is not None

    assert profile.minimum_range is None
    assert profile.maximum_range is None
    assert profile.target_domain is IndirectTargetDomain.AREA
    assert profile.range_policy is IndirectRangePolicy.SCENARIO_DEFINED_GLOBAL_HEX
    assert profile.unresolved_fields == ()
    assert authority.execution_blockers(72) == ()
    resource = authority.resource_policy(profile.resource_policy_id)
    assert resource is not None
    assert resource.ammo_cost == 1


def test_scatter_direction_is_frozen_to_six_way_native_rng() -> None:
    authority = load_indirect_fire_authority()
    scatter = authority.scatter_profile("NATIVE_INDIRECT_SCATTER_V1")
    assert scatter is not None

    assert scatter.dice_expression == "2d6"
    assert scatter.table_reference == "indirect_fire.*_dispersion"
    assert scatter.direction_policy is (
        ScatterDirectionPolicy.HEX_NEIGHBOR_6WAY_RANDOM
    )
    with pytest.raises(FrozenInstanceError):
        scatter.direction_policy = "HEX_DIRECTION_0"  # type: ignore[misc]


def test_mission_and_impact_contracts_are_complete() -> None:
    authority = load_indirect_fire_authority()
    mission = authority.mission_policy("NATIVE_INDIRECT_MISSION_V1")
    impact = authority.impact_profile("NATIVE_INDIRECT_IMPACT_V1")
    assert mission is not None
    assert impact is not None

    assert mission.states == tuple(IndirectFireMissionState)
    assert tuple(state.value for state in mission.states) == (
        "PLANNED",
        "FLYING",
        "ACTIVE",
        "RESOLVED",
        "CANCELLED",
    )
    assert mission.flight_duration_seconds == 150
    assert mission.active_duration_seconds == 300
    assert mission.can_cancel_during_flight is True
    assert impact.affected_area == "RESOLVED_HEX_ONLY"
    assert impact.friendly_fire is True
    assert impact.one_hit_per_unit is True
    assert impact.planned_hex_field == "planned_hex"
    assert impact.impact_hex_field == "impact_hex"


def test_configuration_load_and_hash_are_deterministic() -> None:
    first = load_indirect_fire_authority()
    second = load_indirect_fire_authority()
    payload = json.loads(
        default_indirect_fire_profiles_path().read_text(encoding="utf-8")
    )

    assert first == second
    assert first.content_hash == canonical_hash(payload)
    assert tuple(profile.weapon_id for profile in first.profiles) == (72, 88, 89)
    assert first.unresolved_authority == tuple(sorted(first.unresolved_authority))
    schema = json.loads(
        (REPOSITORY_ROOT / "config/schemas/indirect_fire_profiles.schema.json")
        .read_text(encoding="utf-8")
    )
    assert schema["$id"] == INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION


def test_configuration_rejects_unapproved_scatter_direction(
    tmp_path: Path,
) -> None:
    payload = json.loads(
        default_indirect_fire_profiles_path().read_text(encoding="utf-8")
    )
    payload["scatter_profiles"]["NATIVE_INDIRECT_SCATTER_V1"][
        "direction_policy"
    ] = "CLOCKWISE_D6"
    changed = tmp_path / "indirect.json"
    changed.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_indirect_fire_authority(
            changed,
            repository_root=REPOSITORY_ROOT,
        )


def test_authority_module_has_no_engine_or_world_dependency() -> None:
    source = (
        REPOSITORY_ROOT
        / "src/wargame_native_engine/rules/indirect_fire.py"
    ).read_text(encoding="utf-8")

    assert "core.engine" not in source
    assert "core.world" not in source
    assert "core.action" not in source
    assert "replay" not in "\n".join(
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    ).lower()
