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
from wargame_native_engine.io.replay import replay_source_archive
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


def test_stop_weapon_state_and_wait_follow_current_frozen_actions(
    product_match,
) -> None:
    red = ProductOperationService(product_match.bind_controller(
        "red", controller_id="product-red"
    ))
    blue = ProductOperationService(product_match.bind_controller(
        "blue", controller_id="product-blue"
    ))
    capabilities = red.capabilities([1])
    assert capabilities["operations"]["WEAPON_LOCK"]["applicable_count"] == 1
    assert capabilities["operations"]["WEAPON_UNFOLD"]["applicable_count"] == 0
    lock = red.preview_operation(
        [1], "WEAPON_LOCK", request_id="lock-preview"
    )
    assert lock.executable_count == 1
    assert lock.per_unit_results[0].human_readable_summary == "锁定武器"

    move = red.preview_operation(
        [1], "MOVE", request_id="move-before-stop", destination_hex=105
    )
    red.execute_preview(move.preview_token, request_id="move-before-stop-submit")
    wait = blue.preview_operation(
        [], "WAIT", request_id="blue-wait-stop", whole_side_wait=True
    )
    blue.execute_preview(wait.preview_token, request_id="blue-wait-stop-submit")
    stop = red.preview_operation(
        [1], "STOP_MOVE", request_id="stop-preview"
    )
    assert stop.executable_count == 1
    assert stop.per_unit_results[0].human_readable_summary == "停止当前移动"


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
        [1], "SHOOT", request_id="shoot-preview", target_id=2,
        weapon_choices={1: 36},
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
            [1, 3], "SHOOT", request_id="two-shooters", target_id=2,
            weapon_choices={1: 36, 3: 36},
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


@pytest.mark.parametrize("weapon_id", [36, 43])
def test_explicit_carried_weapon_intent_reaches_engine_and_shared_pool_updates(
    tmp_path: Path, weapon_id: int,
) -> None:
    record = json.loads(SCENARIO.read_text(encoding="utf-8"))
    red_operator = record["operators"][0]
    red_operator.update({"x": 5, "current_hex": 105, "weapon_ids": [36, 43]})
    record["deployment"]["positions"]["red"]["1"] = [5, 1]
    record["metadata"]["source_compatible"]["weapons"]["43"] = {
        "weapon_class": "vehicle_light_weapon",
        "maximum_range": 10,
        "target_domains": ["PERSONNEL"],
        "ammunition_key": "0",
        "requires_unfolded": True,
        "can_fire_while_moving": False,
    }
    record["scenario_id"] = f"explicit-weapon-{weapon_id}"
    bundle = ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256=f"explicit-weapon-{weapon_id}",
            source_kind="test_fixture",
        ),
    )
    app = WargameFacade(
        ScenarioCatalog.from_bundles({record["scenario_id"]: bundle}),
        storage_root=tmp_path / f"sessions-{weapon_id}",
    )
    match = app.create_match(
        scenario_id=record["scenario_id"],
        red_controller_id="weapon-red",
        blue_controller_id="weapon-blue",
        seed=42,
        decision_interval_ms=1_000,
        frame_interval_ms=1_000,
    )
    try:
        red_controller = match.bind_controller("red", controller_id="weapon-red")
        red = ProductOperationService(red_controller)
        blue = match.bind_controller("blue", controller_id="weapon-blue")
        before = red_controller.observe().own_weapon_inventory[0]
        assert before["ammunition_pools"] == {"0": 20}
        assert [item["ammunition"] for item in before["weapons"]] == [20, 20]
        assert all(
            item["shared_with_weapon_ids"] == [36, 43]
            for item in before["weapons"]
        )

        preview = red.preview_operation(
            [1],
            "SHOOT",
            request_id=f"preview-{weapon_id}",
            target_id=2,
            weapon_choices={1: weapon_id},
        )
        assert preview.executable_count == 1
        assert f"武器{weapon_id}" in preview.per_unit_results[0].human_readable_summary
        red.execute_preview(preview.preview_token, request_id=f"submit-{weapon_id}")
        blue.submit_batch(request_id=f"blue-{weapon_id}")

        result = match.gateway.last_action_results(
            match.session_id, "weapon-red",
        )
        assert len(result) == 1
        assert result[0]["accepted"] is True
        assert result[0]["weapon_id"] == weapon_id
        after = red_controller.observe().own_weapon_inventory[0]
        assert after["ammunition_pools"] == {"0": 19}
        assert [item["ammunition"] for item in after["weapons"]] == [19, 19]
    finally:
        match.close()


def test_currently_illegal_carried_weapon_is_rejected_only_by_engine(
    tmp_path: Path,
) -> None:
    record = json.loads(SCENARIO.read_text(encoding="utf-8"))
    record["operators"][0]["raw_attributes"]["ammunition"]["0"] = 0
    record["operators"][0].update({"x": 5, "current_hex": 105})
    record["deployment"]["positions"]["red"]["1"] = [5, 1]
    record["scenario_id"] = "empty-ammunition-intent"
    bundle = ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256="empty-ammunition-intent",
            source_kind="test_fixture",
        ),
    )
    app = WargameFacade(
        ScenarioCatalog.from_bundles({record["scenario_id"]: bundle}),
        storage_root=tmp_path / "empty-ammunition-sessions",
    )
    match = app.create_match(
        scenario_id=record["scenario_id"],
        red_controller_id="empty-red",
        blue_controller_id="empty-blue",
        seed=42,
        decision_interval_ms=1_000,
        frame_interval_ms=1_000,
    )
    try:
        red_controller = match.bind_controller("red", controller_id="empty-red")
        red = ProductOperationService(red_controller)
        blue = match.bind_controller("blue", controller_id="empty-blue")
        assert not any(
            item["action"]["action_type"] == "SHOOT"
            for item in red_controller.observe().valid_actions
        )
        preview = red.preview_operation(
            [1], "SHOOT", request_id="empty-preview", target_id=2,
            weapon_choices={1: 36},
        )
        assert preview.executable_count == 1
        red.execute_preview(preview.preview_token, request_id="empty-submit")
        blue.submit_batch(request_id="empty-blue-submit")

        result = match.gateway.last_action_results(match.session_id, "empty-red")
        assert result == ({
            "action_id": result[0]["action_id"],
            "side": "red",
            "actor_id": 1,
            "action_type": "SHOOT",
            "weapon_id": 36,
            "accepted": False,
            "code": "AMMUNITION_UNAVAILABLE",
        },)
        assert red_controller.observe().own_weapon_inventory[0][
            "ammunition_pools"
        ] == {"0": 0}

        forged = red.preview_operation(
            [1], "SHOOT", request_id="forged-preview", target_id=2,
            weapon_choices={1: 999},
        )
        assert forged.executable_count == 0
        assert forged.per_unit_results[0].reject_code == "weapon_not_carried"
        step = match.gateway.manager._managed(  # noqa: SLF001
            match.session_id
        ).environment.last_step_record
        assert [
            value["parameters"]["weapon_id"]
            for value in step["ordered_actions"]
            if value["action_type"] == "SHOOT"
        ] == [36]
        publication = match.publish(
            tmp_path / "empty-ammunition-replay", truncated=True,
        )
        assert replay_source_archive(
            bundle.scenario, publication.replay_path, strict=True,
        ).matches is True
    finally:
        match.close()
