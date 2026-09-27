from __future__ import annotations

from pathlib import Path

from wargame_native_engine.gateway.models import ControllerView
from wargame_native_engine.viewer.human_capabilities import (
    HUMAN_CAPABILITY_SCHEMA,
    build_human_capabilities,
)


STATIC = Path("src/wargame_native_engine/viewer/static")


def _view(*, weapon_ids: tuple[int, ...] = (36, 43)) -> ControllerView:
    valid_actions = tuple(
        {
            "action_id": f"shoot-{weapon_id}-{target_id}",
            "action": {
                "side": "red",
                "actor_id": 103,
                "action_type": "SHOOT",
                "target_id": target_id,
                "parameters": {"weapon_id": weapon_id},
            },
        }
        for weapon_id, target_id in zip(weapon_ids, (203, 202))
    )
    return ControllerView(
        "session-1",
        "RED",
        0,
        {
            "friendly_operators": [{"operator_id": 103}],
            "visible_enemies": [
                {"operator_id": 202},
                {"operator_id": 203},
            ],
        },
        {
            "operators": [{
                "obj_id": 103,
                "type": 2,
                "sub_type": 0,
                "remain_bullet_nums": {"main": 5, "secondary": 11},
                "weapon_cool_time": 0,
            }],
            "passengers": [],
        },
        valid_actions,
        False,
        None,
        None,
    )


def _summary() -> dict[str, object]:
    return {
        "selected_count": 1,
        "selected_units": [{
            "unit_id": 103,
            "display_name": "重型坦克 103",
        }],
        "operations": {
            "MOVE": {"applicable_count": 1, "unavailable": []},
            "SHOOT": {"applicable_count": 1, "unavailable": []},
            "STOP_MOVE": {"applicable_count": 0, "unavailable": []},
            "WEAPON_UNFOLD": {"applicable_count": 0, "unavailable": []},
            "WEAPON_LOCK": {"applicable_count": 1, "unavailable": []},
            "WAIT": {"applicable_count": 1, "unavailable": []},
            "CAPTURE_OBJECTIVE": {"applicable_count": 1, "unavailable": []},
        },
        "shoot_target_ids": [202, 203],
    }


def test_tank_capabilities_include_direct_fire_without_future_operations() -> None:
    result = build_human_capabilities(
        _summary(),
        _view(),
        [103],
        weapon_profiles={
            "36": {
                "maximum_range": 10,
                "target_domains": ["PERSONNEL", "VEHICLE"],
                "ammunition_key": "main",
            },
            "43": {
                "maximum_range": 4,
                "target_domains": ["PERSONNEL"],
                "ammunition_key": "secondary",
            },
        },
    )

    assert result["schema"] == HUMAN_CAPABILITY_SCHEMA
    capability_types = [
        value["capability_type"] for value in result["capabilities"]
    ]
    assert capability_types == ["MOVE", "DIRECT_FIRE", "WEAPON_LOCK", "WAIT"]
    assert "CAPTURE_OBJECTIVE" not in capability_types
    direct_fire = result["direct_fire"]["units"][0]
    assert direct_fire["operator_key"] == "2_0"
    assert [value["weapon_id"] for value in direct_fire["weapons"]] == [36, 43]
    assert direct_fire["weapons"][0] == {
        "weapon_id": 36,
        "display_name": "大号直瞄炮",
        "weapon_class": "large_direct_fire_gun",
        "maximum_range": 10,
        "target_domains": ["PERSONNEL", "VEHICLE"],
        "ammunition": 5,
        "cooldown_remaining_seconds": 0,
        "ammunition_status": "KNOWN",
        "ammunition_pool": "main",
        "shared_ammunition": False,
        "shared_with_weapon_ids": [],
        "ammunition_cost": None,
        "attack_modes": [],
        "ready_state": "STATE_REPORTED",
        "legal_target_ids": [203],
        "profile_source": "scenario_weapon_profile",
    }
    assert direct_fire["weapons"][1]["legal_target_ids"] == [202]


def test_single_weapon_capability_is_a_single_explicit_choice() -> None:
    summary = _summary()
    result = build_human_capabilities(summary, _view(weapon_ids=(36,)), [103])

    weapons = result["direct_fire"]["units"][0]["weapons"]
    assert len(weapons) == 1
    assert weapons[0]["weapon_id"] == 36
    assert weapons[0]["legal_target_ids"] == [203]


def test_complete_inventory_uses_authoritative_shared_pool_without_legal_shoot() -> None:
    view = ControllerView(
        "session-1",
        "RED",
        4,
        {
            "friendly_operators": [{"operator_id": 103}],
            "visible_enemies": [{"operator_id": 203}],
        },
        {
            "operators": [{
                "obj_id": 103,
                "type": 2,
                "sub_type": 0,
                # Deliberately stale compatibility data must not win.
                "remain_bullet_nums": {"0": 30},
                "weapon_cool_time": 0,
            }],
            "passengers": [],
        },
        (),
        False,
        None,
        None,
        own_weapon_inventory=({
            "operator_id": 103,
            "weapons": [
                {
                    "weapon_id": 36,
                    "attack_modes": ["DIRECT"],
                    "resource_policy": "AMMUNITION_KEY",
                    "ammunition_key": "0",
                    "ammunition": 29,
                    "ammunition_cost": None,
                    "shared_with_weapon_ids": [36, 43],
                },
                {
                    "weapon_id": 43,
                    "attack_modes": ["DIRECT"],
                    "resource_policy": "AMMUNITION_KEY",
                    "ammunition_key": "0",
                    "ammunition": 29,
                    "ammunition_cost": None,
                    "shared_with_weapon_ids": [36, 43],
                },
            ],
            "ammunition_pools": {"0": 29},
            "weapon_cooldown_remaining_ms": 2_000,
        },),
    )
    summary = _summary()
    summary["operations"]["SHOOT"] = {
        "applicable_count": 0,
        "unavailable": [{"unit_id": 103, "code": "no_legal_shoot"}],
    }
    result = build_human_capabilities(
        summary,
        view,
        [103],
        weapon_profiles={
            "36": {"ammunition_key": "0"},
            "43": {"ammunition_key": "0"},
        },
    )

    weapons = result["weapon_inventory"]["units"][0]["weapons"]
    assert [value["weapon_id"] for value in weapons] == [36, 43]
    assert [value["ammunition"] for value in weapons] == [29, 29]
    assert all(value["shared_ammunition"] for value in weapons)
    assert all(value["ready_state"] == "COOLDOWN" for value in weapons)
    assert all(value["legal_target_ids"] == [] for value in weapons)
    assert result["direct_fire"]["selectable_target_ids"] == [203]
    direct = next(
        value for value in result["capabilities"]
        if value["capability_type"] == "DIRECT_FIRE"
    )
    assert direct["metadata"]["intent_only"] is True


def test_capability_projection_does_not_mutate_inputs() -> None:
    summary = _summary()
    before = repr(summary)

    build_human_capabilities(summary, _view(), [103])

    assert repr(summary) == before


def test_frontend_weapon_switch_clears_preview_and_submits_explicit_weapon() -> None:
    source = (STATIC / "dual_control.js").read_text(encoding="utf-8")
    selection = source[
        source.index("async function selectDirectFireWeapon"):
        source.index("async function activateCapability")
    ]
    target_click = source[
        source.index('if (activeTool === "SHOOT"'):
        source.index("if (!isFriendlySelectable(unit)) return")
    ]

    assert "selectedWeaponIds.set(Number(unitId), Number(value.weapon_id))" in selection
    assert "targetUnitId = null" in selection
    assert "operationPreview = null" in selection
    assert "executeRequestId = null" in selection
    assert "...selectedWeaponChoices()" in target_click
    assert "weapon_choices" in source
    assert "Object.fromEntries" in source


def test_frontend_filters_unknown_future_capabilities() -> None:
    source = (STATIC / "capability_ui.js").read_text(encoding="utf-8")

    assert 'SUPPORTED.has(value.capability_type)' in source
    assert '"DIRECT_FIRE"' in source
    assert '"CAPTURE_OBJECTIVE"' not in source
