from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from examples.run_joint_fire import run_example as run_joint_fire_example
from tests.test_guided_capability import (
    HISTORICAL_REPLAY_STEPS_HASH,
    POLICY_V1_1_REPLAY_CONTENT_HASH,
    _engine,
)
from wargame_native_engine.adjudication import GuidedFireRequest
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.action_router import ActionRouter
from wargame_native_engine.core.fire_intent import (
    FireIntent,
    FireMode,
    UnitTarget,
)


def _capability_and_request():
    engine = _engine()
    capability = engine.get_guided_fire_capabilities("red")[0]
    return engine, capability, engine.build_guided_fire_request(capability)


def test_capability_builds_complete_frozen_preview_request() -> None:
    _, capability, request = _capability_and_request()

    assert tuple(item.name for item in fields(GuidedFireRequest)) == (
        "tick",
        "guider_id",
        "shooter_id",
        "weapon_id",
        "target_id",
        "guidance_profile_id",
        "guider_visibility_evidence",
        "shooter_distance",
        "ammunition_before",
        "cooldown_before",
    )
    assert request.tick == 0
    assert request.guider_id == capability.guider_id == 20
    assert request.shooter_id == capability.shooter_id == 10
    assert request.weapon_id == capability.weapon_id == 73
    assert request.target_id == capability.target_id == 30
    assert request.guidance_profile_id == "GROUND_AFFILIATED_GUIDER"
    assert request.guider_visibility_evidence.observable is True
    assert request.guider_visibility_evidence.reason == "observable"
    assert request.shooter_distance == 14
    assert request.ammunition_before == 10
    assert request.cooldown_before == 0
    assert request.status == "PREVIEW_ONLY"
    assert request.executed is False
    with pytest.raises(FrozenInstanceError):
        request.target_id = 99  # type: ignore[misc]


def test_request_cannot_be_constructed_outside_validated_capability_path() -> None:
    with pytest.raises(
        TypeError,
        match="guided_fire_request_requires_validated_capability",
    ):
        GuidedFireRequest()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("replacement", "error"),
    [
        ({"guidance_profile_id": "UNKNOWN_PROFILE"}, "PROFILE_UNAVAILABLE"),
        ({"weapon_id": 36}, "WEAPON_UNSUPPORTED"),
        ({"target_id": 999}, "TARGET_NOT_FOUND"),
    ],
)
def test_request_revalidates_profile_weapon_and_target(
    replacement: dict[str, object],
    error: str,
) -> None:
    engine = _engine()
    capability = engine.get_guided_fire_capabilities("red")[0]

    with pytest.raises(ValueError, match=error):
        engine.build_guided_fire_request(replace(capability, **replacement))


def test_direct_execution_path_remains_identical() -> None:
    left = _engine(shooter_hex=114, guider_hex=113, target_hexes=(115,))
    right = _engine(shooter_hex=114, guider_hex=113, target_hexes=(115,))
    action = EngineAction(
        "red",
        10,
        ActionType.SHOOT,
        target_id=30,
        parameters={"weapon_id": 73},
    )

    left_result = left.step([action], delta_time_seconds=1)
    right_result = right.step([action], delta_time_seconds=1)

    assert left_result == right_result
    assert left.snapshot() == right.snapshot()
    assert ActionRouter().route_fire(action) == FireIntent(
        FireMode.DIRECT,
        shooter_id=10,
        weapon_id=73,
        target=UnitTarget(30),
    )


def test_guided_preview_changes_no_world_rng_event_or_valid_action() -> None:
    engine = _engine()
    capability = engine.get_guided_fire_capabilities("red")[0]
    snapshot_before = engine.snapshot()
    events_before = engine._last_events
    valid_before = tuple(item.to_dict() for item in engine.valid_actions("red"))

    request = engine.build_guided_fire_request(capability)
    intent = FireIntent(
        FireMode.GUIDED,
        shooter_id=capability.shooter_id,
        guider_id=capability.guider_id,
        weapon_id=capability.weapon_id,
        target=UnitTarget(capability.target_id),
    )
    routed = ActionRouter.route_guided_fire_request(intent, request)

    assert routed is request
    assert routed.status == "PREVIEW_ONLY"
    assert routed.executed is False
    assert engine.snapshot() == snapshot_before
    assert engine._last_events == events_before
    assert tuple(item.to_dict() for item in engine.valid_actions("red")) == valid_before
    with pytest.raises(ValueError, match="GUIDED_FIRE_STAGER_REQUIRED"):
        ActionRouter.route_fire_intent(intent)


def test_guided_preview_rejects_intent_request_mismatch() -> None:
    _, capability, request = _capability_and_request()
    wrong_target = FireIntent(
        FireMode.GUIDED,
        shooter_id=capability.shooter_id,
        guider_id=capability.guider_id,
        weapon_id=capability.weapon_id,
        target=UnitTarget(capability.target_id + 1),
    )

    with pytest.raises(ValueError, match="INTENT_REQUEST_MISMATCH"):
        ActionRouter.route_guided_fire_request(wrong_target, request)


def test_policy_versioned_replay_hash_preserves_steps_and_strict_replay(
    tmp_path: Path,
) -> None:
    result = run_joint_fire_example(tmp_path / "guided-request-replay", seed=1)

    assert result["strict_replay_matches"] is True
    with ZipFile(result["replay"]) as archive:
        manifest = json.loads(
            archive.read("replay_manifest.native.jsonl").decode("utf-8")
        )
    assert manifest["deterministic_content_hash"] == (
        POLICY_V1_1_REPLAY_CONTENT_HASH
    )
    assert manifest["steps_sha256"] == HISTORICAL_REPLAY_STEPS_HASH
