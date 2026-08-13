from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
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
        environment.session.step = Mock(  # type: ignore[method-assign]
            wraps=original
        )
        self.environments.append(environment)
        return environment


@pytest.fixture
def gateway(tmp_path: Path):
    factory = EnvironmentFactory()
    manager = SessionManager(factory, storage_root=tmp_path / "managed")
    value = CommandGateway(manager, delta_time_seconds=20)
    return value, factory


def _create(gateway: CommandGateway):
    return gateway.create_session(
        red_controller_id="red-controller",
        blue_controller_id="blue-controller",
        seed=42,
    )


def test_controller_views_are_identity_bound_detached_and_side_only(
    gateway,
) -> None:
    value, factory = gateway
    descriptor = _create(value)

    red = value.view(descriptor.session_id, "red-controller")
    blue = value.view(descriptor.session_id, "blue-controller")

    assert red.perspective == "RED"
    assert blue.perspective == "BLUE"
    assert red.observation["side"] == "red"
    assert blue.observation["side"] == "blue"
    assert "green" not in red.to_record()
    assert "frame_analysis" not in red.to_record()
    assert red.source_frame["extra"]["native"]["perspective"] == "RED"
    assert blue.source_frame["extra"]["native"]["perspective"] == "BLUE"
    assert "judge_info" not in red.observation
    assert [item["operator_id"] for item in red.observation["friendly_operators"]] == [1]
    assert [item["operator_id"] for item in blue.observation["friendly_operators"]] == [2]
    red.observation["mission"]["mutated"] = True
    assert "mutated" not in value.view(
        descriptor.session_id, "red-controller"
    ).observation["mission"]
    assert factory.environments[0].session.observe("blue").side == "blue"
    with pytest.raises(CommandGatewayError, match="controller_not_bound"):
        value.view(descriptor.session_id, "intruder")


def test_joint_submission_advances_once_in_red_blue_order_and_is_idempotent(
    gateway,
) -> None:
    value, factory = gateway
    descriptor = _create(value)
    session_id = descriptor.session_id

    red_pending = value.submit(
        session_id,
        "red-controller",
        request_id="red-0",
        expected_step=0,
        valid_action_id=None,
    )
    repeated_red = value.submit(
        session_id,
        "red-controller",
        request_id="red-0",
        expected_step=0,
        valid_action_id=None,
    )
    blue_advance = value.submit(
        session_id,
        "blue-controller",
        request_id="blue-0",
        expected_step=0,
        valid_action_id=None,
    )
    repeated_blue = value.submit(
        session_id,
        "blue-controller",
        request_id="blue-0",
        expected_step=0,
        valid_action_id=None,
    )

    assert red_pending == repeated_red
    assert red_pending.advanced is False
    assert red_pending.pending_perspectives == ("BLUE",)
    assert blue_advance == repeated_blue
    assert blue_advance.advanced is True
    assert blue_advance.view.step_index == 1
    assert factory.environments[0].session.step.call_count == 1  # type: ignore[attr-defined]
    assert factory.environments[0].last_step_record["ordered_actions"] == []
    repeated_red.view.observation["mission"]["caller_mutation"] = True
    detached_retry = value.submit(
        session_id,
        "red-controller",
        request_id="red-0",
        expected_step=0,
        valid_action_id=None,
    )
    assert "caller_mutation" not in detached_retry.view.observation["mission"]


def test_stale_step_request_conflict_and_side_reuse_are_rejected(
    gateway,
) -> None:
    value, _ = gateway
    session_id = _create(value).session_id
    value.submit(
        session_id, "red-controller",
        request_id="red", expected_step=0, valid_action_id=None,
    )
    with pytest.raises(CommandGatewayError, match="request_id_conflict"):
        value.submit(
            session_id, "red-controller",
            request_id="red", expected_step=0, valid_action_id="different",
        )
    with pytest.raises(CommandGatewayError, match="side_already_submitted"):
        value.submit(
            session_id, "red-controller",
            request_id="red-second", expected_step=0, valid_action_id=None,
        )
    value.submit(
        session_id, "blue-controller",
        request_id="blue", expected_step=0, valid_action_id=None,
    )
    with pytest.raises(CommandGatewayError, match="stale_expected_step"):
        value.submit(
            session_id, "red-controller",
            request_id="stale", expected_step=0, valid_action_id=None,
        )


def test_valid_action_identity_is_resolved_only_for_bound_side(gateway) -> None:
    value, factory = gateway
    session_id = _create(value).session_id
    blue_action = value.view(
        session_id, "blue-controller"
    ).valid_actions[0]["action_id"]

    with pytest.raises(CommandGatewayError, match="not a current RED action"):
        value.submit(
            session_id,
            "red-controller",
            request_id="wrong-side",
            expected_step=0,
            valid_action_id=blue_action,
        )
    assert factory.environments[0].session.step.call_count == 0  # type: ignore[attr-defined]


def test_concurrent_duplicate_side_submission_cannot_double_write(gateway) -> None:
    value, factory = gateway
    session_id = _create(value).session_id

    def submit(request_id: str):
        try:
            return value.submit(
                session_id,
                "red-controller",
                request_id=request_id,
                expected_step=0,
                valid_action_id=None,
            )
        except CommandGatewayError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(submit, ("one", "two")))

    assert sum(getattr(item, "accepted", False) for item in results) == 1
    assert "side_already_submitted" in results
    assert factory.environments[0].session.step.call_count == 0  # type: ignore[attr-defined]


def test_sessions_isolate_state_seed_recorder_and_step_index(gateway) -> None:
    value, factory = gateway
    first = _create(value)
    second = value.create_session(
        red_controller_id="red-two",
        blue_controller_id="blue-two",
        seed=43,
    )

    value.submit(
        first.session_id, "red-controller",
        request_id="r", expected_step=0, valid_action_id=None,
    )
    value.submit(
        first.session_id, "blue-controller",
        request_id="b", expected_step=0, valid_action_id=None,
    )

    assert value.view(first.session_id, "red-controller").step_index == 1
    assert value.view(second.session_id, "red-two").step_index == 0
    assert factory.environments[0].session.seed == 42
    assert factory.environments[1].session.seed == 43
    assert factory.environments[1].session.step.call_count == 0  # type: ignore[attr-defined]


def test_truncated_publication_uses_recorder_and_live_analysis(gateway, tmp_path: Path) -> None:
    value, _ = gateway
    session_id = _create(value).session_id
    value.submit(
        session_id, "red-controller",
        request_id="r", expected_step=0, valid_action_id=None,
    )
    value.submit(
        session_id, "blue-controller",
        request_id="b", expected_step=0, valid_action_id=None,
    )
    output = tmp_path / "published"

    publication = value.publish_session(
        session_id,
        "red-controller",
        str(output),
        truncated=True,
    )
    archive = SourceReplayArchiveImporter().import_path(publication.replay_path)

    assert publication.frame_count == 2
    assert publication.step_count == 1
    assert publication.terminal is False
    assert publication.truncated is True
    assert archive.frame_names == ("0.json", "1.json")
    assert len(publication.analysis_path.read_text(encoding="utf-8").splitlines()) == 2
    assert archive.manifest["truncated"] is True
    assert "GREEN" not in value.view(session_id, "red-controller").to_record()
    assert value.manager.descriptor(session_id).finalized is True
    with pytest.raises(CommandGatewayError, match="session_finalized"):
        value.submit(
            session_id, "red-controller",
            request_id="after-publication", expected_step=1,
            valid_action_id=None,
        )
