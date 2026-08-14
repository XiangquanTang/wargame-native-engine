from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.run_minimal_source_replay import _template
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.gateway import (
    CommandGateway,
    CommandGatewayError,
    SessionManager,
)
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)


SCENARIO_PATH = Path("examples/source_movement_scenario.json")


class EnvironmentFactory:
    def __init__(self) -> None:
        self.environments: list[SourceCompatibleEnvironment] = []

    def __call__(self) -> SourceCompatibleEnvironment:
        scenario = load_scenario(SCENARIO_PATH)
        environment = SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(scenario)),
            template=_template(scenario),
        )
        original = environment.session.step
        environment.session.step = Mock(wraps=original)  # type: ignore[method-assign]
        self.environments.append(environment)
        return environment


@pytest.fixture
def gateway(tmp_path: Path):
    factory = EnvironmentFactory()
    manager = SessionManager(factory, storage_root=tmp_path / "managed")
    value = CommandGateway(manager, delta_time_seconds=20)
    descriptor = value.create_session(
        red_controller_id="red-controller",
        blue_controller_id="blue-controller",
        seed=42,
    )
    return value, factory, descriptor.session_id


def test_server_planned_multicell_action_is_window_authorized_and_executed(
    gateway,
) -> None:
    value, factory, session_id = gateway

    proposal = value.plan_move(
        session_id,
        "red-controller",
        request_id="plan-red",
        expected_window=0,
        unit_id=1,
        destination_hex=106,
    )

    assert proposal.reachable is True
    assert len(proposal.ordered_path) >= 5
    assert proposal.action_id is not None
    red_action_ids = {
        item["action_id"]
        for item in value.view(session_id, "red-controller").valid_actions
    }
    assert proposal.action_id in red_action_ids

    red = value.submit_batch(
        session_id,
        "red-controller",
        request_id="submit-red",
        expected_window=0,
        ordered_action_ids=[proposal.action_id],
    )
    blue = value.submit_batch(
        session_id,
        "blue-controller",
        request_id="submit-blue",
        expected_window=0,
        ordered_action_ids=[],
    )

    assert red.advanced is False
    assert blue.advanced is True
    assert factory.environments[0].session.step.call_count == 1  # type: ignore[attr-defined]
    recorded = factory.environments[0].last_step_record["ordered_actions"]
    assert len(recorded) == 1
    assert recorded[0]["parameters"]["path"] == list(proposal.ordered_path)


def test_proposal_records_are_detached_and_request_id_is_idempotent(gateway) -> None:
    value, _, session_id = gateway
    first = value.plan_move(
        session_id,
        "red-controller",
        request_id="same-plan",
        expected_window=0,
        unit_id=1,
        destination_hex=105,
    )
    record = first.to_record()
    record["ordered_path"].append(999)  # type: ignore[union-attr]
    record["segment_details"][0]["to_hex"] = 999  # type: ignore[index]

    repeated = value.plan_move(
        session_id,
        "red-controller",
        request_id="same-plan",
        expected_window=0,
        unit_id=1,
        destination_hex=105,
    )
    fetched = value.proposal(session_id, "red-controller", first.proposal_id)

    assert repeated == first
    assert fetched == first
    assert 999 not in fetched.ordered_path
    assert all(item["to_hex"] != 999 for item in fetched.segment_details)
    with pytest.raises(CommandGatewayError, match="proposal_request_conflict"):
        value.plan_move(
            session_id,
            "red-controller",
            request_id="same-plan",
            expected_window=0,
            unit_id=1,
            destination_hex=104,
        )


def test_proposal_is_side_session_and_window_scoped(gateway, tmp_path: Path) -> None:
    value, factory, session_id = gateway
    proposal = value.plan_move(
        session_id,
        "red-controller",
        request_id="scoped",
        expected_window=0,
        unit_id=1,
        destination_hex=105,
    )
    assert proposal.action_id is not None

    with pytest.raises(CommandGatewayError, match="proposal_side_mismatch"):
        value.proposal(session_id, "blue-controller", proposal.proposal_id)
    with pytest.raises(CommandGatewayError, match="not a current BLUE action"):
        value.submit_batch(
            session_id,
            "blue-controller",
            request_id="wrong-side-submit",
            expected_window=0,
            ordered_action_ids=[proposal.action_id],
        )
    assert factory.environments[0].session.step.call_count == 0  # type: ignore[attr-defined]

    value.submit_batch(
        session_id,
        "red-controller",
        request_id="close-red",
        expected_window=0,
        ordered_action_ids=[],
    )
    value.submit_batch(
        session_id,
        "blue-controller",
        request_id="close-blue",
        expected_window=0,
        ordered_action_ids=[],
    )
    with pytest.raises(CommandGatewayError, match="proposal_not_found"):
        value.proposal(session_id, "red-controller", proposal.proposal_id)
    with pytest.raises(CommandGatewayError, match="not a current RED action"):
        value.submit_batch(
            session_id,
            "red-controller",
            request_id="expired-action",
            expected_window=1,
            ordered_action_ids=[proposal.action_id],
        )

    second_factory = EnvironmentFactory()
    second = CommandGateway(
        SessionManager(second_factory, storage_root=tmp_path / "second"),
        delta_time_seconds=20,
    )
    second_id = second.create_session(
        red_controller_id="red-two",
        blue_controller_id="blue-two",
        seed=42,
    ).session_id
    with pytest.raises(CommandGatewayError, match="proposal_not_found"):
        second.proposal(second_id, "red-two", proposal.proposal_id)


def test_unreachable_plan_does_not_create_an_authorized_action(gateway) -> None:
    value, _, session_id = gateway
    proposal = value.plan_move(
        session_id,
        "red-controller",
        request_id="unreachable",
        expected_window=0,
        unit_id=1,
        destination_hex=999999,
    )

    assert proposal.reachable is False
    assert proposal.action_id is None
    assert proposal.blocked_reason == "UNKNOWN_DESTINATION"


def test_actor_without_frozen_move_capability_cannot_plan(gateway) -> None:
    value, factory, session_id = gateway

    with pytest.raises(CommandGatewayError, match="move_plan_rejected"):
        value.plan_move(
            session_id,
            "red-controller",
            request_id="enemy",
            expected_window=0,
            unit_id=2,
            destination_hex=105,
        )
    assert factory.environments[0].session.step.call_count == 0  # type: ignore[attr-defined]
