from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
from typing import Callable
from zipfile import ZipFile

import pytest

from wargame_native_engine.agents import (
    AgentDecisionContext,
    LocalSeatMatchError,
    LocalSeatMatchRunner,
    ScriptedSeatAgent,
    SeatActionCapabilities,
    SeatDecision,
    SeatAgentExecutionError,
    SeatOperation,
    SeatOperationError,
    WaitSeatAgent,
)
from wargame_native_engine.analysis import EightFactorAnalyzer
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import (
    source_frame_template_for_scenario,
)
from wargame_native_engine.data import (
    DataProvenance,
    EXPLICIT_NATIVE_SUPPLEMENT,
    ScenarioBundle,
    ScenarioCatalog,
    convert_source_scenario,
    publish_source_scenario_conversion,
)
from wargame_native_engine.gateway import (
    CommandGateway,
    CommandGatewayError,
    SessionManager,
)
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.operations import MatchHandle, WargameFacade
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)


SCENARIO = Path("examples/source_movement_scenario.json")
SNAPSHOT_ROOT = Path(os.environ.get(
    "WARGAME_SOURCE_SNAPSHOT_ROOT",
    "local_data/source_snapshot",
))
SOURCE_SCENARIO_123 = SNAPSHOT_ROOT / "scenarios/2201010105.json"
SOURCE_MAP_123 = SNAPSHOT_ROOT / "maps/123/basic.json"
SUPPLEMENT_123 = Path(
    "config/source_scenario_conversion/scenarios/"
    "2201010105.native-acceptance-supplement.json"
)


class RecordingSeatAgent:
    def __init__(
        self,
        agent_id: str,
        decision: SeatDecision | Callable[[AgentDecisionContext], SeatDecision],
        call_log: list[str] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.decision = decision
        self.call_log = call_log if call_log is not None else []
        self.reset_contexts = []
        self.contexts: list[AgentDecisionContext] = []
        self.closed = False

    def reset(self, context) -> None:
        self.call_log.append(f"reset:{context.side}")
        self.reset_contexts.append(deepcopy(context))

    def act(self, *, context: AgentDecisionContext) -> SeatDecision:
        self.call_log.append(f"act:{context.side}:{context.decision_index}")
        self.contexts.append(deepcopy(context))
        if callable(self.decision):
            return self.decision(context)
        return self.decision

    def close(self) -> None:
        self.closed = True


class FailingSeatAgent(RecordingSeatAgent):
    def __init__(self, agent_id: str, message: str, call_log=None) -> None:
        super().__init__(agent_id, SeatDecision(), call_log)
        self.message = message

    def act(self, *, context: AgentDecisionContext) -> SeatDecision:
        self.call_log.append(f"act:{context.side}:{context.decision_index}")
        self.contexts.append(deepcopy(context))
        raise LookupError(self.message)


def _facade_match(
    tmp_path: Path,
    *,
    bundle: ScenarioBundle | None = None,
    decision_interval_ms: int = 10_000,
    frame_interval_ms: int = 1_000,
) -> MatchHandle:
    active = bundle or ScenarioBundle.from_path(SCENARIO)
    app = WargameFacade(
        ScenarioCatalog.from_bundles({
            active.scenario_identity.scenario_id: active,
        }),
        storage_root=tmp_path / "managed",
    )
    return app.create_match(
        scenario_id=active.scenario_identity.scenario_id,
        red_controller_id="seat-red",
        blue_controller_id="seat-blue",
        seed=42,
        decision_interval_ms=decision_interval_ms,
        frame_interval_ms=frame_interval_ms,
    )


def _run_wait_match(tmp_path: Path, name: str, windows: int = 2):
    match = _facade_match(tmp_path / name)
    red = RecordingSeatAgent(f"{name}-red", SeatDecision())
    blue = RecordingSeatAgent(f"{name}-blue", SeatDecision())
    try:
        result = LocalSeatMatchRunner(
            match,
            run_namespace="test-local-seat-determinism",
            red_agent=red,
            blue_agent=blue,
        ).run(
            tmp_path / f"{name}-output",
            max_decision_windows=windows,
        )
    finally:
        match.close()
    return result, red.contexts, blue.contexts


def test_product_move_wait_chain_uses_one_agent_call_per_side_and_window(
    tmp_path: Path,
) -> None:
    call_log: list[str] = []
    red = RecordingSeatAgent(
        "red-script",
        SeatDecision.single(SeatOperation("MOVE", (1,), destination_hex=105)),
        call_log,
    )
    blue = RecordingSeatAgent("blue-wait", SeatDecision(), call_log)
    match = _facade_match(tmp_path)
    try:
        result = LocalSeatMatchRunner(
            match, run_namespace="test-local-seat", red_agent=red, blue_agent=blue
        ).run(tmp_path / "output", max_decision_windows=1)
        audit = match.gateway.control_audit(match.session_id, "seat-red")
    finally:
        match.close()

    assert call_log == [
        "reset:red", "reset:blue", "act:red:0", "act:blue:0",
    ]
    assert result.decision_window_count == 1
    assert result.fact_frame_count == 11
    assert result.step_count == 10
    assert result.truncated is True
    assert result.strict_replay_match is True
    assert result.online_offline_analysis_match is True
    assert red.closed is blue.closed is True
    assert [
        item["operation_type"]
        for item in audit
        if item["event"] == "product_operation_previewed"
    ] == ["MOVE"]
    archive = SourceReplayArchiveImporter().import_path(result.replay_path)
    assert archive.steps[0]["ordered_actions"][0]["parameters"]["path"] == [
        102, 103, 104, 105,
    ]
    assert all(not step["ordered_actions"] for step in archive.steps[1:])
    assert len(result.analysis_path.read_text(encoding="utf-8").splitlines()) == (
        result.fact_frame_count
    )
    assert result.summary_path.is_file()
    assert result.transition_path.is_file()
    transitions = [
        json.loads(line)
        for line in result.transition_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["side"] for item in transitions] == ["red", "blue"]
    assert all(item["truncated"] is True for item in transitions)
    assert result.transition_count == 2
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary["decision_window_count"] == 1
    assert summary["fact_frame_count"] == 11
    assert summary["step_count"] == 10
    assert summary["transition_count"] == 2
    assert summary["final_state_hash"] == result.final_state_hash
    with pytest.raises(FrozenInstanceError):
        result.fact_frame_count = 99  # type: ignore[misc]


def test_agent_context_is_side_only_detached_and_mutation_cannot_pollute_session(
    tmp_path: Path,
) -> None:
    observed_blue_ids: list[int] = []

    def mutate_red(context: AgentDecisionContext) -> SeatDecision:
        friendly = context.observation["friendly_operators"]
        friendly[0]["operator_id"] = 999999  # type: ignore[index]
        context.operation_capabilities.operation_summary[
            "selected_units"
        ].clear()  # type: ignore[union-attr]
        return SeatDecision()

    def observe_blue(context: AgentDecisionContext) -> SeatDecision:
        observed_blue_ids.append(
            context.observation["friendly_operators"][0]["operator_id"]
        )
        return SeatDecision()

    red = RecordingSeatAgent("mutating-red", mutate_red)
    blue = RecordingSeatAgent("observing-blue", observe_blue)
    match = _facade_match(
        tmp_path, decision_interval_ms=1_000, frame_interval_ms=1_000
    )
    try:
        result = LocalSeatMatchRunner(
            match, run_namespace="test-local-seat", red_agent=red, blue_agent=blue
        ).run(tmp_path / "output", max_decision_windows=1)
    finally:
        match.close()

    assert red.contexts[0].side == "red"
    assert blue.contexts[0].side == "blue"
    assert red.contexts[0].perspective == "RED"
    assert blue.contexts[0].perspective == "BLUE"
    assert red.contexts[0].observation["visible_enemies"] == []
    assert blue.contexts[0].observation["visible_enemies"] == []
    assert {
        item["operator_id"]
        for item in red.contexts[0].observation["friendly_operators"]
    } == {1}
    assert {
        item["operator_id"]
        for item in blue.contexts[0].observation["friendly_operators"]
    } == {2}
    assert observed_blue_ids == [2]
    assert not hasattr(red.contexts[0], "green_frame")
    assert not hasattr(red.contexts[0], "world_state")
    assert not hasattr(red.contexts[0], "frame_analysis")
    archive = SourceReplayArchiveImporter().import_path(result.replay_path)
    assert all(item.obj_id != 999999 for item in archive.frames[0].operators)
    assert all(item.obj_id != 999999 for item in archive.frames[-1].operators)


def test_invalid_product_operation_is_not_silently_waited_or_advanced(
    tmp_path: Path,
) -> None:
    red = RecordingSeatAgent(
        "invalid-red", SeatDecision.single(SeatOperation("STOP_MOVE", (1,)))
    )
    blue = RecordingSeatAgent("blue", SeatDecision())
    match = _facade_match(tmp_path)
    output = tmp_path / "output"
    try:
        with pytest.raises(SeatOperationError) as captured:
            LocalSeatMatchRunner(
                match, run_namespace="test-local-seat", red_agent=red, blue_agent=blue
            ).run(output, max_decision_windows=1)
        assert match.status.frame_index == 0
        assert match.gateway.window(
            match.session_id, "seat-red"
        ).pending_perspectives == ("RED", "BLUE")
    finally:
        match.close()
    assert captured.value.operation_error_code == "preview_not_executable"
    assert len(red.contexts) == 1
    assert len(blue.contexts) == 1
    assert not output.exists()


def test_blue_agent_failure_occurs_before_any_submission_or_advance(
    tmp_path: Path,
) -> None:
    call_log: list[str] = []
    red = RecordingSeatAgent("red", SeatDecision(), call_log)
    blue = FailingSeatAgent("blue", "blue failed", call_log)
    match = _facade_match(tmp_path)
    try:
        with pytest.raises(SeatAgentExecutionError) as captured:
            LocalSeatMatchRunner(
                match, run_namespace="test-local-seat", red_agent=red, blue_agent=blue
            ).run(tmp_path / "output", max_decision_windows=1)
        window = match.gateway.window(match.session_id, "seat-red")
        assert match.status.frame_index == 0
    finally:
        match.close()
    assert captured.value.side == "blue"
    assert captured.value.cause_type == "LookupError"
    assert captured.value.submitted_sides == ()
    assert window.submitted is False
    assert window.pending_perspectives == ("RED", "BLUE")
    assert call_log.count("act:red:0") == 1
    assert call_log.count("act:blue:0") == 1


def test_red_agent_failure_calls_neither_blue_nor_environment_step(
    tmp_path: Path,
) -> None:
    red = FailingSeatAgent("red", "red failed")
    blue = RecordingSeatAgent("blue", SeatDecision())
    match = _facade_match(tmp_path)
    try:
        with pytest.raises(SeatAgentExecutionError) as captured:
            LocalSeatMatchRunner(
                match, run_namespace="test-local-seat", red_agent=red, blue_agent=blue
            ).run(tmp_path / "output", max_decision_windows=1)
        assert match.status.frame_index == 0
    finally:
        match.close()
    assert captured.value.submitted_sides == ()
    assert blue.contexts == []


def test_agent_reset_failure_stops_before_any_decision_or_submission(
    tmp_path: Path,
) -> None:
    class ResetFailAgent(RecordingSeatAgent):
        def reset(self, context) -> None:
            del context
            raise ArithmeticError("reset failed")

    red = ResetFailAgent("reset-red", SeatDecision())
    blue = RecordingSeatAgent("blue", SeatDecision())
    match = _facade_match(tmp_path)
    try:
        with pytest.raises(SeatAgentExecutionError) as captured:
            LocalSeatMatchRunner(
                match, run_namespace="test-local-seat", red_agent=red, blue_agent=blue
            ).run(tmp_path / "output", max_decision_windows=1)
        window = match.gateway.window(match.session_id, "seat-red")
        assert match.status.frame_index == 0
    finally:
        match.close()
    assert captured.value.phase == "reset"
    assert captured.value.cause_type == "ArithmeticError"
    assert window.pending_perspectives == ("RED", "BLUE")
    assert blue.reset_contexts == []
    assert blue.contexts == []


def test_scripted_seat_agent_returns_product_operations_then_explicit_wait() -> None:
    agent = ScriptedSeatAgent(
        "scripted",
        [SeatDecision.single(SeatOperation("MOVE", (1,), destination_hex=105))],
    )
    context = AgentDecisionContext(
        "red",
        0,
        0,
        0,
        {},
        SeatActionCapabilities("red", 0, (), (), (), {}),
    )
    assert agent.act(context=context) == SeatDecision.single(SeatOperation(
        "MOVE", (1,), destination_hex=105
    ))
    assert agent.act(context=context) == SeatDecision()


def test_analysis_failure_after_advance_reports_desynchronization(
    tmp_path: Path,
) -> None:
    bundle = ScenarioBundle.from_path(SCENARIO)

    class FailAfterSetup:
        def __init__(self) -> None:
            self.base = EightFactorAnalyzer()
            self.model_id = self.base.model_id
            self.model_version = self.base.model_version
            self.model_hash = self.base.model_hash
            self.calls = 0

        def analyze_green_frame(self, value):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("analysis failed after committed step")
            return self.base.analyze_green_frame(value)

    def environment_factory() -> SourceCompatibleEnvironment:
        scenario = bundle.scenario
        return SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(scenario)),
            template=source_frame_template_for_scenario(scenario),
        )

    manager = SessionManager(
        environment_factory,
        storage_root=tmp_path / "managed",
        analyzer_factory=FailAfterSetup,
    )
    gateway = CommandGateway(
        manager, decision_interval_ms=1_000, frame_interval_ms=1_000
    )
    descriptor = gateway.create_session(
        red_controller_id="seat-red",
        blue_controller_id="seat-blue",
        seed=42,
    )
    match = MatchHandle(
        bundle, gateway, descriptor.session_id, "seat-red", "seat-blue"
    )
    red = RecordingSeatAgent("red", SeatDecision())
    blue = RecordingSeatAgent("blue", SeatDecision())
    with pytest.raises(LocalSeatMatchError) as captured:
        LocalSeatMatchRunner(
            match, run_namespace="test-local-seat", red_agent=red, blue_agent=blue
        ).run(tmp_path / "output", max_decision_windows=1)
    assert captured.value.status == "analysis_desynchronized"
    assert captured.value.detail["environment_already_advanced"] is True
    assert len(red.contexts) == len(blue.contexts) == 1
    assert not (tmp_path / "output").exists()
    with pytest.raises(CommandGatewayError, match="analysis_desynchronized"):
        manager.descriptor(match.session_id)
    match.close()


def test_same_seed_wait_agents_produce_exact_same_replay_and_analysis(
    tmp_path: Path,
) -> None:
    first, first_red, first_blue = _run_wait_match(tmp_path, "first")
    second, second_red, second_blue = _run_wait_match(tmp_path, "second")

    assert first.final_state_hash == second.final_state_hash
    assert first.winner == second.winner
    assert first.outcome == second.outcome
    assert first.replay_path.read_bytes() == second.replay_path.read_bytes()
    assert first.analysis_path.read_bytes() == second.analysis_path.read_bytes()
    assert first.transition_path.read_bytes() == second.transition_path.read_bytes()
    first_archive = SourceReplayArchiveImporter().import_path(first.replay_path)
    second_archive = SourceReplayArchiveImporter().import_path(second.replay_path)
    assert first_archive.steps == second_archive.steps
    assert [item.source_hash for item in first_archive.frames] == [
        item.source_hash for item in second_archive.frames
    ]
    assert [item.observation for item in first_red] == [
        item.observation for item in second_red
    ]
    assert [item.observation for item in first_blue] == [
        item.observation for item in second_blue
    ]


def test_short_terminal_match_publishes_strict_replay_and_exact_analysis_hashes(
    tmp_path: Path,
) -> None:
    record = load_scenario(SCENARIO).to_dict()
    record["scenario_id"] = "local-seat-terminal-fixture"
    blue = next(
        item for item in record["operators"] if item["side"] == "blue"
    )
    blue["health"] = 0
    blue["blood"] = 0
    blue["alive"] = False
    bundle = ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256=canonical_hash(record),
            source_kind="deterministic_test_derivative",
            warnings=("destroyed BLUE fixture for elimination terminal test",),
        ),
    )
    match = _facade_match(
        tmp_path,
        bundle=bundle,
        decision_interval_ms=10_000,
        frame_interval_ms=1_000,
    )
    try:
        result = LocalSeatMatchRunner(
            match,
            run_namespace="test-local-seat-terminal",
            red_agent=WaitSeatAgent("terminal-red"),
            blue_agent=WaitSeatAgent("terminal-blue"),
        ).run(tmp_path / "output")
    finally:
        match.close()

    assert result.terminal is True
    assert result.truncated is False
    assert result.decision_window_count == 1
    assert result.fact_frame_count == 2
    assert result.winner == "red"
    assert result.outcome == "win"
    assert result.transition_count == 2
    assert result.strict_replay_match is True
    terminal_transitions = [
        json.loads(line)
        for line in result.transition_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["reward"] for item in terminal_transitions] == [1.0, -1.0]
    assert all(item["terminated"] is True for item in terminal_transitions)
    assert all(item["truncated"] is False for item in terminal_transitions)
    analysis = [
        json.loads(line)
        for line in result.analysis_path.read_text(encoding="utf-8").splitlines()
    ]
    with ZipFile(result.replay_path) as archive:
        frame_payloads = [
            archive.read(f"{index}.json")
            for index in range(result.fact_frame_count)
        ]
    assert [item["provenance"]["frame_hash"] for item in analysis] == [
        hashlib.sha256(payload).hexdigest() for payload in frame_payloads
    ]
    assert [item["provenance"]["frame_index"] for item in analysis] == [0, 1]


@pytest.mark.skipif(
    not SOURCE_SCENARIO_123.is_file() or not SOURCE_MAP_123.is_file(),
    reason="read-only Map 123 Source inputs are unavailable",
)
def test_map123_two_window_real_agent_smoke(tmp_path: Path) -> None:
    conversion = convert_source_scenario(
        SOURCE_SCENARIO_123,
        SOURCE_MAP_123,
        conversion_mode=EXPLICIT_NATIVE_SUPPLEMENT,
        source_scenario_identity="scenarios/2201010105.json",
        source_map_identity="maps/123/basic.json",
        expected_scenario_id="2201010105",
        expected_map_id="123",
        supplement_path=SUPPLEMENT_123,
    )
    published = publish_source_scenario_conversion(
        conversion, tmp_path / "canonical-map123"
    )
    bundle = ScenarioBundle.from_path(published.scenario_path)
    red = RecordingSeatAgent("map123-red", SeatDecision())
    blue = RecordingSeatAgent("map123-blue", SeatDecision())
    match = _facade_match(
        tmp_path,
        bundle=bundle,
        decision_interval_ms=2_000,
        frame_interval_ms=1_000,
    )
    try:
        result = LocalSeatMatchRunner(
            match, run_namespace="test-local-seat-map123", red_agent=red, blue_agent=blue
        ).run(tmp_path / "map123-output", max_decision_windows=2)
    finally:
        match.close()

    assert len(red.contexts[0].observation["friendly_operators"]) == 26
    assert len(blue.contexts[0].observation["friendly_operators"]) == 23
    assert result.decision_window_count == 2
    assert result.transition_count == 4
    assert result.fact_frame_count == 5
    assert result.truncated is True
    assert result.strict_replay_match is True
    assert len(result.analysis_path.read_text(encoding="utf-8").splitlines()) == 5
    map_transitions = [
        json.loads(line)
        for line in result.transition_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["decision_index"] for item in map_transitions] == [0, 0, 1, 1]
    assert [item["side"] for item in map_transitions] == [
        "red", "blue", "red", "blue",
    ]
    assert all(item["truncated"] is False for item in map_transitions[:2])
    assert all(item["truncated"] is True for item in map_transitions[2:])
