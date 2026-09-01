from __future__ import annotations

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
from wargame_native_engine.adjudication import (
    EffectBundle,
    GuidedFireStageResult,
)
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.action_router import ActionRouter
from wargame_native_engine.core.fire_capability import GuidedFireCapability
from wargame_native_engine.core.fire_intent import (
    FireIntent,
    FireMode,
    UnitTarget,
)


def _guided_intent(capability: GuidedFireCapability) -> FireIntent:
    return FireIntent(
        mode=FireMode.GUIDED,
        shooter_id=capability.shooter_id,
        guider_id=capability.guider_id,
        weapon_id=capability.weapon_id,
        target=UnitTarget(capability.target_id),
    )


def test_guided_intent_enters_router_through_native_staging_callback() -> None:
    engine = _engine(target_concealed=True)
    intent = _guided_intent(engine.get_guided_fire_capabilities("red")[0])

    routed = ActionRouter.route_fire_intent(
        intent,
        guided_stage=engine.stage_guided_fire_intent,
    )

    assert isinstance(routed, GuidedFireStageResult)
    assert routed.request.guider_id == intent.guider_id
    assert routed.request.shooter_id == intent.shooter_id
    assert routed.request.weapon_id == intent.weapon_id
    assert routed.request.target_id == intent.target.unit_id


def test_illegal_guided_intent_is_rejected_before_resolution() -> None:
    engine = _engine()
    invalid = FireIntent(
        mode=FireMode.GUIDED,
        shooter_id=10,
        guider_id=20,
        weapon_id=73,
        target=UnitTarget(999),
    )

    with pytest.raises(
        ValueError,
        match="GUIDED_FIRE_CAPABILITY_NOT_AVAILABLE",
    ):
        engine.stage_guided_fire_intent(invalid)


def test_legal_guided_intent_returns_complete_staged_effect_bundle() -> None:
    engine = _engine(target_concealed=True)
    capability = engine.get_guided_fire_capabilities("red")[0]

    staged = engine.stage_guided_fire_intent(_guided_intent(capability))

    assert staged.validation.valid is True
    assert staged.validation.reason == "LEGAL"
    assert staged.resolution.error is None
    assert staged.effect_bundle == staged.resolution.effect_bundle
    assert isinstance(staged.effect_bundle, EffectBundle)
    assert staged.effect_bundle.damage_effects[0].target_id == 30
    assert staged.effect_bundle.resource_effects[0].shooter_id == 10


def test_direct_route_and_execution_remain_unchanged() -> None:
    left = _engine(shooter_hex=114, guider_hex=113, target_hexes=(115,))
    right = _engine(shooter_hex=114, guider_hex=113, target_hexes=(115,))
    action = EngineAction(
        "red",
        10,
        ActionType.SHOOT,
        target_id=30,
        parameters={"weapon_id": 73},
    )

    assert ActionRouter().route_fire(action) == FireIntent(
        FireMode.DIRECT,
        shooter_id=10,
        weapon_id=73,
        target=UnitTarget(30),
    )
    assert left.step([action], delta_time_seconds=1) == right.step(
        [action],
        delta_time_seconds=1,
    )
    assert left.snapshot() == right.snapshot()


def test_guided_pipeline_does_not_change_world_or_publish_events() -> None:
    engine = _engine(target_concealed=True)
    intent = _guided_intent(engine.get_guided_fire_capabilities("red")[0])
    snapshot_before = engine.snapshot()
    events_before = engine._last_events

    staged = engine.stage_guided_fire_intent(intent)

    assert staged.resolution.damage_effects
    assert engine.snapshot()["world"] == snapshot_before["world"]
    assert engine._last_events == events_before


def test_guided_pipeline_does_not_advance_authoritative_rng() -> None:
    engine = _engine(target_concealed=True)
    intent = _guided_intent(engine.get_guided_fire_capabilities("red")[0])
    rng_before = engine.snapshot()["rng"]

    staged = engine.stage_guided_fire_intent(intent)

    assert staged.effect_bundle.rng_effect.draws_after > (
        staged.effect_bundle.rng_effect.draws_before
    )
    assert engine.snapshot()["rng"] == rng_before


def test_guided_pipeline_versions_policy_hash_and_preserves_step_hash(
    tmp_path: Path,
) -> None:
    engine = _engine(target_concealed=True)
    intent = _guided_intent(engine.get_guided_fire_capabilities("red")[0])
    engine.stage_guided_fire_intent(intent)

    result = run_joint_fire_example(tmp_path / "guided-pipeline-replay", seed=1)

    assert result["strict_replay_matches"] is True
    with ZipFile(result["replay"]) as archive:
        manifest = json.loads(
            archive.read("replay_manifest.native.jsonl").decode("utf-8")
        )
    assert manifest["deterministic_content_hash"] == (
        POLICY_V1_1_REPLAY_CONTENT_HASH
    )
    assert manifest["steps_sha256"] == HISTORICAL_REPLAY_STEPS_HASH


def test_same_tick_guided_intents_share_prestate_and_do_not_commit() -> None:
    engine = _engine(
        target_hexes=(115, 116),
        target_concealed=True,
    )
    capabilities = engine.get_guided_fire_capabilities("red")
    intents = tuple(_guided_intent(item) for item in reversed(capabilities))
    snapshot_before = engine.snapshot()
    events_before = engine._last_events

    staged = engine.stage_guided_fire_intents(intents)

    assert tuple(item.request.target_id for item in staged) == (30, 31)
    assert all(item.request.tick == snapshot_before["world"]["tick"] for item in staged)
    assert (
        staged[1].effect_bundle.rng_effect.draws_before
        == staged[0].effect_bundle.rng_effect.draws_after
    )
    assert engine.snapshot() == snapshot_before
    assert engine._last_events == events_before
    world = engine._world
    assert world is not None
    assert world.operators[30].alive is True
    assert world.operators[31].alive is True
