from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from wargame_native_engine.data import (
    DataProvenance,
    ScenarioBundle,
    ScenarioCatalog,
)
from wargame_native_engine.gateway import CommandGatewayError
from wargame_native_engine.operations import (
    ProductOperationService,
    WargameFacade,
)


SCENARIO = Path("examples/source_movement_scenario.json")


@pytest.fixture
def product_match(tmp_path: Path):
    bundle = ScenarioBundle.from_path(SCENARIO)
    app = WargameFacade(
        ScenarioCatalog.from_bundles({
            bundle.scenario_identity.scenario_id: bundle,
        }),
        storage_root=tmp_path / "sessions",
    )
    match = app.create_match(
        scenario_id=bundle.scenario_identity.scenario_id,
        red_controller_id="product-red",
        blue_controller_id="product-blue",
        seed=42,
        decision_interval_ms=10_000,
        frame_interval_ms=10_000,
    )
    try:
        yield match
    finally:
        try:
            match.close()
        except KeyError:
            pass


def test_group_move_preview_is_server_planned_and_hides_action_ids(
    product_match,
) -> None:
    red = product_match.bind_controller("red", controller_id="product-red")
    service = ProductOperationService(red)
    before = product_match.status

    preview = service.preview_operation(
        [1],
        "MOVE",
        request_id="preview-move",
        destination_hex=105,
    )

    assert preview.executable_count == 1
    assert preview.rejected_count == 0
    assert preview.per_unit_results[0].path == (102, 103, 104, 105)
    assert preview.per_unit_results[0].estimated_duration_ms == 80_000
    assert "action_id" not in str(preview.to_record()).lower()
    assert "proposal_id" not in str(preview.to_record()).lower()
    assert product_match.status == before


def test_partial_preview_requires_confirmation_and_only_valid_actor_executes(
    product_match,
) -> None:
    red = product_match.bind_controller("red", controller_id="product-red")
    blue = product_match.bind_controller("blue", controller_id="product-blue")
    service = ProductOperationService(red)
    preview = service.preview_operation(
        [1, 2],
        "MOVE",
        request_id="partial-move",
        destination_hex=105,
    )

    assert preview.selected_count == 2
    assert preview.executable_count == 1
    assert preview.rejected_count == 1
    assert preview.partial_confirmation_required is True
    rejected = next(
        item for item in preview.per_unit_results
        if not item.accepted_for_preview
    )
    assert rejected.unit_id == 2
    assert rejected.reject_code == "unit_not_controllable"
    with pytest.raises(
        CommandGatewayError, match="partial_confirmation_required"
    ):
        service.execute_preview(
            preview.preview_token,
            request_id="execute-partial",
        )
    assert product_match.status.frame_index == 0

    result = service.execute_preview(
        preview.preview_token,
        request_id="execute-partial",
        confirm_partial=True,
    )
    assert result.product_state == "WAITING_OPPONENT"
    assert result.executed_count == 1
    assert result.skipped_count == 1
    advanced = blue.submit_batch(request_id="blue-empty")
    assert advanced.advanced is True
    assert product_match.status.frame_index == 1
    step = product_match.gateway.manager._managed(  # noqa: SLF001
        product_match.session_id
    ).environment.last_step_record
    assert len(step["ordered_actions"]) == 1
    assert step["ordered_actions"][0]["actor_id"] == 1
    audit = product_match.gateway.control_audit(
        product_match.session_id, "product-red"
    )
    preview_audit = next(
        item for item in audit
        if item["event"] == "product_operation_previewed"
        and item["preview_token"] == preview.preview_token
    )
    assert preview_audit["rejected"] == [{
        "unit_id": 2,
        "reject_code": "unit_not_controllable",
        "reject_message": "unit 2 is not a friendly unit in RED",
    }]


def test_execute_is_idempotent_even_after_product_status_refresh(product_match) -> None:
    red = product_match.bind_controller("red", controller_id="product-red")
    blue = product_match.bind_controller("blue", controller_id="product-blue")
    service = ProductOperationService(red)
    preview = service.preview_operation(
        [1], "WAIT", request_id="preview-wait"
    )
    first = service.execute_preview(
        preview.preview_token, request_id="execute-once"
    )
    assert first.product_state == "WAITING_OPPONENT"
    blue.submit_batch(request_id="blue-empty")
    assert service.status()["product_state"] == "WAITING_SELECTION"
    repeated = service.execute_preview(
        preview.preview_token, request_id="execute-once"
    )
    assert repeated == first
    assert product_match.status.frame_index == 1
    with pytest.raises(CommandGatewayError, match="preview_not_found"):
        service.execute_preview(
            preview.preview_token, request_id="another-execution"
        )


def test_preview_request_conflict_cross_side_and_expiration(product_match) -> None:
    red = ProductOperationService(product_match.bind_controller(
        "red", controller_id="product-red"
    ))
    blue_controller = product_match.bind_controller(
        "blue", controller_id="product-blue"
    )
    blue = ProductOperationService(blue_controller)
    preview = red.preview_operation([1], "WAIT", request_id="same-request")
    with pytest.raises(CommandGatewayError, match="request_id_conflict"):
        red.preview_operation(
            [1], "MOVE", request_id="same-request", destination_hex=105
        )
    with pytest.raises(CommandGatewayError, match="preview_not_found"):
        blue.preview(preview.preview_token)
    red.execute_preview(preview.preview_token, request_id="red-submit")
    blue_wait = blue.preview_operation(
        [], "WAIT", request_id="blue-wait", whole_side_wait=True
    )
    assert blue_wait.can_execute is True
    blue.execute_preview(blue_wait.preview_token, request_id="blue-submit")
    with pytest.raises(CommandGatewayError, match="preview_not_found"):
        red.preview(preview.preview_token)


def test_new_selection_replaces_older_unexecuted_actor_preview(
    product_match,
) -> None:
    red = ProductOperationService(product_match.bind_controller(
        "red", controller_id="product-red"
    ))
    first = red.preview_operation(
        [1], "MOVE", request_id="first-choice", destination_hex=104
    )
    second = red.preview_operation(
        [1], "MOVE", request_id="second-choice", destination_hex=105
    )

    with pytest.raises(CommandGatewayError, match="preview_not_found"):
        red.execute_preview(first.preview_token, request_id="execute-old")
    with pytest.raises(CommandGatewayError, match="preview_superseded"):
        red.preview_operation(
            [1], "MOVE", request_id="first-choice", destination_hex=104
        )
    result = red.execute_preview(second.preview_token, request_id="execute-new")
    assert result.executed_count == 1


def test_zero_executable_preview_cannot_execute(product_match) -> None:
    red = ProductOperationService(product_match.bind_controller(
        "red", controller_id="product-red"
    ))
    preview = red.preview_operation(
        [2], "STOP_MOVE", request_id="invalid-stop"
    )
    assert preview.executable_count == 0
    assert preview.can_execute is False
    with pytest.raises(CommandGatewayError, match="preview_not_executable"):
        red.execute_preview(preview.preview_token, request_id="execute-invalid")
    assert product_match.status.frame_index == 0


def test_shoot_preview_uses_frozen_server_target_after_movement(product_match) -> None:
    red_controller = product_match.bind_controller(
        "red", controller_id="product-red"
    )
    blue_controller = product_match.bind_controller(
        "blue", controller_id="product-blue"
    )
    red = ProductOperationService(red_controller)
    blue = ProductOperationService(blue_controller)
    move = red.preview_operation(
        [1], "MOVE", request_id="move-near", destination_hex=105
    )
    red.execute_preview(move.preview_token, request_id="move-submit")
    blue_wait = blue.preview_operation(
        [], "WAIT", request_id="blue-wait-0", whole_side_wait=True
    )
    blue.execute_preview(blue_wait.preview_token, request_id="blue-submit-0")

    for index in range(1, 22):
        if any(
            item["action"]["action_type"] == "SHOOT"
            for item in red_controller.observe().valid_actions
        ):
            break
        rw = red.preview_operation(
            [], "WAIT", request_id=f"red-wait-{index}", whole_side_wait=True
        )
        bw = blue.preview_operation(
            [], "WAIT", request_id=f"blue-wait-{index}", whole_side_wait=True
        )
        red.execute_preview(rw.preview_token, request_id=f"red-submit-{index}")
        blue.execute_preview(bw.preview_token, request_id=f"blue-submit-{index}")
    shoot = red.preview_operation(
        [1], "SHOOT", request_id="shoot-preview", target_id=2
    )
    assert shoot.executable_count == 1
    result = shoot.per_unit_results[0]
    assert result.accepted_for_preview is True
    assert result.weapon_options == ({
        "weapon_id": 36,
        "target_id": 2,
        "summary": "武器36",
    },)
    assert "action_id" not in str(shoot.to_record()).lower()


def test_five_unit_move_preview_keeps_independent_server_paths(
    tmp_path: Path,
) -> None:
    record = json.loads(SCENARIO.read_text(encoding="utf-8"))
    source = record["operators"][0]
    for unit_id in (3, 4, 5, 6):
        unit = deepcopy(source)
        unit["operator_id"] = unit_id
        unit["subtype"] = f"fixture-vehicle-{unit_id}"
        record["operators"].append(unit)
        record["deployment"]["positions"]["red"][str(unit_id)] = [1, 1]
    record["scenario_id"] = "five-unit-product-preview"
    bundle = ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256="fixture-five-unit",
            source_kind="test_fixture",
        ),
    )
    app = WargameFacade(
        ScenarioCatalog.from_bundles({
            bundle.scenario_identity.scenario_id: bundle,
        }),
        storage_root=tmp_path / "sessions",
    )
    match = app.create_match(
        scenario_id=bundle.scenario_identity.scenario_id,
        red_controller_id="five-red",
        blue_controller_id="five-blue",
        seed=42,
        decision_interval_ms=10_000,
        frame_interval_ms=10_000,
    )
    try:
        red = ProductOperationService(match.bind_controller(
            "red", controller_id="five-red"
        ))
        blue = ProductOperationService(match.bind_controller(
            "blue", controller_id="five-blue"
        ))
        unit_ids = [1, 3, 4, 5, 6]
        preview = red.preview_operation(
            unit_ids,
            "MOVE",
            request_id="five-preview",
            destination_hex=105,
        )
        assert preview.selected_count == 5
        assert preview.executable_count == 5
        assert preview.rejected_count == 0
        assert {
            item.unit_id for item in preview.per_unit_results
        } == set(unit_ids)
        assert all(
            item.path == (102, 103, 104, 105)
            for item in preview.per_unit_results
        )

        public = preview.to_record()
        public["per_unit_results"][0]["path"][:] = [999]
        red.execute_preview(preview.preview_token, request_id="five-submit")
        wait = blue.preview_operation(
            [], "WAIT", request_id="five-blue-wait", whole_side_wait=True
        )
        result = blue.execute_preview(
            wait.preview_token, request_id="five-blue-submit"
        )
        assert result.advanced is True
        step = match.gateway.manager._managed(  # noqa: SLF001
            match.session_id
        ).environment.last_step_record
        actions = step["ordered_actions"]
        assert [item["actor_id"] for item in actions] == unit_ids
        assert all(
            item["parameters"]["path"] == [102, 103, 104, 105]
            for item in actions
        )
    finally:
        match.close()


def test_two_unit_shoot_preview_matches_only_frozen_server_actions(
    tmp_path: Path,
) -> None:
    record = json.loads(SCENARIO.read_text(encoding="utf-8"))
    red_source = record["operators"][0]
    red_source["current_hex"] = 105
    red_source["x"] = 5
    record["deployment"]["positions"]["red"]["1"] = [5, 1]
    second = deepcopy(red_source)
    second["operator_id"] = 3
    second["subtype"] = "fixture-second-shooter"
    record["operators"].append(second)
    record["deployment"]["positions"]["red"]["3"] = [5, 1]
    record["scenario_id"] = "two-unit-product-fire"
    bundle = ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256="fixture-two-shooters",
            source_kind="test_fixture",
        ),
    )
    app = WargameFacade(
        ScenarioCatalog.from_bundles({
            bundle.scenario_identity.scenario_id: bundle,
        }),
        storage_root=tmp_path / "sessions",
    )
    match = app.create_match(
        scenario_id=bundle.scenario_identity.scenario_id,
        red_controller_id="shoot-red",
        blue_controller_id="shoot-blue",
        seed=42,
        decision_interval_ms=1_000,
        frame_interval_ms=1_000,
    )
    try:
        red = ProductOperationService(match.bind_controller(
            "red", controller_id="shoot-red"
        ))
        blue = ProductOperationService(match.bind_controller(
            "blue", controller_id="shoot-blue"
        ))
        if red.capabilities([1, 3])["operations"]["SHOOT"][
            "applicable_count"
        ] == 0:
            red_wait = red.preview_operation(
                [], "WAIT", request_id="visibility-red", whole_side_wait=True
            )
            blue_wait = blue.preview_operation(
                [], "WAIT", request_id="visibility-blue", whole_side_wait=True
            )
            red.execute_preview(red_wait.preview_token, request_id="visibility-r")
            blue.execute_preview(blue_wait.preview_token, request_id="visibility-b")

        preview = red.preview_operation(
            [1, 3], "SHOOT", request_id="two-shooters", target_id=2
        )
        assert preview.executable_count == 2
        assert preview.rejected_count == 0
        assert all(
            item.weapon_options == ({
                "weapon_id": 36,
                "target_id": 2,
                "summary": "武器36",
            },)
            for item in preview.per_unit_results
        )
        red.execute_preview(preview.preview_token, request_id="fire-red")
        blue_wait = blue.preview_operation(
            [], "WAIT", request_id="fire-blue-wait", whole_side_wait=True
        )
        result = blue.execute_preview(
            blue_wait.preview_token, request_id="fire-blue"
        )
        assert result.advanced is True
        step = match.gateway.manager._managed(  # noqa: SLF001
            match.session_id
        ).environment.last_step_record
        assert [
            item["action_type"] for item in step["ordered_actions"]
        ] == ["SHOOT", "SHOOT"]
        assert {
            item["actor_id"] for item in step["ordered_actions"]
        } == {1, 3}
        assert all(item["target_id"] == 2 for item in step["ordered_actions"])
    finally:
        match.close()
