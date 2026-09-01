from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from examples.run_joint_fire import run_example as run_joint_fire_example
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.fire_capability import (
    IndirectFireCapabilityStatus,
)
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.rules.indirect_fire import (
    INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION,
    IndirectFireDamagePolicy,
    default_indirect_fire_ammunition_mapping_path,
    default_indirect_fire_damage_policy_path,
    default_indirect_fire_observation_policy_path,
    load_indirect_fire_ammunition_authority,
    load_indirect_fire_damage_policy,
    load_indirect_fire_observation_authority,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = (
    REPOSITORY_ROOT / "examples/demos/direct_fire_basic/scenario.json"
)
POLICY_V1_1_DIRECT_REPLAY_CONTENT_HASH = (
    "e258afb894723ab43a60761380afe6b84fa8bebc6a1124d59535890410a74933"
)


def _engine(*, weapon_ids: list[int] | None = None) -> NativeEngine:
    payload = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    payload["scenario_id"] = "indirect-fire-authority-completion-test"
    payload["operators"][0]["weapon_ids"] = list(
        [72] if weapon_ids is None else weapon_ids
    )
    engine = NativeEngine(scenario_from_dict(payload))
    engine.reset(42)
    return engine


def test_weapon_72_has_formally_available_authority() -> None:
    engine = _engine()

    capability = engine.get_indirect_fire_capabilities("red")[0]

    assert capability.weapon_id == 72
    assert capability.status is IndirectFireCapabilityStatus.AVAILABLE
    assert capability.blockers == ()


def test_ammunition_cost_and_key_come_from_versioned_mapping() -> None:
    authority = load_indirect_fire_ammunition_authority()

    mapping = authority.mapping_for_weapon(72)

    assert mapping is not None
    assert mapping.ammunition_key == "0"
    assert mapping.ammo_cost_per_mission == 1
    assert authority.content_hash == canonical_hash(json.loads(
        default_indirect_fire_ammunition_mapping_path().read_text(
            encoding="utf-8"
        )
    ))
    with pytest.raises(FrozenInstanceError):
        mapping.ammunition_key = "100"  # type: ignore[misc]


def test_unknown_weapon_has_no_ammunition_authority_or_capability() -> None:
    authority = load_indirect_fire_ammunition_authority()

    assert authority.mapping_for_weapon(999) is None
    assert _engine(weapon_ids=[999]).get_indirect_fire_capabilities("red") == ()


def test_missing_ammunition_mapping_keeps_capability_blocked() -> None:
    engine = _engine()
    engine._indirect_fire_ammunition_authority = replace(  # noqa: SLF001
        engine._indirect_fire_ammunition_authority,  # noqa: SLF001
        mappings=(),
    )

    capability = engine.get_indirect_fire_capabilities("red")[0]

    assert capability.status is IndirectFireCapabilityStatus.BLOCKED
    assert "ammunition_consumption_per_mission" in capability.blockers


def test_observation_policy_is_explicit_and_hash_bound() -> None:
    authority = load_indirect_fire_observation_authority()
    payload = json.loads(
        default_indirect_fire_observation_policy_path().read_text(
            encoding="utf-8"
        )
    )

    assert authority.impact_observation_policy == "UNOBSERVED"
    assert authority.requires_observer is False
    assert authority.correction_level_policy == "FIXED_UNOBSERVED"
    assert authority.content_hash == canonical_hash(payload)


def test_damage_policy_is_explicit_and_hash_bound() -> None:
    policy = load_indirect_fire_damage_policy()
    payload = json.loads(
        default_indirect_fire_damage_policy_path().read_text(encoding="utf-8")
    )

    assert isinstance(policy, IndirectFireDamagePolicy)
    assert policy.unarmored_target_modifier == 0
    assert policy.unarmored_application == "EXPLICIT_VALUE"
    assert policy.numeric_correction_application == (
        "ADD_AND_CLAMP_TO_TARGET_STRENGTH"
    )
    assert policy.symbolic_correction_application == (
        "SHIFT_ORDERED_LADDER_AND_CLAMP"
    )
    assert policy.content_hash == canonical_hash(payload)


def test_authority_descriptor_is_deterministic_and_complete() -> None:
    first = _engine()
    second = _engine()

    left = first.indirect_fire_authority_descriptor(
        "INDIRECT_HEAVY_ARTILLERY_72"
    )
    right = second.indirect_fire_authority_descriptor(
        "INDIRECT_HEAVY_ARTILLERY_72"
    )

    assert left == right
    assert left is not None
    assert left.authority_version == INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION
    assert left.execution_policy_hash == (
        first._indirect_fire_execution_policy.content_hash  # noqa: SLF001
    )
    assert left.ammo_mapping_hash == (
        first._indirect_fire_ammunition_authority.content_hash  # noqa: SLF001
    )
    assert left.observation_policy_hash == (
        first._indirect_fire_observation_authority.content_hash  # noqa: SLF001
    )
    assert left.damage_policy_hash == (
        first._indirect_fire_damage_policy.content_hash  # noqa: SLF001
    )


def test_authority_queries_do_not_change_world_rng_events_or_valid_actions() -> None:
    engine = _engine()
    before = deepcopy(engine.snapshot())
    events = engine._last_events  # noqa: SLF001
    direct_actions = tuple(
        value.to_dict() for value in engine.valid_actions("red")
    )

    engine.get_indirect_fire_capabilities("red")
    engine.indirect_fire_authority_descriptor("INDIRECT_HEAVY_ARTILLERY_72")

    assert engine.snapshot() == before
    assert engine._last_events == events  # noqa: SLF001
    assert tuple(
        value.to_dict() for value in engine.valid_actions("red")
    ) == direct_actions


def test_direct_and_guided_action_types_are_not_changed() -> None:
    source = (
        REPOSITORY_ROOT / "src/wargame_native_engine/core/action_router.py"
    ).read_text(encoding="utf-8")

    assert "weapon_id == 72" not in source
    assert "ActionType.SHOOT" in source
    assert "FireMode.GUIDED" in source


def test_direct_replay_content_hash_records_policy_v1_1(tmp_path: Path) -> None:
    result = run_joint_fire_example(tmp_path / "authority-replay", seed=1)

    assert result["strict_replay_matches"] is True
    with ZipFile(result["replay"]) as archive:
        manifest = json.loads(
            archive.read("replay_manifest.native.jsonl").decode("utf-8")
        )
    assert manifest["deterministic_content_hash"] == (
        POLICY_V1_1_DIRECT_REPLAY_CONTENT_HASH
    )


@pytest.mark.parametrize(
    ("path", "expected_id"),
    (
        (
            "config/schemas/indirect_fire_ammunition_mapping.schema.json",
            "native-indirect-fire-ammunition-mapping/1.0",
        ),
        (
            "config/schemas/indirect_fire_observation_policy.schema.json",
            "native-indirect-fire-observation-policy/1.0",
        ),
        (
            "config/schemas/indirect_fire_damage_policy.schema.json",
            "native-indirect-fire-damage-policy/1.0",
        ),
    ),
)
def test_authority_schema_identity_is_versioned(
    path: str,
    expected_id: str,
) -> None:
    schema = json.loads((REPOSITORY_ROOT / path).read_text(encoding="utf-8"))

    assert schema["$id"] == expected_id
