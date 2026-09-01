from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from tests.test_guided_capability import _engine
from wargame_native_engine.adjudication import (
    GuidedFireEffectPlan,
    GuidedFireRecoveryUpdate,
    GuidedFireValidationResult,
)


def _request(**engine_options):
    engine = _engine(**engine_options)
    capability = engine.get_guided_fire_capabilities("red")[0]
    return engine, engine.build_guided_fire_request(capability)


def test_legal_request_validates_and_builds_frozen_effect_plan() -> None:
    engine, request = _request()

    validation = engine.validate_guided_fire_request(request)
    plan = engine.build_guided_fire_effect_plan(request)

    assert validation == GuidedFireValidationResult(True, "LEGAL", request)
    assert plan == GuidedFireEffectPlan(
        shooter_id=10,
        guider_id=20,
        weapon_id=73,
        target_id=30,
        ammo_consume=1,
        recovery_updates=(
            GuidedFireRecoveryUpdate(10, 75_000),
            GuidedFireRecoveryUpdate(20, 75_000),
        ),
    )
    with pytest.raises(FrozenInstanceError):
        plan.ammo_consume = 2  # type: ignore[misc]


def test_guider_visibility_is_required_but_shooter_visibility_is_not() -> None:
    engine, request = _request(target_concealed=True)
    world = engine._world
    assert world is not None
    assert request.target_id not in world.operators[request.shooter_id].raw_attributes[
        "see_enemy_bop_ids"
    ]
    assert request.target_id in world.operators[request.guider_id].raw_attributes[
        "see_enemy_bop_ids"
    ]

    assert engine.validate_guided_fire_request(request).valid is True


def test_guider_no_longer_observing_target_is_rejected() -> None:
    engine, request = _request()
    world = engine._world
    assert world is not None
    world.operators[request.guider_id].current_hex = 101
    world.operators[request.target_id].raw_attributes["concealed"] = True

    validation = engine.validate_guided_fire_request(request)

    assert validation.valid is False
    assert validation.reason == "TARGET_NOT_OBSERVED_BY_GUIDER"


def test_weapon_73_is_guided_but_direct_only_weapon_authority_is_rejected() -> None:
    guided_engine, guided_request = _request()
    assert guided_request.weapon_id == 73
    assert guided_engine.validate_guided_fire_request(guided_request).valid is True

    direct_only_engine, direct_only_request = _request()
    direct_only_engine._guidance_authority = replace(
        direct_only_engine._guidance_authority,
        weapon_fire_modes=(),
    )

    validation = direct_only_engine.validate_guided_fire_request(
        direct_only_request
    )
    assert validation.valid is False
    assert validation.reason == "WEAPON_GUIDED_MODE_UNSUPPORTED"


def test_ammunition_exhaustion_is_rejected() -> None:
    engine, request = _request()
    world = engine._world
    assert world is not None
    world.operators[request.shooter_id].raw_attributes["ammunition"]["100"] = 0

    validation = engine.validate_guided_fire_request(request)

    assert validation.valid is False
    assert validation.reason == "AMMUNITION_UNAVAILABLE"


def test_weapon_cooldown_is_rejected() -> None:
    engine, request = _request()
    world = engine._world
    assert world is not None
    world.operators[request.shooter_id].weapon_cooldown_remaining_ms = 1

    validation = engine.validate_guided_fire_request(request)

    assert validation.valid is False
    assert validation.reason == "WEAPON_ON_COOLDOWN"


def test_destroyed_target_is_rejected() -> None:
    engine, request = _request()
    world = engine._world
    assert world is not None
    world.operators[request.target_id].alive = False

    validation = engine.validate_guided_fire_request(request)

    assert validation.valid is False
    assert validation.reason == "TARGET_NOT_ALIVE"


def test_validation_and_effect_planning_change_no_world_rng_or_events() -> None:
    engine, request = _request()
    snapshot_before = engine.snapshot()
    events_before = engine._last_events

    validation = engine.validate_guided_fire_request(request)
    plan = engine.build_guided_fire_effect_plan(request)

    assert validation.valid is True
    assert plan.ammo_consume == 1
    assert engine.snapshot() == snapshot_before
    assert engine._last_events == events_before
