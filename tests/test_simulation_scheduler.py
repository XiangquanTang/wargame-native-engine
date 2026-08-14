from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.run_minimal_source_replay import _template
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.core.action import ActionType
from wargame_native_engine.gateway import (
    CommandGateway,
    SessionDescriptor,
    SessionManager,
    SimulationSchedule,
    SimulationScheduler,
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


def test_ten_second_decision_generates_ten_one_second_fact_frames(
    tmp_path: Path,
) -> None:
    factory = EnvironmentFactory()
    manager = SessionManager(factory, storage_root=tmp_path / "managed")
    gateway = CommandGateway(
        manager,
        decision_interval_ms=10_000,
        frame_interval_ms=1_000,
    )
    descriptor = gateway.create_session(
        red_controller_id="red-controller",
        blue_controller_id="blue-controller",
        seed=42,
    )
    session_id = descriptor.session_id
    blue_move = next(
        item for item in gateway.view(
            session_id, "blue-controller"
        ).valid_actions
        if item["action"]["action_type"] == ActionType.MOVE.value
    )

    gateway.submit_batch(
        session_id,
        "blue-controller",
        request_id="blue-window-0",
        expected_window=0,
        ordered_action_ids=[blue_move["action_id"]],
    )
    completed = gateway.submit_batch(
        session_id,
        "red-controller",
        request_id="red-window-0",
        expected_window=0,
        ordered_action_ids=[],
    )

    factual = manager.descriptor(session_id)
    assert completed.advanced is True
    assert completed.view.step_index == 1
    assert completed.view.frame_index == 10
    assert completed.view.sim_time_ms == 10_000
    assert factual.step_index == factual.frame_index == 10
    assert factual.sim_time_ms == 10_000
    assert gateway.window(
        session_id, "red-controller"
    ).decision_window_index == 1
    assert factory.environments[0].session.step.call_count == 10  # type: ignore[attr-defined]
    blue = factory.environments[0].session.snapshot()["world"]["operators"]["2"]
    assert blue["move_path"] == [105]
    assert blue["segment_elapsed_ms"] == 10_000

    output = tmp_path / "published"
    publication = gateway.publish_session(
        session_id,
        "red-controller",
        str(output),
        truncated=True,
    )
    archive = SourceReplayArchiveImporter().import_path(publication.replay_path)
    assert archive.frame_names == tuple(f"{index}.json" for index in range(11))
    assert len(archive.steps) == 10
    assert archive.steps[0]["ordered_actions"] == [blue_move["action"]]
    assert all(step["ordered_actions"] == [] for step in archive.steps[1:])
    assert all(step["delta_time_ms"] == 1_000 for step in archive.steps)


def test_scheduler_stops_immediately_when_terminal_occurs_mid_window() -> None:
    class FakeManager:
        def __init__(self) -> None:
            self.frame = 5
            self.sim_time = 2_000
            self.calls: list[tuple[object, ...]] = []

        def descriptor(self, session_id: str) -> SessionDescriptor:
            del session_id
            return SessionDescriptor(
                "session",
                self.frame,
                self.frame >= 8,
                frame_index=self.frame,
                sim_time_ms=self.sim_time,
            )

        def advance(self, session_id, actions, *, delta_time_seconds):
            del session_id
            self.calls.append(tuple(actions))
            self.frame += 1
            self.sim_time += int(delta_time_seconds * 1000)
            return self.descriptor("session")

    manager = FakeManager()
    scheduler = SimulationScheduler(
        manager, SimulationSchedule(10_000, 1_000)
    )
    result = scheduler.advance_window(
        "session", decision_window_index=3, ordered_actions=(),
    )

    assert result.first_frame_index == 6
    assert result.last_frame_index == 8
    assert result.generated_frame_count == 3
    assert result.sim_time_before_ms == 2_000
    assert result.sim_time_after_ms == 5_000
    assert result.terminal is True
    assert manager.calls == [(), (), ()]


@pytest.mark.parametrize(
    ("decision", "frame"),
    [(True, 1), (0, 1), (10, True), (10, 0), (10, 3)],
)
def test_schedule_requires_positive_divisible_exact_integer_milliseconds(
    decision,
    frame,
) -> None:
    with pytest.raises(ValueError):
        SimulationSchedule(decision, frame)
