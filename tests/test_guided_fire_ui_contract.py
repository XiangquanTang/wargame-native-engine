from __future__ import annotations

from pathlib import Path

from tests.test_guided_capability import _scenario_record
from tests.test_realtime_dual_side_control import (
    Fixture,
    decoded,
    request,
    running_server,
)
from wargame_native_engine.agents import CommandStatus
from wargame_native_engine.data import DataProvenance, ScenarioBundle
from wargame_native_engine.io.serialization import canonical_hash


STATIC = Path("src/wargame_native_engine/viewer/static")


def _bundle(**kwargs: object) -> ScenarioBundle:
    record = _scenario_record(**kwargs)
    return ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256=canonical_hash(record),
            source_kind="project_authored_guided_fire_ui_contract",
        ),
    )


def _bootstrap(address: tuple[str, int]) -> dict[str, object]:
    status, payload, _ = request(
        address,
        "GET",
        "/api/product/bootstrap?token=red-token-123",
    )
    assert status == 200, payload
    return decoded(payload)


def _capabilities(
    address: tuple[str, int], actor_id: int,
) -> dict[str, object]:
    status, payload, _ = request(
        address,
        "POST",
        "/api/product/capabilities",
        {"token": "red-token-123", "actor_ids": [actor_id]},
    )
    assert status == 200, payload
    return decoded(payload)


def test_legal_guided_capability_is_projected_from_engine_action(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path, bundle=_bundle(target_concealed=True))
    try:
        with running_server(fixture.server) as address:
            model = _capabilities(address, 20)

            guided = next(
                value for value in model["capabilities"]
                if value["capability_type"] == "GUIDED_FIRE"
            )
            assert guided["operation_type"] == "GUIDED_FIRE"
            assert guided["metadata"] == {
                "applicable_count": 1,
                "option_count": 1,
                "shooter_count": 1,
                "target_count": 1,
            }
            assert model["guided_fire"]["options"] == [{
                "capability": "GUIDED_FIRE",
                "guider_display_name": "fixture-20 20",
                "guider_id": 20,
                "maximum_range": 20,
                "profile_id": None,
                "shooter_display_name": "fixture-10 10",
                "shooter_id": 10,
                "target_id": 30,
                "weapon_display_name": "重型导弹",
                "weapon_id": 73,
            }]
    finally:
        fixture.close()


def test_invalid_affiliation_does_not_expose_guided_capability(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path, bundle=_bundle(affiliation_id=None))
    try:
        with running_server(fixture.server) as address:
            model = _capabilities(address, 20)
            assert model["guided_fire"]["options"] == []
            assert all(
                value["capability_type"] != "GUIDED_FIRE"
                for value in model["capabilities"]
            )
    finally:
        fixture.close()


def test_missing_guider_contact_does_not_expose_guided_capability(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path, bundle=_bundle(
        shooter_hex=114,
        guider_hex=101,
        target_concealed=True,
    ))
    try:
        with running_server(fixture.server) as address:
            model = _capabilities(address, 20)
            assert model["guided_fire"]["options"] == []
    finally:
        fixture.close()


def test_human_guided_preview_executes_the_exact_engine_fire_action(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path, bundle=_bundle(target_concealed=True))
    try:
        with running_server(fixture.server) as address:
            bootstrap = _bootstrap(address)
            status, payload, _ = request(
                address,
                "POST",
                "/api/product/preview",
                {
                    "token": "red-token-123",
                    "request_id": "guided-ui-preview",
                    "observation_version": bootstrap["observation_version"],
                    "actor_ids": [20],
                    "operation_type": "GUIDED_FIRE",
                    "fire_mode": "GUIDED",
                    "guider_id": 20,
                    "shooter_id": 10,
                    "weapon_id": 73,
                    "target_id": 30,
                },
            )
            assert status == 200, payload
            preview = decoded(payload)
            assert preview["can_execute"] is True
            assert preview["per_unit_results"][0]["human_readable_summary"] == (
                "引导单位20为发射平台10引导射击目标30，武器73"
            )

            status, payload, _ = request(
                address,
                "POST",
                "/api/product/execute",
                {
                    "token": "red-token-123",
                    "preview_token": preview["preview_token"],
                    "request_id": "guided-ui-execute",
                },
            )
            assert status == 202, payload
            receipt = decoded(payload)
            assert receipt["accepted"] is True
            event = fixture.session.command_events[0]
            assert event.status is CommandStatus.PENDING
            assert event.decision.operations[0].preview_arguments() == {
                "actor_ids": (20,),
                "operation_type": "GUIDED_FIRE",
                "destination_hex": None,
                "target_id": 30,
                "movement_mode": "NORMAL",
                "weapon_choices": {},
                "whole_side_wait": False,
                "fire_mode": "GUIDED",
                "guider_id": 20,
                "shooter_id": 10,
                "weapon_id": 73,
            }

            tick = fixture.session.advance_tick()
            assert tick.red_command_sequences == (0,)
            assert fixture.session.command_events[0].status is CommandStatus.APPLIED
            _seat, view = fixture.session.controller_snapshot(
                fixture.reset.red_binding
            )
            assert view.source_frame["actions"] == [{
                "action_type": "FIRE",
                "actor_id": 20,
                "parameters": {
                    "fire_mode": "GUIDED",
                    "guider_id": 20,
                    "shooter_id": 10,
                    "weapon_id": 73,
                },
                "side": "red",
                "target_id": 30,
                "target_position": None,
            }]
    finally:
        fixture.close()


def test_legacy_shoot_preview_remains_direct_without_guided_fields(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path, bundle=_bundle(target_concealed=False))
    try:
        with running_server(fixture.server) as address:
            bootstrap = _bootstrap(address)
            model = _capabilities(address, 10)
            direct = next(
                value for value in model["capabilities"]
                if value["capability_type"] == "DIRECT_FIRE"
            )
            assert direct["operation_type"] == "SHOOT"
            status, payload, _ = request(
                address,
                "POST",
                "/api/product/preview",
                {
                    "token": "red-token-123",
                    "request_id": "legacy-direct-preview",
                    "observation_version": bootstrap["observation_version"],
                    "actor_ids": [10],
                    "operation_type": "SHOOT",
                    "target_id": 30,
                    "weapon_choices": {"10": 73},
                },
            )
            assert status == 200, payload
            assert decoded(payload)["can_execute"] is True
    finally:
        fixture.close()


def test_product_api_cannot_forge_a_guided_engine_capability(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path, bundle=_bundle(target_concealed=True))
    try:
        with running_server(fixture.server) as address:
            bootstrap = _bootstrap(address)
            status, payload, _ = request(
                address,
                "POST",
                "/api/product/preview",
                {
                    "token": "red-token-123",
                    "request_id": "forged-guided-preview",
                    "observation_version": bootstrap["observation_version"],
                    "actor_ids": [20],
                    "operation_type": "GUIDED_FIRE",
                    "fire_mode": "GUIDED",
                    "guider_id": 20,
                    "shooter_id": 999,
                    "weapon_id": 73,
                    "target_id": 30,
                },
            )
            assert status == 200, payload
            preview = decoded(payload)
            assert preview["can_execute"] is False
            assert preview["per_unit_results"][0]["reject_code"] == (
                "guided_fire_capability_unavailable"
            )
            assert fixture.session.command_events == ()
    finally:
        fixture.close()


def test_frontend_guided_flow_uses_capability_fields_not_weapon_inference() -> None:
    source = (STATIC / "dual_control.js").read_text(encoding="utf-8")
    capability_source = (STATIC / "capability_ui.js").read_text(
        encoding="utf-8"
    )

    assert 'GUIDED_FIRE: "引导射击"' in source
    assert 'fire_mode: "GUIDED"' in source
    assert "guider_id: selectedIds()[0]" in source
    assert "shooter_id: selectedGuidedPlatform.shooter_id" in source
    assert "weapon_id: selectedGuidedPlatform.weapon_id" in source
    assert '"GUIDED_FIRE"' in capability_source
    assert "weapon_id === 73" not in source
    assert "weaponId === 73" not in source
