from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_minimal_source_replay import _template
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.gateway import (
    CommandGateway,
    CommandGatewayError,
    DeadlineMode,
    SessionManager,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import file_sha256
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)


SCENARIO_PATH = Path("examples/source_movement_scenario.json")


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value
        self.calls = 0

    def now(self) -> float:
        self.calls += 1
        return self.value


class ExplodingClock:
    def now(self) -> float:
        raise AssertionError("synchronous scheduling must not read wall clock")


def _gateway(tmp_path: Path, **options):
    scenario = load_scenario(SCENARIO_PATH)

    def factory() -> SourceCompatibleEnvironment:
        return SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(scenario)),
            template=_template(scenario),
        )

    manager = SessionManager(factory, storage_root=tmp_path / "facts")
    gateway = CommandGateway(manager, **options)
    descriptor = gateway.create_session(
        red_controller_id="red-controller",
        blue_controller_id="blue-controller",
        seed=42,
    )
    return gateway, manager, scenario, descriptor.session_id


def test_synchronous_policy_never_reads_clock_and_requires_explicit_batches(
    tmp_path: Path,
) -> None:
    gateway, manager, _, session_id = _gateway(
        tmp_path, clock=ExplodingClock(),
    )

    gateway.submit_batch(
        session_id,
        "red-controller",
        request_id="red",
        expected_window=0,
        ordered_action_ids=[],
    )
    assert manager.descriptor(session_id).frame_index == 0
    with pytest.raises(CommandGatewayError, match="deadline_not_enabled"):
        gateway.expire_window(session_id)
    gateway.submit_batch(
        session_id,
        "blue-controller",
        request_id="blue",
        expected_window=0,
        ordered_action_ids=[],
    )
    assert manager.descriptor(session_id).frame_index == 1


def test_wall_deadline_accepts_before_deadline_and_times_out_missing_side(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    gateway, manager, _, session_id = _gateway(
        tmp_path,
        deadline_mode=DeadlineMode.SERVER_MONOTONIC,
        deadline_seconds=10,
        clock=clock,
    )
    view = gateway.window(session_id, "red-controller")
    assert view.opened_at_server_monotonic == 100.0
    assert view.deadline_at_server_monotonic == 110.0

    clock.value = 109.999
    gateway.submit_batch(
        session_id,
        "red-controller",
        request_id="red-before-deadline",
        expected_window=0,
        ordered_action_ids=[],
    )
    assert manager.descriptor(session_id).frame_index == 0
    clock.value = 110.0
    result = gateway.expire_window(session_id)

    assert result.generated_frame_count == 1
    assert manager.descriptor(session_id).frame_index == 1
    records = gateway.control_audit(session_id, "red-controller")
    timeout = [record for record in records if record["event"] == "submission_timeout"]
    assert len(timeout) == 1
    assert timeout[0]["side"] == "blue"
    assert timeout[0]["ordered_action_ids"] == []
    closed = [record for record in records if record["event"] == "window_closed"]
    assert closed[0]["timeout"] is True
    assert closed[0]["close_reason"] == "deadline"


def test_late_submission_is_rejected_after_timeout_advance(tmp_path: Path) -> None:
    clock = FakeClock()
    gateway, manager, _, session_id = _gateway(
        tmp_path,
        deadline_mode="server_monotonic",
        deadline_seconds=5,
        clock=clock,
    )
    clock.value = 105.0

    with pytest.raises(CommandGatewayError, match="decision_window_expired"):
        gateway.submit_batch(
            session_id,
            "blue-controller",
            request_id="late-blue",
            expected_window=0,
            ordered_action_ids=[],
        )

    assert manager.descriptor(session_id).frame_index == 1
    assert gateway.window(
        session_id, "blue-controller"
    ).decision_window_index == 1
    rejected = [
        record for record in gateway.control_audit(
            session_id, "blue-controller"
        )
        if record["event"] == "submission_rejected"
    ]
    assert rejected[-1]["reject_reason"] == "decision_window_expired"


def test_stale_request_never_expires_a_different_current_window(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    gateway, manager, _, session_id = _gateway(
        tmp_path,
        deadline_mode="server_monotonic",
        deadline_seconds=5,
        clock=clock,
    )
    for side in ("red", "blue"):
        gateway.submit_batch(
            session_id,
            f"{side}-controller",
            request_id=f"first-{side}",
            expected_window=0,
            ordered_action_ids=[],
        )
    assert manager.descriptor(session_id).frame_index == 1
    clock.value = 200.0

    with pytest.raises(CommandGatewayError, match="stale_expected_step"):
        gateway.submit_batch(
            session_id,
            "red-controller",
            request_id="stale-old-window",
            expected_window=0,
            ordered_action_ids=[],
        )

    assert manager.descriptor(session_id).frame_index == 1
    assert gateway.window(
        session_id, "red-controller"
    ).decision_window_index == 1


def test_control_audit_is_outside_fact_archive_and_strict_replay(
    tmp_path: Path,
) -> None:
    gateway, _, scenario, session_id = _gateway(tmp_path)
    window_index = 0
    while not gateway.manager.descriptor(session_id).terminal:
        for side in ("red", "blue"):
            gateway.submit_batch(
                session_id,
                f"{side}-controller",
                request_id=f"{side}-{window_index}",
                expected_window=window_index,
                ordered_action_ids=[],
            )
        window_index += 1
    publication = gateway.publish_session(
        session_id,
        "red-controller",
        str(tmp_path / "published"),
        truncated=False,
    )
    assert publication.control_audit_path is not None
    audit_path = publication.control_audit_path
    records = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["event"] == "window_opened"
    assert {record["event"] for record in records} >= {
        "submission_accepted", "window_closed", "publication_requested",
    }
    replay_hash = file_sha256(publication.replay_path)
    assert replay_source_archive(
        scenario, publication.replay_path, strict=True,
    ).matches

    audit_path.write_text('{"diagnostic":"changed"}\n', encoding="utf-8")
    assert file_sha256(publication.replay_path) == replay_hash
    assert replay_source_archive(
        scenario, publication.replay_path, strict=True,
    ).matches
    audit_path.unlink()
    assert file_sha256(publication.replay_path) == replay_hash
    assert replay_source_archive(
        scenario, publication.replay_path, strict=True,
    ).matches


@pytest.mark.parametrize("deadline", [None, True, 0, -1, float("nan")])
def test_wall_deadline_requires_positive_finite_duration(
    tmp_path: Path, deadline,
) -> None:
    with pytest.raises(ValueError, match="deadline_seconds"):
        _gateway(
            tmp_path,
            deadline_mode="server_monotonic",
            deadline_seconds=deadline,
            clock=FakeClock(),
        )
