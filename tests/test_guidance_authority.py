from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from examples.run_joint_fire import run_example as run_joint_fire_example
from wargame_native_engine.adjudication.request import WeaponProfile
from wargame_native_engine.compat.source_frame.models import UnitDomain
from wargame_native_engine.core.action_router import ActionRouter
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.fire_intent import FireIntent, FireMode, UnitTarget
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.io.scenario import load_scenario, scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.rules.guidance import (
    GuidanceAffiliationEvidence,
    GuidanceScope,
    load_guidance_authority,
)


SCENARIO = Path("examples/joint_fire_scenario.json")
OLD_WORLD_HASH = "15086e7d9a71c91ba9acc415f46386cd86efdd62c206c550cb0f5d28ea16b38c"
OLD_OPERATOR_HASH = "1c69458b98f0e1a10248ff73553eb6528a8e73e2eb6819464d4736a7c2402086"
POLICY_V1_1_REPLAY_CONTENT_HASH = (
    "e258afb894723ab43a60761380afe6b84fa8bebc6a1124d59535890410a74933"
)
HISTORICAL_REPLAY_STEPS_HASH = (
    "977a3510e9371c77916e923f42dc0003984aba0c77277942abd4be187f245dd4"
)
OLD_FINAL_STATE_HASH = (
    "f64334d960e7c0ab0e9d5ee780315a941a9bc2ad2071e8e2fb4816bdefcbf21d"
)


def _scenario_payload() -> dict[str, object]:
    return json.loads(SCENARIO.read_text(encoding="utf-8"))


def _engine_with_red_operator_fields(
    *,
    fields: dict[str, object] | None = None,
    raw_attributes: dict[str, object] | None = None,
) -> NativeEngine:
    payload = _scenario_payload()
    operators = payload["operators"]
    assert isinstance(operators, list)
    red = operators[0]
    assert isinstance(red, dict)
    red.update(fields or {})
    raw = red["raw_attributes"]
    assert isinstance(raw, dict)
    raw.update(raw_attributes or {})
    engine = NativeEngine(scenario_from_dict(payload))
    engine.reset(1)
    return engine


def test_old_operator_serialization_and_world_hash_are_unchanged() -> None:
    world = load_scenario(SCENARIO).initial_world()
    record = world.operators[1].to_dict()

    assert "guidance_profile_id" not in record
    assert "guidance_affiliation_id" not in record
    assert "guided_fire_recovery_remaining_ms" not in record
    assert "guidance_affiliation_evidence" not in record
    assert canonical_hash(record) == OLD_OPERATOR_HASH
    assert canonical_hash(world.to_dict()) == OLD_WORLD_HASH
    assert OperatorState.from_dict(record).to_dict() == record


def test_explicit_guidance_state_round_trips_with_immutable_evidence() -> None:
    record = load_scenario(SCENARIO).initial_world().operators[1].to_dict()
    record.update({
        "guidance_profile_id": "GROUND_AFFILIATED_GUIDER",
        "guidance_affiliation_id": 3,
        "guided_fire_recovery_remaining_ms": 12_000,
        "guidance_affiliation_evidence": {
            "source_field": "native_supplement.guidance_affiliation_id",
            "source_value": 3,
            "mapping_version": "source-guidance-authority/1.0",
        },
    })

    operator = OperatorState.from_dict(record)

    assert operator.to_dict() == record
    assert operator.guidance_affiliation_evidence is not None
    with pytest.raises(FrozenInstanceError):
        operator.guidance_affiliation_evidence.mapping_version = "changed"  # type: ignore[misc]


def test_exact_source_type_subtype_profiles_and_unknown_policy() -> None:
    authority = load_guidance_authority()

    assert authority.profile_for_source_codes(
        2, 4
    ).profile_id == "GROUND_AFFILIATED_GUIDER"
    assert authority.profile_for_source_codes(
        1, 2
    ).profile_id == "PERSONNEL_AFFILIATED_GUIDER"
    air = authority.profile_for_source_codes(3, 5)
    assert air is not None
    assert air.profile_id == "AIR_SIDE_GUIDER"
    assert air.guidance_scope is GuidanceScope.SIDE_WIDE
    assert authority.profile_for_source_codes(999, 999) is None
    assert authority.profile_for_source_codes(True, 4) is None


def test_weapon_73_supports_guided_and_ordinary_weapon_defaults_direct() -> None:
    authority = load_guidance_authority()
    assert authority.supported_fire_modes(73) == frozenset({
        FireMode.DIRECT,
        FireMode.GUIDED,
    })
    assert authority.supported_fire_modes(36) == frozenset({FireMode.DIRECT})
    ordinary = WeaponProfile(
        weapon_id=36,
        weapon_class="large_direct_fire_gun",
        maximum_range=10,
        target_domains=frozenset({UnitDomain.VEHICLE}),
    )
    assert ordinary.supported_fire_modes == frozenset({FireMode.DIRECT})

    payload = _scenario_payload()
    metadata = payload["metadata"]
    assert isinstance(metadata, dict)
    source = metadata["source_compatible"]
    assert isinstance(source, dict)
    weapons = source["weapons"]
    assert isinstance(weapons, dict)
    weapons["73"] = {
        "weapon_class": "heavy_missile",
        "maximum_range": 20,
        "target_domains": ["VEHICLE"],
        "ammunition_key": "0",
        "requires_unfolded": True,
        "can_fire_while_moving": False,
    }
    engine = NativeEngine(scenario_from_dict(payload))
    profile = engine._source_weapon_profile(73)
    assert profile.supported_fire_modes == frozenset({
        FireMode.DIRECT,
        FireMode.GUIDED,
    })


def test_profile_without_affiliation_does_not_generate_guidance() -> None:
    engine = _engine_with_red_operator_fields(fields={
        "guidance_profile_id": "GROUND_AFFILIATED_GUIDER",
    })

    profile = engine.guidance_profile_for_operator(1)
    assert profile is not None
    assert profile.profile_id == "GROUND_AFFILIATED_GUIDER"
    assert engine.guidance_affiliation_for_operator(1) is None
    assert engine.get_guided_fire_capabilities("red") == ()


@pytest.mark.parametrize(
    "raw_attributes",
    [
        {"guide_ability": 1},
        {"car_id": 2, "car": 2, "source_frame": {"car": 2}},
    ],
    ids=["raw-guide-ability", "raw-car-link"],
)
def test_source_compatibility_raw_fields_do_not_authorize_guidance(
    raw_attributes: dict[str, object],
) -> None:
    engine = _engine_with_red_operator_fields(raw_attributes=raw_attributes)

    assert engine.guidance_profile_for_operator(1) is None
    assert engine.guidance_affiliation_for_operator(1) is None
    assert engine.get_guided_fire_capabilities("red") == ()


def test_unknown_source_subtype_cannot_produce_guided_capability() -> None:
    engine = _engine_with_red_operator_fields(raw_attributes={
        "source_frame": {"type": 999, "sub_type": 999},
    })

    assert engine.guidance_profile_for_operator(1) is None
    assert engine.get_guided_fire_capabilities("red") == ()


def test_guided_fire_contract_requires_native_staging_authority() -> None:
    with pytest.raises(ValueError, match="GUIDED_FIRE_STAGER_REQUIRED"):
        ActionRouter.route_fire_intent(FireIntent(
            mode=FireMode.GUIDED,
            shooter_id=1,
            guider_id=3,
            weapon_id=73,
            target=UnitTarget(2),
        ))


def test_policy_versioned_replay_preserves_world_steps_and_strict_replay(
    tmp_path: Path,
) -> None:
    result = run_joint_fire_example(tmp_path / "guidance-old-replay", seed=1)
    assert result["strict_replay_matches"] is True
    assert result["final_state_hash"] == OLD_FINAL_STATE_HASH

    with ZipFile(result["replay"]) as archive:
        manifest = json.loads(
            archive.read("replay_manifest.native.jsonl").decode("utf-8")
        )
    assert manifest["deterministic_content_hash"] == (
        POLICY_V1_1_REPLAY_CONTENT_HASH
    )
    assert manifest["steps_sha256"] == HISTORICAL_REPLAY_STEPS_HASH
    assert manifest["final_state_hash"] == OLD_FINAL_STATE_HASH
