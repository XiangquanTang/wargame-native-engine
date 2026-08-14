from __future__ import annotations

from pathlib import Path

import pytest

from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.gateway import CommandGatewayError
from wargame_native_engine.operations import WargameFacade


SCENARIO_PATH = Path("examples/source_movement_scenario.json")


@pytest.fixture
def match(tmp_path: Path):
    bundle = ScenarioBundle.from_path(SCENARIO_PATH)
    facade = WargameFacade(
        ScenarioCatalog.from_bundles({
            bundle.scenario_identity.scenario_id: bundle,
        }),
        storage_root=tmp_path / "matches",
    )
    return facade.create_match(
        scenario_id=bundle.scenario_identity.scenario_id,
        red_controller_id="red-human",
        blue_controller_id="blue-human",
        seed=42,
        decision_interval_ms=20_000,
        frame_interval_ms=20_000,
    )


def test_facade_hides_engine_internals_and_binds_side_identity(match) -> None:
    red = match.bind_controller("RED", controller_id="red-human")
    blue = match.bind_controller("blue", controller_id="blue-human")

    assert red.observe().perspective == "RED"
    assert blue.observe().perspective == "BLUE"
    assert not hasattr(red, "world_state")
    assert not hasattr(red, "engine")
    assert match.map_record["map_identity"]["sha256"] == (
        match.bundle.map_identity.sha256
    )
    with pytest.raises(PermissionError):
        match.bind_controller("RED", controller_id="blue-human")


def test_unit_command_menu_is_friendly_side_only_and_exact(match) -> None:
    red = match.bind_controller("RED", controller_id="red-human")
    menu = red.get_unit_commands(1)

    assert menu.perspective == "RED"
    assert menu.unit["operator_id"] == 1
    assert menu.move_planning_available is True
    assert "MOVE" in menu.available
    assert menu.unavailable["SHOOT"] == "not_in_current_frozen_valid_actions"
    assert all(
        item["action"]["actor_id"] == 1
        for group in menu.available.values()
        for item in group
    )
    with pytest.raises(CommandGatewayError, match="unit_not_controllable"):
        red.get_unit_commands(2)


def test_stage_replaces_same_actor_and_submission_advances_once(match) -> None:
    red = match.bind_controller("RED", controller_id="red-human")
    blue = match.bind_controller("BLUE", controller_id="blue-human")
    menu = red.get_unit_commands(1)
    waits = menu.available["WAIT"]
    move = menu.available["MOVE"][0]

    red.stage_action(waits[0]["action_id"])
    staged = red.stage_action(move["action_id"])
    assert len(staged) == 1
    assert staged[0].action_type == "MOVE"
    assert red.get_unit_commands(1).staged_action_id == move["action_id"]

    red_result = red.submit_batch(request_id="red-batch", expected_window=0)
    blue_result = blue.submit_batch(request_id="blue-empty", expected_window=0)
    assert red_result.advanced is False
    assert blue_result.advanced is True
    assert red.staged_actions == ()
    assert red.observe().step_index == 1


def test_facade_destination_proposal_is_staged_by_action_id_only(match) -> None:
    red = match.bind_controller("RED", controller_id="red-human")
    blue = match.bind_controller("BLUE", controller_id="blue-human")
    proposal = red.plan_move(
        1,
        106,
        request_id="route-to-106",
    )

    assert proposal.reachable is True
    assert proposal.action_id is not None
    assert len(proposal.ordered_path) >= 5
    red.stage_action(proposal.action_id)
    red.submit_batch(request_id="red-route", expected_window=0)
    result = blue.submit_batch(request_id="blue-noop", expected_window=0)

    assert result.advanced is True
    advance = match.gateway.last_advance(match.session_id, "red-human")
    assert advance is not None
    closed = next(
        item
        for item in match.gateway.control_audit(match.session_id, "red-human")
        if item["event"] == "window_closed"
    )
    assert closed["merged_action_ids"] == [proposal.action_id]


def test_staging_external_records_does_not_change_authorized_action(match) -> None:
    red = match.bind_controller("RED", controller_id="red-human")
    proposal = red.plan_move(1, 105, request_id="immutable")
    assert proposal.action_id is not None
    record = proposal.to_record()
    record["ordered_path"].clear()  # type: ignore[union-attr]
    red.stage_action(proposal.action_id)

    fetched = red.proposal(proposal.proposal_id)
    assert fetched.ordered_path == proposal.ordered_path
    assert fetched.ordered_path
