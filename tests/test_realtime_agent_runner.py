from __future__ import annotations

from pathlib import Path
from threading import Event, Thread, enumerate as active_threads
from time import monotonic, sleep

import pytest

from scripts.run_realtime_agent_match import parser, run_smoke

from wargame_native_engine.agents import (
    AgentDecisionContext,
    RealtimeAgentSession,
    RealtimeExecutionMode,
    RealtimeLocalAgentRunner,
    RealtimeShutdownTimeoutError,
    SeatDecision,
    WaitSeatAgent,
)
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.operations import WargameFacade


SCENARIO = Path("examples/source_movement_scenario.json")


class MatchFactory:
    def __init__(self, root: Path, tick_interval_ms: int = 20) -> None:
        self.root = root
        self.tick_interval_ms = tick_interval_ms
        self.bundle = ScenarioBundle.from_path(SCENARIO)

    def __call__(self, seed: int, episode_sequence: int):
        app = WargameFacade(
            ScenarioCatalog.from_bundles({
                self.bundle.scenario_identity.scenario_id: self.bundle,
            }),
            storage_root=self.root / f"managed-{episode_sequence}",
        )
        return app.create_match(
            scenario_id=self.bundle.scenario_identity.scenario_id,
            red_controller_id="runner-red",
            blue_controller_id="runner-blue",
            seed=seed,
            decision_interval_ms=self.tick_interval_ms,
            frame_interval_ms=self.tick_interval_ms,
        )


class ExplodingAgent(WaitSeatAgent):
    def __init__(self) -> None:
        super().__init__("exploding-realtime-agent")
        self.calls = 0

    def act(self, *, context: AgentDecisionContext) -> SeatDecision:
        del context
        self.calls += 1
        raise RuntimeError("agent-inference-failed")


class BlockingAgent(WaitSeatAgent):
    def __init__(self, entered: Event, release: Event) -> None:
        super().__init__("blocking-realtime-agent")
        self.entered = entered
        self.release = release

    def act(self, *, context: AgentDecisionContext) -> SeatDecision:
        del context
        self.entered.set()
        assert self.release.wait(3)
        return SeatDecision()


def test_realtime_local_agents_run_independently_and_publish(tmp_path: Path) -> None:
    session = RealtimeAgentSession(
        MatchFactory(tmp_path / "factory"),
        run_namespace="runner-worker-000",
        tick_interval_ms=20,
        execution_mode=RealtimeExecutionMode.BACKGROUND,
    )
    runner = RealtimeLocalAgentRunner(
        session,
        red_agent=WaitSeatAgent("runner-red-wait"),
        blue_agent=WaitSeatAgent("runner-blue-wait"),
    )
    output = tmp_path / "episode"
    try:
        result = runner.run(
            output,
            seed=42,
            max_ticks=3,
            timeout_seconds=5,
        )
        assert result.publication.tick_count == 3
        assert result.publication.truncated is True
        assert result.agent_failures == ()
        assert result.publication.command_path.is_file()
        assert not any(
            thread.name.startswith("wargame-agent-")
            or thread.name.startswith("wargame-realtime-")
            for thread in active_threads()
        )
    finally:
        session.close()


def test_late_agent_result_is_dropped_before_publication_snapshot(
    tmp_path: Path,
) -> None:
    entered = Event()
    release = Event()
    session = RealtimeAgentSession(
        MatchFactory(tmp_path / "factory", tick_interval_ms=20),
        run_namespace="runner-late-worker-000",
        tick_interval_ms=20,
        execution_mode=RealtimeExecutionMode.BACKGROUND,
    )
    runner = RealtimeLocalAgentRunner(
        session,
        red_agent=BlockingAgent(entered, release),
        blue_agent=WaitSeatAgent("late-test-blue"),
    )
    results = []
    failures = []

    def run() -> None:
        try:
            results.append(runner.run(
                tmp_path / "episode",
                seed=42,
                max_ticks=1,
                timeout_seconds=3,
                shutdown_timeout_seconds=2,
            ))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = Thread(target=run)
    try:
        thread.start()
        assert entered.wait(1)
        deadline = monotonic() + 2
        while session.current_tick < 1 and monotonic() < deadline:
            sleep(0.005)
        assert session.current_tick == 1
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert failures == []
        assert len(results) == 1
        assert not any(event.seat == "red" for event in session.command_events)
        assert all(
            event.status.name != "PENDING" for event in session.command_events
        )
        assert not any(
            value.name.startswith("wargame-agent-")
            or value.name.startswith("wargame-realtime-")
            for value in active_threads()
        )
    finally:
        release.set()
        thread.join(5)
        session.close(timeout_seconds=2)


def test_runner_reports_agent_worker_shutdown_timeout_without_publishing(
    tmp_path: Path,
) -> None:
    entered = Event()
    release = Event()
    session = RealtimeAgentSession(
        MatchFactory(tmp_path / "factory", tick_interval_ms=20),
        run_namespace="runner-timeout-worker-000",
        tick_interval_ms=20,
        execution_mode=RealtimeExecutionMode.BACKGROUND,
    )
    runner = RealtimeLocalAgentRunner(
        session,
        red_agent=BlockingAgent(entered, release),
        blue_agent=WaitSeatAgent("timeout-test-blue"),
    )
    output = tmp_path / "episode"
    try:
        assert entered.is_set() is False
        with pytest.raises(RealtimeShutdownTimeoutError) as captured:
            runner.run(
                output,
                seed=42,
                max_ticks=1,
                timeout_seconds=3,
                shutdown_timeout_seconds=0.02,
            )
        assert entered.is_set()
        assert captured.value.phase == "agent_worker"
        assert not output.exists()
        release.set()
        deadline = monotonic() + 2
        while any(
            value.name.startswith("wargame-agent-")
            for value in active_threads()
        ) and monotonic() < deadline:
            sleep(0.01)
        assert not any(
            value.name.startswith("wargame-agent-")
            for value in active_threads()
        )
    finally:
        release.set()
        session.close(timeout_seconds=2)


def test_one_agent_exception_does_not_stop_other_seat_or_ticks(
    tmp_path: Path,
) -> None:
    red = ExplodingAgent()
    session = RealtimeAgentSession(
        MatchFactory(tmp_path / "factory"),
        run_namespace="runner-failure-worker-000",
        tick_interval_ms=20,
        execution_mode=RealtimeExecutionMode.BACKGROUND,
    )
    runner = RealtimeLocalAgentRunner(
        session,
        red_agent=red,
        blue_agent=WaitSeatAgent("healthy-blue-agent"),
    )
    try:
        result = runner.run(
            tmp_path / "episode",
            seed=42,
            max_ticks=2,
            timeout_seconds=5,
        )
        assert result.publication.tick_count == 2
        assert result.agent_failures
        assert {value.seat for value in result.agent_failures} == {"red"}
        assert red.calls >= 1
        assert any(
            event.seat == "blue" for event in session.command_events
        )
    finally:
        session.close()


def test_realtime_cli_smoke_reports_complete_package(tmp_path: Path) -> None:
    output = tmp_path / "cli-episode"
    args = parser().parse_args([
        "--scenario-bundle", str(SCENARIO),
        "--output-dir", str(output),
        "--run-namespace", "cli-worker-000",
        "--tick-interval-ms", "20",
        "--max-ticks", "2",
    ])
    report = run_smoke(args)
    assert report["run_namespace"] == "cli-worker-000"
    assert report["episode_id"].startswith("native-realtime-")
    assert report["tick_count"] == 2
    assert report["fact_frame_count"] == 3
    assert report["truncated"] is True
    assert output.joinpath("seat_commands.native.jsonl").is_file()
    assert output.joinpath("realtime_ticks.native.jsonl").is_file()
