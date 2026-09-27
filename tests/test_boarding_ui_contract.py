from __future__ import annotations

from pathlib import Path

from tests.test_realtime_dual_side_control import (
    Fixture,
    MatchFactory,
    decoded,
    request,
    running_server,
)
from tests.test_boarding_contract import _type_derived_scenario_record
from wargame_native_engine.agents import (
    CommandStatus,
    ManualRealtimeClock,
    RealtimeAgentSession,
    SeatOperation,
)
from wargame_native_engine.data import DataProvenance, ScenarioBundle
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.viewer.human_capabilities import (
    build_human_capabilities,
)
from wargame_native_engine.viewer.live_control import controller_view_record


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


def test_type_derived_board_unload_service_path_without_socket(
    tmp_path: Path,
) -> None:
    record = _type_derived_scenario_record()
    # Keep the adjacent destination free so the carrier can move before the
    # unload projection is checked.
    operators = record["operators"]
    map_record = record["map"]
    deployment = record["deployment"]
    assert isinstance(operators, list)
    assert isinstance(map_record, dict)
    assert isinstance(deployment, dict)
    enemy = next(value for value in operators if value["operator_id"] == 3)
    enemy.update({"current_hex": 103, "x": 3, "y": 1})
    deployment["positions"]["blue"]["3"] = [3, 1]
    map_record["hexes"][1]["neighbors"][0] = 103
    map_record["hexes"].append({
        "pos": 103,
        "elev": 0,
        "cond": 0,
        "neighbors": [-1, -1, -1, 102, -1, -1],
        "roads": [0, 0, 0, 0, 0, 0],
        "rivers": [0, 0, 0, 0, 0, 0],
    })
    bundle = ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256=canonical_hash(record),
            source_kind="project_authored_type_boarding_ui_test",
        ),
    )
    session = RealtimeAgentSession(
        MatchFactory(tmp_path, bundle=bundle),
        run_namespace="type-derived-boarding-service-test",
        tick_interval_ms=1_000,
        clock=ManualRealtimeClock(),
    )
    reset = session.reset(seed=42)
    try:
        seat, view, summary = session.operation_capabilities_snapshot(
            reset.red_binding, [1]
        )
        human = build_human_capabilities(summary, view, [1])
        assert _boarding_options(human, "BOARD") == [{
            "operation_type": "BOARD",
            "actor_id": 1,
            "target_id": 2,
            "profile_id": "GROUND_PERSONNEL_SINGLE_V1",
        }]
        board = SeatOperation("BOARD", (1,), target_id=2)
        preview = session.preview_operation(
            reset.red_binding,
            board,
            observation_version=seat.observation_version,
            request_id="type-board-preview",
        )
        assert preview.can_execute is True
        receipt = session.submit_operation(
            reset.red_binding,
            observation_version=seat.observation_version,
            operation=board,
        )
        assert receipt.accepted is True
        session.advance_tick()
        _seat, transitioning_view = session.controller_snapshot(
            reset.red_binding
        )
        transitioning_units = {
            value["operator_id"]: value
            for value in controller_view_record(
                transitioning_view
            )["frame"]["viewer_units"]
        }
        assert transitioning_units[1]["boarding_state"] == "BOARDING"
        assert transitioning_units[1]["map_visible"] is True
        assert "transport_display_count" not in transitioning_units[2]
        for _ in range(74):
            session.advance_tick()
        assert session.command_events[receipt.command_sequence].status is (
            CommandStatus.APPLIED
        )

        _seat, boarded_view = session.controller_snapshot(reset.red_binding)
        boarded_units = {
            value["operator_id"]: value
            for value in controller_view_record(
                boarded_view
            )["frame"]["viewer_units"]
        }
        assert boarded_units[1]["boarding_state"] == "ON_BOARD"
        assert boarded_units[1]["map_visible"] is False
        assert boarded_units[1]["current_hex"] is None
        assert boarded_units[1]["row"] is None
        assert boarded_units[1]["column"] is None
        assert boarded_units[2]["transport_display_count"] == 2
        assert boarded_units[2]["carried_units"] == [{
            "operator_id": 1,
            "name": "fixture-1",
            "icon_key": "infantry",
            "alive": True,
            "destroyed": False,
            "boarding_state": "ON_BOARD",
        }]

        _blue_seat, blue_view = session.controller_snapshot(reset.blue_binding)
        blue_units = controller_view_record(blue_view)["frame"]["viewer_units"]
        assert all(
            "transport_display_count" not in value
            for value in blue_units
            if value["side"] == "red"
        )

        move = SeatOperation("MOVE", (2,), destination_hex=102)
        move_receipt = session.submit_operation(
            reset.red_binding,
            observation_version=_seat.observation_version,
            operation=move,
        )
        assert move_receipt.accepted is True
        for _ in range(20):
            session.advance_tick()
        _seat, moved_view = session.controller_snapshot(reset.red_binding)
        moved_units = {
            value["operator_id"]: value
            for value in controller_view_record(
                moved_view
            )["frame"]["viewer_units"]
        }
        assert moved_units[2]["current_hex"] == 102
        assert moved_units[2]["transport_display_count"] == 2
        assert moved_units[1]["map_visible"] is False
        # Movement arrival enters the existing 75-second stop transition.
        # Occupancy remains projected while the carrier becomes unload-ready.
        for _ in range(75):
            session.advance_tick()

        seat, view, summary = session.operation_capabilities_snapshot(
            reset.red_binding, [2]
        )
        human = build_human_capabilities(summary, view, [2])
        assert _boarding_options(human, "UNLOAD") == [{
            "operation_type": "UNLOAD",
            "actor_id": 2,
            "target_id": 1,
            "profile_id": "GROUND_PERSONNEL_SINGLE_V1",
        }]
        unload = SeatOperation("UNLOAD", (2,), target_id=1)
        receipt = session.submit_operation(
            reset.red_binding,
            observation_version=seat.observation_version,
            operation=unload,
        )
        assert receipt.accepted is True
        session.advance_tick()
        _seat, unloading_view = session.controller_snapshot(reset.red_binding)
        unloading_units = {
            value["operator_id"]: value
            for value in controller_view_record(
                unloading_view
            )["frame"]["viewer_units"]
        }
        assert unloading_units[1]["boarding_state"] == "UNLOADING"
        assert unloading_units[1]["map_visible"] is False
        assert unloading_units[2]["transport_display_count"] == 2
        for _ in range(74):
            session.advance_tick()
        _seat, final_view = session.controller_snapshot(reset.red_binding)
        passenger = next(
            value for value in final_view.observation["friendly_operators"]
            if value["operator_id"] == 1
        )
        assert passenger["state"] == "ACTIVE"
        assert passenger.get("mounted_on") is None
        final_units = {
            value["operator_id"]: value
            for value in controller_view_record(
                final_view
            )["frame"]["viewer_units"]
        }
        assert final_units[1]["map_visible"] is True
        assert final_units[1]["current_hex"] == 102
        assert final_units[1]["row"] == 1
        assert final_units[1]["column"] == 2
        assert "transport_display_count" not in final_units[2]
        assert "carried_units" not in final_units[2]

        reset_again = session.reset(seed=42)
        _seat, reset_view = session.controller_snapshot(
            reset_again.red_binding
        )
        reset_units = {
            value["operator_id"]: value
            for value in controller_view_record(
                reset_view
            )["frame"]["viewer_units"]
        }
        assert reset_units[1]["map_visible"] is True
        assert reset_units[1]["current_hex"] == 101
        assert "transport_display_count" not in reset_units[2]
    finally:
        session.close()
