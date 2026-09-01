from __future__ import annotations

from pathlib import Path

from tests.test_realtime_dual_side_control import (
    Fixture,
    decoded,
    request,
    running_server,
)
from wargame_native_engine.agents import CommandStatus
from wargame_native_engine.data import ScenarioBundle


SCENARIO = Path("examples/demos/boarding_basic/scenario.json")


def _bootstrap(address: tuple[str, int]) -> dict[str, object]:
    return decoded(request(
        address,
        "GET",
        "/api/product/bootstrap?token=red-token-123",
    )[1])


def _capabilities(
    address: tuple[str, int], actor_id: int,
) -> dict[str, object]:
    status, payload, _content_type = request(
        address,
        "POST",
        "/api/product/capabilities",
        {"token": "red-token-123", "actor_ids": [actor_id]},
    )
    assert status == 200, payload
    return decoded(payload)


def _preview(
    address: tuple[str, int],
    observation_version: int,
    *,
    request_id: str,
    operation_type: str,
    actor_id: int,
    target_id: int,
) -> tuple[int, dict[str, object]]:
    status, payload, _content_type = request(
        address,
        "POST",
        "/api/product/preview",
        {
            "token": "red-token-123",
            "request_id": request_id,
            "observation_version": observation_version,
            "actor_ids": [actor_id],
            "operation_type": operation_type,
            "target_id": target_id,
        },
    )
    return status, decoded(payload)


def _execute(
    address: tuple[str, int], preview_token: str, *, request_id: str,
) -> dict[str, object]:
    status, payload, _content_type = request(
        address,
        "POST",
        "/api/product/execute",
        {
            "token": "red-token-123",
            "preview_token": preview_token,
            "request_id": request_id,
            "confirm_partial": False,
        },
    )
    assert status == 202, payload
    return decoded(payload)


def _boarding_options(
    payload: dict[str, object], operation_type: str,
) -> list[dict[str, object]]:
    boarding = payload["boarding"]
    assert isinstance(boarding, dict)
    options = boarding["options"]
    assert isinstance(options, list)
    return [
        value for value in options
        if isinstance(value, dict)
        and value.get("operation_type") == operation_type
    ]


def test_human_board_and_unload_capabilities_are_engine_projections(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path, bundle=ScenarioBundle.from_path(SCENARIO))
    try:
        with running_server(fixture.server) as address:
            board = _capabilities(address, 101)
            assert _boarding_options(board, "BOARD") == [{
                "operation_type": "BOARD",
                "actor_id": 101,
                "target_id": 102,
                "profile_id": "GROUND_PERSONNEL_SINGLE_V1",
            }]
            assert any(
                value.get("capability_type") == "BOARD"
                for value in board["capabilities"]
            )

            initial = _bootstrap(address)
            status, preview = _preview(
                address,
                int(initial["observation_version"]),
                request_id="boarding-ui-board-preview",
                operation_type="BOARD",
                actor_id=101,
                target_id=102,
            )
            assert status == 200
            receipt = _execute(
                address,
                str(preview["preview_token"]),
                request_id="boarding-ui-board-execute",
            )
            fixture.session.advance_tick()
            assert receipt["accepted"] is True
            assert fixture.session.command_events[-1].status is CommandStatus.APPLIED

            for _ in range(74):
                fixture.session.advance_tick()
            unload = _capabilities(address, 102)
            assert _boarding_options(unload, "UNLOAD") == [{
                "operation_type": "UNLOAD",
                "actor_id": 102,
                "target_id": 101,
                "profile_id": "GROUND_PERSONNEL_SINGLE_V1",
            }]
            assert any(
                value.get("capability_type") == "UNLOAD"
                for value in unload["capabilities"]
            )
    finally:
        fixture.close()


def test_human_forged_board_carrier_is_rejected(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path, bundle=ScenarioBundle.from_path(SCENARIO))
    try:
        with running_server(fixture.server) as address:
            current = _bootstrap(address)
            status, preview = _preview(
                address,
                int(current["observation_version"]),
                request_id="boarding-ui-forged-preview",
                operation_type="BOARD",
                actor_id=101,
                target_id=201,
            )
            assert status == 200
            assert preview["can_execute"] is False
            assert fixture.session.command_events == ()
    finally:
        fixture.close()


def test_human_ui_refreshes_after_complete_unload(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path, bundle=ScenarioBundle.from_path(SCENARIO))
    try:
        with running_server(fixture.server) as address:
            initial = _bootstrap(address)
            _, board = _preview(
                address,
                int(initial["observation_version"]),
                request_id="boarding-ui-refresh-board-preview",
                operation_type="BOARD",
                actor_id=101,
                target_id=102,
            )
            _execute(
                address,
                str(board["preview_token"]),
                request_id="boarding-ui-refresh-board-execute",
            )
            for _ in range(75):
                fixture.session.advance_tick()

            before_unload = _bootstrap(address)
            _, unload = _preview(
                address,
                int(before_unload["observation_version"]),
                request_id="boarding-ui-refresh-unload-preview",
                operation_type="UNLOAD",
                actor_id=102,
                target_id=101,
            )
            _execute(
                address,
                str(unload["preview_token"]),
                request_id="boarding-ui-refresh-unload-execute",
            )
            for _ in range(75):
                fixture.session.advance_tick()

            refreshed = _bootstrap(address)
            assert refreshed["tick_index"] == 150
            seat_view, _controller = fixture.session.controller_snapshot(
                fixture.reset.red_binding
            )
            passenger = next(
                value for value in seat_view.observation["friendly_operators"]
                if value["operator_id"] == 101
            )
            assert passenger["state"] == "ACTIVE"
            assert passenger.get("mounted_on") is None
            assert _boarding_options(_capabilities(address, 101), "BOARD")
    finally:
        fixture.close()


def test_frontend_boarding_path_consumes_server_options_only() -> None:
    capability_source = Path(
        "src/wargame_native_engine/viewer/static/capability_ui.js"
    ).read_text(encoding="utf-8")
    control_source = Path(
        "src/wargame_native_engine/viewer/static/dual_control.js"
    ).read_text(encoding="utf-8")

    assert "model?.boarding?.options" in capability_source
    assert "boardingOptions" in capability_source
    assert 'activeTool === "BOARD" || activeTool === "UNLOAD"' in control_source
    assert "previewOperation(kind, { target_id: targetUnitId })" in control_source
    assert "GROUND_PERSONNEL_SINGLE_V1" not in capability_source
    assert "GROUND_PERSONNEL_SINGLE_V1" not in control_source
