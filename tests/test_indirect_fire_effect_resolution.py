from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from wargame_native_engine.adjudication.indirect_fire import (
    IndirectFireEffectStatus,
    IndirectFireTriState,
)
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.indirect_fire import FireMissionPhase
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = (
    REPOSITORY_ROOT / "examples/demos/direct_fire_basic/scenario.json"
)


def _hexes(size: int = 9) -> list[dict[str, object]]:
    existing = {
        row * 100 + column
        for row in range(1, size + 1)
        for column in range(1, size + 1)
    }
    result = []
    for row in range(1, size + 1):
        for column in range(1, size + 1):
            candidates = (
                row * 100 + column + 1,
                (row + 1) * 100 + column,
                (row + 1) * 100 + column - 1,
                row * 100 + column - 1,
                (row - 1) * 100 + column - 1,
                (row - 1) * 100 + column,
            )
            result.append({
                "pos": row * 100 + column,
                "elev": 0,
                "cond": 0,
                "neighbors": [
                    value if value in existing else -1 for value in candidates
                ],
                "roads": [0, 0, 0, 0, 0, 0],
                "rivers": [0, 0, 0, 0, 0, 0],
            })
    return result


def _active_impact(
    *,
    seed: int = 5,
    target_armor: str = "LIGHT",
    extra_target_ids: tuple[int, ...] = (),
) -> tuple[NativeEngine, object]:
    payload = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    payload["scenario_id"] = "indirect-fire-effect-resolution-test"
    payload["map"].update({
        "map_id": "indirect-effect-9x9",
        "width": 9,
        "height": 9,
        "hexes": _hexes(),
    })
    payload["terminal_rules"]["max_ticks"] = 500
    shooter = payload["operators"][0]
    shooter["weapon_ids"] = [72]
    shooter.update({"current_hex": 101, "x": 1, "y": 1})
    target = payload["operators"][1]
    target.update({"current_hex": 505, "x": 5, "y": 5})
    target["raw_attributes"]["armor_class"] = target_armor
    payload["deployment"]["positions"]["red"]["1"] = [1, 1]
    payload["deployment"]["positions"]["blue"]["2"] = [5, 5]
    for operator_id in extra_target_ids:
        value = deepcopy(target)
        value["operator_id"] = operator_id
        payload["operators"].append(value)
        payload["deployment"]["positions"]["blue"][str(operator_id)] = [5, 5]

    engine = NativeEngine(scenario_from_dict(payload))
    engine.reset(seed)
    capability = engine.get_indirect_fire_capabilities("red")[0]
    plan = engine.build_indirect_fire_plan_request(capability, 505)
    assert plan.valid is True and plan.request is not None
    mission = engine.create_indirect_fire_mission(plan.request)
    engine.step([], delta_time_ms=150_000)
    assert engine._world is not None  # noqa: SLF001
    assert engine._world.indirect_fire_missions[0].phase is FireMissionPhase.ACTIVE  # noqa: SLF001
    impact = engine.resolve_indirect_fire_impact(mission.mission_id)
    assert impact.valid is True and impact.resolution is not None
    return engine, impact.resolution


def test_impact_resolution_generates_frozen_damage_effects() -> None:
    engine, impact = _active_impact(seed=5)

    result = engine.resolve_indirect_fire_effects(impact)

    assert result.status is IndirectFireEffectStatus.RESOLVED
    assert result.blockers == ()
    assert result.effect_bundle is not None
    assert len(result.effect_bundle.damage_effects) == 1
    effect = result.effect_bundle.damage_effects[0]
    assert effect.operator_id == 2
    assert effect.damage_result == "LOSS_1"
    assert effect.suppression_result is IndirectFireTriState.FALSE
    assert effect.destroyed_result is IndirectFireTriState.FALSE


def test_affected_operator_damage_effects_use_id_order() -> None:
    engine, impact = _active_impact(
        seed=5,
        extra_target_ids=(9, 4),
    )

    result = engine.resolve_indirect_fire_effects(impact)

    assert result.effect_bundle is not None
    assert tuple(
        value.operator_id for value in result.effect_bundle.damage_effects
    ) == (2, 4, 9)


def test_same_seed_produces_identical_effect_contract() -> None:
    left, left_impact = _active_impact(seed=5)
    right, right_impact = _active_impact(seed=5)

    assert (
        left.resolve_indirect_fire_effects(left_impact)
        == right.resolve_indirect_fire_effects(right_impact)
    )


def test_different_seed_changes_resolved_effect() -> None:
    left, left_impact = _active_impact(seed=4)
    right, right_impact = _active_impact(seed=5)

    left_result = left.resolve_indirect_fire_effects(left_impact)
    right_result = right.resolve_indirect_fire_effects(right_impact)

    assert left_result.status is IndirectFireEffectStatus.RESOLVED
    assert right_result.status is IndirectFireEffectStatus.RESOLVED
    assert left_result.effect_bundle != right_result.effect_bundle
    assert left_result.effect_bundle is not None
    assert right_result.effect_bundle is not None
    assert left_result.effect_bundle.damage_effects[0].damage_result == "NO_EFFECT"
    assert right_result.effect_bundle.damage_effects[0].damage_result == "LOSS_1"


def test_missing_unarmored_modifier_returns_unresolved_rule() -> None:
    engine, impact = _active_impact(seed=5, target_armor="NONE")

    result = engine.resolve_indirect_fire_effects(impact)

    assert result.status is IndirectFireEffectStatus.UNRESOLVED_RULE
    assert result.effect_bundle is not None
    effect = result.effect_bundle.damage_effects[0]
    assert effect.damage_result == "UNRESOLVED_RULE"
    assert effect.resolution_evidence.blockers == ("unknown_vehicle_armor",)


def test_nonzero_result_correction_uses_explicit_damage_policy() -> None:
    engine, impact = _active_impact(seed=1)

    result = engine.resolve_indirect_fire_effects(impact)

    assert result.status is IndirectFireEffectStatus.RESOLVED
    assert result.effect_bundle is not None
    effect = result.effect_bundle.damage_effects[0]
    assert effect.resolution_evidence.result_correction in {-2, -1, 1}
    assert effect.resolution_evidence.blockers == ()


def test_damage_rng_starts_after_scatter_and_uses_three_draws_per_target() -> None:
    engine, impact = _active_impact(seed=5)

    result = engine.resolve_indirect_fire_effects(impact)

    assert result.effect_bundle is not None
    transition = result.effect_bundle.damage_rng_transition
    assert transition is not None
    assert transition.before == impact.rng_transition.after
    assert transition.draw_count == 3
    assert result.effect_bundle.rng_transition.draw_count == 6


def test_effect_resolution_does_not_modify_world_rng_or_events() -> None:
    engine, impact = _active_impact(seed=5)
    before = deepcopy(engine.snapshot())
    before_events = engine._last_events  # noqa: SLF001

    result = engine.resolve_indirect_fire_effects(impact)

    assert result.effect_bundle is not None
    assert engine.snapshot() == before
    assert engine.snapshot()["rng"] == before["rng"]
    assert engine._last_events == before_events  # noqa: SLF001


def test_effect_resolution_preserves_snapshot_hash() -> None:
    engine, impact = _active_impact(seed=5)
    before_hash = canonical_hash(engine.snapshot())

    engine.resolve_indirect_fire_effects(impact)

    assert canonical_hash(engine.snapshot()) == before_hash
