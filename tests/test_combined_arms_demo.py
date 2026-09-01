from __future__ import annotations

from pathlib import Path

from scripts.run_demo import run_registered_demo
from tests.test_realtime_dual_side_control import (
    Fixture,
    decoded,
    request,
    running_server,
)
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.demo_registry import load_default_demo_registry
from wargame_native_engine.viewer.operator_registry import load_operator_registry


def test_combined_arms_demo_contains_requested_interactive_formation() -> None:
    registry = load_default_demo_registry()
    descriptor = registry.resolve("combined_arms_test")
    bundle = ScenarioBundle.from_path(registry.scenario_path(descriptor))
    red = {
        (value.operator_type, value.subtype)
        for value in bundle.scenario.operators
        if value.side == "red"
    }

    assert red == {
        ("VEHICLE", "source-2-3"),
        ("PERSONNEL", "source-1-2"),
        ("VEHICLE", "source-2-0"),
        ("VEHICLE", "source-2-4"),
        ("AIR", "source-3-5"),
        ("VEHICLE", "source-2-1"),
        ("AIR", "source-3-7"),
    }

    operator_registry = load_operator_registry()
    assert {
        operator_registry.resolve_operator(type_code, subtype_code).display_name
        for type_code, subtype_code in (
            (2, 3), (1, 2), (2, 0), (2, 4), (3, 5), (2, 1), (3, 7)
        )
    } == {
        "炮兵", "步兵", "重型坦克", "无人战车", "无人机",
        "重型战车", "巡飞弹",
    }
    assert {
        operator_registry.resolve_operator(type_code, subtype_code).symbol.base
        for type_code, subtype_code in (
            (2, 3), (1, 2), (2, 0), (2, 4), (3, 5), (2, 1), (3, 7)
        )
    } == {
        "artillery", "infantry", "tank", "ugv", "drone",
        "armored_vehicle", "loitering_munition",
    }


def test_realtime_product_api_exposes_multi_weapon_and_fire_capabilities(
    tmp_path: Path,
) -> None:
    bundle = ScenarioBundle.from_path(
        "examples/demos/combined_arms_test/scenario.json"
    )
    fixture = Fixture(tmp_path, bundle=bundle)
    try:
        with running_server(fixture.server) as address:
            models = {}
            for actor_id in range(1, 8):
                status, payload, _ = request(
                    address,
                    "POST",
                    "/api/product/capabilities",
                    {"token": "red-token-123", "actor_ids": [actor_id]},
                )
                assert status == 200
                models[actor_id] = decoded(payload)
    finally:
        fixture.close()

    direct_weapons = {
        actor_id: [
            weapon["weapon_id"]
            for unit in model["direct_fire"]["units"]
            for weapon in unit["weapons"]
        ]
        for actor_id, model in models.items()
    }
    assert direct_weapons == {
        1: [],
        2: [29, 35],
        3: [36, 43],
        4: [4, 43, 74],
        5: [],
        6: [4, 43, 54, 73],
        7: [76],
    }
    assert [
        value["weapon_id"]
        for value in models[1]["indirect_fire"]["options"]
    ] == [72]
    assert models[4]["guided_fire"]["options"] == [{
        "capability": "GUIDED_FIRE",
        "guider_display_name": "无人战车 4",
        "guider_id": 4,
        "maximum_range": 20,
        "profile_id": None,
        "shooter_display_name": "重型战车 6",
        "shooter_id": 6,
        "target_id": 8,
        "weapon_display_name": "重型导弹",
        "weapon_id": 73,
    }]
    assert models[5]["guided_fire"]["options"] == []


def test_interactive_demo_previews_every_exposed_fire_path(
    tmp_path: Path,
) -> None:
    bundle = ScenarioBundle.from_path(
        "examples/demos/combined_arms_test/scenario.json"
    )
    fixture = Fixture(tmp_path, bundle=bundle)
    try:
        with running_server(fixture.server) as address:
            status, payload, _ = request(
                address,
                "GET",
                "/api/product/bootstrap?token=red-token-123",
            )
            assert status == 200
            observation_version = decoded(payload)["observation_version"]

            for actor_id in (2, 3, 4, 6, 7):
                status, payload, _ = request(
                    address,
                    "POST",
                    "/api/product/capabilities",
                    {"token": "red-token-123", "actor_ids": [actor_id]},
                )
                assert status == 200
                model = decoded(payload)
                for unit in model["direct_fire"]["units"]:
                    for weapon in unit["weapons"]:
                        target_id = weapon["legal_target_ids"][0]
                        weapon_id = weapon["weapon_id"]
                        status, payload, _ = request(
                            address,
                            "POST",
                            "/api/product/preview",
                            {
                                "token": "red-token-123",
                                "request_id": (
                                    f"combined-direct-{actor_id}-{weapon_id}"
                                ),
                                "observation_version": observation_version,
                                "actor_ids": [actor_id],
                                "operation_type": "SHOOT",
                                "target_id": target_id,
                                "weapon_choices": {str(actor_id): weapon_id},
                            },
                        )
                        assert status == 200
                        assert decoded(payload)["can_execute"] is True

            status, payload, _ = request(
                address,
                "POST",
                "/api/product/preview",
                {
                    "token": "red-token-123",
                    "request_id": "combined-guided-4-6-73",
                    "observation_version": observation_version,
                    "actor_ids": [4],
                    "operation_type": "GUIDED_FIRE",
                    "fire_mode": "GUIDED",
                    "guider_id": 4,
                    "shooter_id": 6,
                    "weapon_id": 73,
                    "target_id": 8,
                },
            )
            assert status == 200
            assert decoded(payload)["can_execute"] is True

            status, payload, _ = request(
                address,
                "POST",
                "/api/product/preview",
                {
                    "token": "red-token-123",
                    "request_id": "combined-indirect-1-72",
                    "observation_version": observation_version,
                    "actor_ids": [1],
                    "operation_type": "INDIRECT_FIRE",
                    "fire_mode": "INDIRECT",
                    "shooter_id": 1,
                    "weapon_id": 72,
                    "target_hex": 110,
                },
            )
            assert status == 200
            assert decoded(payload)["can_execute"] is True
    finally:
        fixture.close()


def test_combined_arms_demo_runs_and_strict_replays(tmp_path: Path) -> None:
    result = run_registered_demo(
        "combined_arms_test",
        output_dir=tmp_path / "combined-arms",
        overwrite=True,
    )

    assert result["status"] == "PASS"
    assert result["strict_replay_matches"] is True
    assert result["formation_roles"] == [
        "artillery",
        "drone",
        "heavy_combat_vehicle",
        "infantry",
        "loitering_munition",
        "tank",
        "unmanned_ground_vehicle",
    ]
