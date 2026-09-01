from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from examples.run_joint_fire import run_example as run_joint_fire_example
from wargame_native_engine.core.action import ActionType
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.fire_capability import GuidedFireCapability
from wargame_native_engine.io.scenario import scenario_from_dict


POLICY_V1_1_REPLAY_CONTENT_HASH = (
    "e258afb894723ab43a60761380afe6b84fa8bebc6a1124d59535890410a74933"
)
HISTORICAL_REPLAY_STEPS_HASH = (
    "977a3510e9371c77916e923f42dc0003984aba0c77277942abd4be187f245dd4"
)


def _operator(
    operator_id: int,
    side: str,
    current_hex: int,
    *,
    weapon_ids: list[int] | None = None,
    concealed: bool = False,
    guidance_profile_id: str | None = None,
    guidance_affiliation_id: int | None = None,
) -> dict[str, object]:
    evidence = (
        None
        if guidance_affiliation_id is None
        else {
            "source_field": "native_supplement.guidance_affiliation_id",
            "source_value": guidance_affiliation_id,
            "mapping_version": "source-guidance-authority/1.0",
        }
    )
    row, column = divmod(current_hex, 100)
    value: dict[str, object] = {
        "operator_id": operator_id,
        "side": side,
        "operator_type": "VEHICLE",
        "subtype": "source-2-4" if guidance_profile_id else "source-2-1",
        "x": column,
        "y": row,
        "altitude": 0,
        "heading": 0,
        "health": 100,
        "max_health": 100,
        "blood": 1,
        "max_blood": 1,
        "speed": 0,
        "max_speed": 36,
        "basic_speed": 36,
        "move_range": 0,
        "sensor_range": 25,
        "state": "STOPPED",
        "alive": True,
        "deployed": True,
        "weapon_ids": list(weapon_ids or []),
        "current_hex": current_hex,
        "cur_pos_millionths": 0,
        "move_path": [],
        "movement_mode": "NORMAL",
        "stopped": True,
        "weapon_unfolded": True,
        "weapon_cooldown_remaining_ms": 0,
        "guided_fire_recovery_remaining_ms": 0,
        "raw_attributes": {
            "armor_class": "LIGHT",
            "ammunition": {"0": 10, "100": 10},
            "concealed": concealed,
        },
    }
    if guidance_profile_id is not None:
        value["guidance_profile_id"] = guidance_profile_id
    if guidance_affiliation_id is not None:
        value["guidance_affiliation_id"] = guidance_affiliation_id
        value["guidance_affiliation_evidence"] = evidence
    return value


def _scenario_record(
    *,
    shooter_hex: int = 101,
    guider_hex: int = 114,
    target_hexes: tuple[int, ...] = (115,),
    target_concealed: bool = False,
    affiliation_id: int | None = 10,
    shooter_weapon_id: int = 73,
    reverse_input: bool = False,
    add_wrong_affiliate: bool = False,
) -> dict[str, object]:
    hexes = []
    for column in range(1, 25):
        hex_id = 100 + column
        hexes.append({
            "pos": hex_id,
            "elev": 0,
            "cond": 0,
            "neighbors": [
                -1 if column == 24 else hex_id + 1,
                -1,
                -1,
                -1 if column == 1 else hex_id - 1,
                -1,
                -1,
            ],
            "roads": [0, 0, 0, 0, 0, 0],
            "rivers": [0, 0, 0, 0, 0, 0],
        })
    operators = [
        _operator(10, "red", shooter_hex, weapon_ids=[shooter_weapon_id]),
        _operator(
            20,
            "red",
            guider_hex,
            guidance_profile_id="GROUND_AFFILIATED_GUIDER",
            guidance_affiliation_id=affiliation_id,
        ),
    ]
    if add_wrong_affiliate:
        operators.append(_operator(11, "red", 102))
    operators.extend(
        _operator(
            30 + index,
            "blue",
            target_hex,
            concealed=target_concealed,
        )
        for index, target_hex in enumerate(target_hexes)
    )
    if reverse_input:
        operators.reverse()
    deployment = {"red": {}, "blue": {}}
    for operator in operators:
        side = str(operator["side"])
        deployment[side][str(operator["operator_id"])] = [
            operator["x"], operator["y"]
        ]
    return {
        "schema_version": "native-scenario/1.0",
        "scenario_id": "guided-capability-test",
        "initial_stage": "combat",
        "map": {
            "map_id": "guided-capability-line-map",
            "width": 24,
            "height": 3,
            "distance_unit": "hex",
            "blocked_positions": [],
            "hex_size_m": 200,
            "elevation_unit_m": 20,
            "elevation_kind": "LEVEL",
            "roadblocks": [],
            "hexes": hexes,
        },
        "sides": ["red", "blue"],
        "operators": operators,
        "objectives": [],
        "deployment": {"positions": deployment},
        "terminal_rules": {
            "max_ticks": 10,
            "elimination": True,
            "objective_score_to_win": None,
        },
        "metadata": {
            "fixture_type": "project_authored_guided_capability",
            "source_compatible": {
                "weapons": {
                    "36": {
                        "weapon_class": "large_direct_fire_gun",
                        "maximum_range": 10,
                        "target_domains": ["PERSONNEL", "VEHICLE"],
                        "ammunition_key": "0",
                        "requires_unfolded": True,
                        "can_fire_while_moving": False,
                    },
                    "73": {
                        "weapon_class": "heavy_missile",
                        "maximum_range": 20,
                        "target_domains": ["VEHICLE"],
                        "ammunition_key": "100",
                        "requires_unfolded": True,
                        "can_fire_while_moving": False,
                    },
                }
            },
        },
    }


def _engine(**kwargs) -> NativeEngine:
    engine = NativeEngine(scenario_from_dict(_scenario_record(**kwargs)))
    engine.reset(42)
    return engine


def test_affiliated_ugv_returns_frozen_weapon_73_capability() -> None:
    engine = _engine()

    capabilities = engine.get_guided_fire_capabilities("red")

    assert capabilities == (GuidedFireCapability(
        guider_id=20,
        shooter_id=10,
        weapon_id=73,
        target_id=30,
        guidance_profile_id="GROUND_AFFILIATED_GUIDER",
    ),)
    with pytest.raises(FrozenInstanceError):
        capabilities[0].target_id = 99  # type: ignore[misc]


def test_ugv_without_affiliation_returns_no_capability() -> None:
    engine = _engine(affiliation_id=None)

    assert engine.get_guided_fire_capabilities("red") == ()


def test_wrong_affiliation_does_not_authorize_another_shooter() -> None:
    engine = _engine(affiliation_id=11, add_wrong_affiliate=True)

    assert engine.get_guided_fire_capabilities("red") == ()


def test_guider_observation_authorizes_when_shooter_cannot_see_target() -> None:
    engine = _engine(target_concealed=True)
    world = engine._world
    assert world is not None
    assert 30 not in world.operators[10].raw_attributes["see_enemy_bop_ids"]
    assert 30 in world.operators[20].raw_attributes["see_enemy_bop_ids"]

    assert engine.get_guided_fire_capabilities("red")


def test_missing_guider_observation_rejects_even_when_shooter_sees_target() -> None:
    engine = _engine(
        shooter_hex=114,
        guider_hex=101,
        target_concealed=True,
    )
    world = engine._world
    assert world is not None
    assert 30 in world.operators[10].raw_attributes["see_enemy_bop_ids"]
    assert 30 not in world.operators[20].raw_attributes["see_enemy_bop_ids"]

    assert engine.get_guided_fire_capabilities("red") == ()


def test_non_guided_weapon_returns_no_capability() -> None:
    engine = _engine(shooter_weapon_id=36)

    assert engine.get_guided_fire_capabilities("red") == ()


def test_target_outside_shooter_weapon_range_returns_no_capability() -> None:
    engine = _engine(
        shooter_hex=101,
        guider_hex=122,
        target_hexes=(123,),
    )

    assert engine.get_guided_fire_capabilities("red") == ()


@pytest.mark.parametrize(
    ("role", "updates"),
    [
        ("guider", {"alive": False}),
        ("guider", {"deployed": False}),
        ("guider", {"guided_fire_recovery_remaining_ms": 1}),
        ("guider", {"stopped": False}),
        ("guider", {"weapon_unfolded": False}),
        ("guider", {"suppression_remaining_ms": 1}),
        ("shooter", {"alive": False}),
        ("shooter", {"deployed": False}),
        ("shooter", {"guided_fire_recovery_remaining_ms": 1}),
        ("shooter", {"weapon_cooldown_remaining_ms": 1}),
        ("shooter", {"weapon_unfolded": False}),
        ("shooter", {"stopped": False}),
        ("shooter", {"transition_kind": "WEAPON_TRANSITION"}),
    ],
)
def test_guider_and_shooter_status_constraints_fail_closed(
    role: str,
    updates: dict[str, object],
) -> None:
    engine = _engine()
    world = engine._world
    assert world is not None
    operator = world.operators[20 if role == "guider" else 10]
    for name, value in updates.items():
        setattr(operator, name, value)

    assert engine.get_guided_fire_capabilities("red") == ()


def test_capability_order_is_stable_when_input_order_is_reversed() -> None:
    normal = _engine(target_hexes=(115, 116))
    reversed_input = _engine(target_hexes=(115, 116), reverse_input=True)

    expected = (
        GuidedFireCapability(20, 10, 73, 30, "GROUND_AFFILIATED_GUIDER"),
        GuidedFireCapability(20, 10, 73, 31, "GROUND_AFFILIATED_GUIDER"),
    )
    assert normal.get_guided_fire_capabilities("red") == expected
    assert reversed_input.get_guided_fire_capabilities("red") == expected


def test_query_preserves_world_rng_events_and_legacy_valid_actions() -> None:
    engine = _engine()
    snapshot_before = engine.snapshot()
    events_before = engine._last_events
    valid_before = tuple(
        value.to_dict() for value in engine.valid_actions("red")
    )

    capabilities = engine.get_guided_fire_capabilities("red")

    assert capabilities
    assert engine.snapshot() == snapshot_before
    assert engine._last_events == events_before
    assert tuple(
        value.to_dict() for value in engine.valid_actions("red")
    ) == valid_before
    assert all(
        value.action.action_type is not ActionType.SHOOT
        or "mode" not in value.action.parameters
        for value in engine.valid_actions("red")
    )


def test_policy_versioned_replay_hash_preserves_steps_and_strict_replay(
    tmp_path: Path,
) -> None:
    result = run_joint_fire_example(tmp_path / "guided-capability-replay", seed=1)
    assert result["strict_replay_matches"] is True
    with ZipFile(result["replay"]) as archive:
        manifest = json.loads(
            archive.read("replay_manifest.native.jsonl").decode("utf-8")
        )
    assert manifest["deterministic_content_hash"] == (
        POLICY_V1_1_REPLAY_CONTENT_HASH
    )
    assert manifest["steps_sha256"] == HISTORICAL_REPLAY_STEPS_HASH
    assert manifest["perspective_policy_version"] == "1.1.0"
