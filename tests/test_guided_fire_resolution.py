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
    DiceRoller,
    DirectFireAdjudicator,
    GuidedFireResolutionEvent,
)
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.event import EventType
from wargame_native_engine.core.rng import NativeRNG
from wargame_native_engine.core.world import WorldState


def _request(*, seed: int = 1, target_concealed: bool = True):
    engine = _engine(target_concealed=target_concealed)
    engine.reset(seed)
    capability = engine.get_guided_fire_capabilities("red")[0]
    return engine, engine.build_guided_fire_request(capability)


def test_legal_guided_fire_stages_hit_damage_and_resolution_event() -> None:
    engine, request = _request(seed=1)

    resolution = engine.resolve_guided_fire_request(request)

    assert resolution.error is None
    assert resolution.hit_result is True
    assert resolution.guider_id == 20
    assert resolution.shooter_id == 10
    assert resolution.weapon_id == 73
    assert resolution.target_id == 30
    assert len(resolution.damage_effects) == 1
    damage = resolution.damage_effects[0]
    assert damage.vehicle_losses == 1
    assert damage.destroyed is True
    assert resolution.resolution_event == GuidedFireResolutionEvent(
        guider_id=20,
        shooter_id=10,
        weapon_id=73,
        target_id=30,
        hit=True,
        damage_summary=resolution.damage_effects,
    )
    dice_evidence = tuple(
        item
        for item in resolution.adjudication_evidence
        if item.event_type is EventType.DICE_ROLLED
    )
    assert len(dice_evidence) == 2
    assert sum(len(item.data["individual_dice"]) for item in dice_evidence) == 4


def test_guider_visible_shooter_invisible_still_resolves() -> None:
    engine, request = _request(seed=1, target_concealed=True)
    world = engine._world
    assert world is not None
    assert request.target_id in world.operators[request.guider_id].raw_attributes[
        "see_enemy_bop_ids"
    ]
    assert request.target_id not in world.operators[
        request.shooter_id
    ].raw_attributes["see_enemy_bop_ids"]

    resolution = engine.resolve_guided_fire_request(request)

    assert resolution.error is None
    assert resolution.hit_result is True


def test_guider_invisible_request_fails_without_world_rng_or_event_change() -> None:
    engine, request = _request(seed=1, target_concealed=False)
    world = engine._world
    assert world is not None
    world.operators[request.guider_id].current_hex = 101
    world.operators[request.target_id].raw_attributes["concealed"] = True
    snapshot_before = engine.snapshot()
    events_before = engine._last_events

    with pytest.raises(
        ValueError,
        match="GUIDED_FIRE_VALIDATION_FAILED:TARGET_NOT_OBSERVED_BY_GUIDER",
    ):
        engine.resolve_guided_fire_request(request)

    assert engine.snapshot() == snapshot_before
    assert engine._last_events == events_before


def test_weapon_73_damage_and_rng_exactly_reuse_direct_fire_stage() -> None:
    engine, request = _request(seed=1)
    assert engine._world is not None
    assert engine._rng is not None
    assert engine._adjudication_tables is not None
    pre_state = WorldState.from_dict(engine._world.to_dict())
    direct_request = engine._guided_direct_fire_request(request, pre_state)
    direct_rng = NativeRNG.restore(engine._rng.snapshot())
    direct = DirectFireAdjudicator(
        engine._adjudication_tables,
        DiceRoller(direct_rng),
    ).stage(direct_request)

    guided = engine.resolve_guided_fire_request(request)

    assert direct.outcome is not None
    assert guided.damage_effects[0].vehicle_losses == direct.outcome.vehicle_losses
    assert guided.damage_effects[0].squad_losses == direct.outcome.squad_losses
    assert (
        guided.damage_effects[0].suppression_applied
        == direct.outcome.suppression_applied
    )
    assert guided.damage_effects[0].destroyed == direct.outcome.destroyed
    assert guided.rng_effect.state_after == direct_rng.snapshot()["state"]
    assert guided.rng_effect.draws_after == direct_rng.snapshot()["draws"]


def test_existing_direct_weapon_73_result_is_unchanged() -> None:
    engine = _engine(target_concealed=False)
    engine.reset(1)
    action = EngineAction(
        "red",
        10,
        ActionType.SHOOT,
        target_id=30,
        parameters={"weapon_id": 73},
    )

    result = engine.step([action], delta_time_seconds=1)
    world = engine._world
    assert world is not None

    assert result.decisions[0].accepted is True
    assert world.operators[30].blood == 0
    assert world.operators[30].alive is False
    assert world.operators[10].raw_attributes["ammunition"]["100"] == 9
    assert world.operators[10].weapon_cooldown_remaining_ms == 74_000
    assert engine.snapshot()["rng"]["draws"] == 4


def test_guided_resolution_is_deterministic_for_same_seed() -> None:
    left_engine, left_request = _request(seed=5)
    right_engine, right_request = _request(seed=5)

    left = left_engine.resolve_guided_fire_request(left_request)
    right = right_engine.resolve_guided_fire_request(right_request)

    assert left == right
    assert left.effect_bundle == right.effect_bundle


def test_resolution_uses_only_direct_fire_rng_draws_and_does_not_commit_rng() -> None:
    engine, request = _request(seed=1)
    snapshot_before = engine.snapshot()

    resolution = engine.resolve_guided_fire_request(request)

    assert resolution.rng_effect.draws_before == snapshot_before["rng"]["draws"]
    assert resolution.rng_effect.draws_after - resolution.rng_effect.draws_before == 4
    assert engine.snapshot() == snapshot_before


def test_effect_bundle_has_damage_ammo_cooldown_and_guided_recovery() -> None:
    engine, request = _request(seed=1)

    bundle = engine.resolve_guided_fire_request(request).effect_bundle

    assert len(bundle.damage_effects) == 1
    assert len(bundle.resource_effects) == 1
    resource = bundle.resource_effects[0]
    assert resource.shooter_id == 10
    assert resource.ammunition_key == "100"
    assert resource.ammunition_before == 10
    assert resource.ammunition_after == 9
    assert resource.cooldown_before_ms == 0
    assert resource.cooldown_after_ms == 75_000
    assert tuple(
        (item.operator_id, item.recovery_remaining_ms)
        for item in resource.recovery_updates
    ) == ((10, 75_000), (20, 75_000))


def test_successful_resolution_does_not_mutate_world_or_emit_engine_event() -> None:
    engine, request = _request(seed=1)
    snapshot_before = engine.snapshot()
    events_before = engine._last_events

    resolution = engine.resolve_guided_fire_request(request)

    assert resolution.hit_result is True
    assert engine.snapshot() == snapshot_before
    assert engine._last_events == events_before


def test_policy_versioned_replay_hash_preserves_steps_and_strict_replay(
    tmp_path: Path,
) -> None:
    result = run_joint_fire_example(tmp_path / "guided-resolution-replay", seed=1)

    assert result["strict_replay_matches"] is True
    with ZipFile(result["replay"]) as archive:
        manifest = json.loads(
            archive.read("replay_manifest.native.jsonl").decode("utf-8")
        )
    assert manifest["deterministic_content_hash"] == (
        POLICY_V1_1_REPLAY_CONTENT_HASH
    )
    assert manifest["steps_sha256"] == HISTORICAL_REPLAY_STEPS_HASH
