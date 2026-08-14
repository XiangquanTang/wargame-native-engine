from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

from scripts.run_local_agent_match import _PreferredValidAgent
from scripts.run_minimal_source_replay import _template
from wargame_native_engine.agents import (
    AgentAction,
    AgentExecutionError,
    AgentResetContext,
    AgentStepContext,
    LocalAgentMatchError,
    LocalAgentMatchRunner,
    NoOpAgent,
    ScriptedAgent,
)
from wargame_native_engine.analysis import EightFactorAnalyzer, LiveAnalyzedSession
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.core.action import ActionType, EngineAction, ValidAction
from wargame_native_engine.core.observation import SideObservation
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import file_sha256
from wargame_native_engine.replay import SourceFrameRecorder
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)
from wargame_native_engine.version import ENGINE_VERSION


SCENARIO_PATH = Path("examples/source_movement_scenario.json")
STARTED_AT = "1970-01-01T00:00:00+00:00"


class _RecordingPreferredAgent(_PreferredValidAgent):
    def __init__(self, agent_id: str, preferences: Sequence[ActionType]) -> None:
        super().__init__(agent_id, preferences)
        self.reset_contexts: list[AgentResetContext] = []
        self.step_contexts: list[AgentStepContext] = []
        self.observation_hashes: list[str] = []
        self.observations: list[SideObservation] = []
        self.act_calls = 0

    def reset(self, context: AgentResetContext) -> None:
        self.reset_contexts.append(deepcopy(context))

    def act(self, *, observation, valid_actions, context):
        self.act_calls += 1
        self.observations.append(deepcopy(observation))
        self.observation_hashes.append(observation.observation_hash)
        self.step_contexts.append(deepcopy(context))
        return super().act(
            observation=observation,
            valid_actions=valid_actions,
            context=context,
        )


@pytest.fixture(scope="module")
def completed_match(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("local-agent-completed")
    environment = _new_environment()
    red = _RecordingPreferredAgent(
        "test-red",
        (ActionType.SHOOT, ActionType.MOVE, ActionType.WAIT),
    )
    blue = _RecordingPreferredAgent("test-blue", (ActionType.WAIT,))
    result = LocalAgentMatchRunner(
        environment=environment,
        red_agent=red,
        blue_agent=blue,
        delta_time_seconds=20,
    ).run(root / "match", seed=42)
    return {
        "root": root,
        "environment": environment,
        "red": red,
        "blue": blue,
        "result": result,
    }


def test_completed_match_outputs_native_replay_analysis_and_summary(
    completed_match,
) -> None:
    result = completed_match["result"]
    assert result.status == "completed"
    assert result.terminal is True
    assert result.truncated is False
    assert result.winner == "red"
    assert result.outcome == "win"
    assert result.step_count == 41
    assert result.frame_count == 42
    assert result.strict_replay_match is True
    assert result.online_offline_analysis_match is True
    assert {item.name for item in result.output_dir.iterdir()} == {
        "replay.zip",
        "frame_analysis.native.jsonl",
        "match_summary.native.json",
    }


def test_replay_initial_terminal_frames_analysis_and_strict_replay(
    completed_match,
) -> None:
    result = completed_match["result"]
    archive = SourceReplayArchiveImporter().import_path(result.replay_path)
    lines = result.analysis_path.read_text(encoding="utf-8").splitlines()
    records = tuple(json.loads(line) for line in lines)

    assert archive.frame_names == tuple(f"{index}.json" for index in range(42))
    assert len(archive.frames) == len(records) == 42
    assert records[0]["provenance"]["frame_index"] == 0
    assert records[-1]["provenance"]["frame_index"] == 41
    assert archive.frames[0].time.cur_step == 0
    assert archive.frames[-1].time.stage == 3
    with ZipFile(result.replay_path) as replay:
        for index in (0, 20, 41):
            payload = replay.read(f"{index}.json")
            assert records[index]["provenance"]["frame_hash"] == (
                hashlib.sha256(payload).hexdigest()
            )
    verification = replay_source_archive(
        completed_match["environment"].session.scenario,
        result.replay_path,
        ruleset=completed_match["environment"].session.backend.ruleset,
        strict=True,
    )
    assert verification.matches is True


def test_summary_hashes_match_published_artifacts(completed_match) -> None:
    result = completed_match["result"]
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary == result.summary
    assert summary["summary_schema"] == "native-local-agent-match-summary/1.0"
    assert summary["replay_sha256"] == file_sha256(result.replay_path)
    assert summary["analysis_sha256"] == file_sha256(result.analysis_path)
    assert result.summary_path.read_bytes().endswith(b"\n")


def test_agent_context_and_calls_are_side_specific(completed_match) -> None:
    red = completed_match["red"]
    blue = completed_match["blue"]
    result = completed_match["result"]
    assert len(red.reset_contexts) == len(blue.reset_contexts) == 1
    assert red.reset_contexts[0].agent_id == "test-red"
    assert red.reset_contexts[0].perspective == "RED"
    assert blue.reset_contexts[0].agent_id == "test-blue"
    assert blue.reset_contexts[0].perspective == "BLUE"
    assert red.reset_contexts[0].episode_seed == 42
    assert red.act_calls == blue.act_calls == result.step_count
    assert all(value.side == "red" for value in red.observations)
    assert all(value.side == "blue" for value in blue.observations)
    assert all(value.agent_id == "test-red" for value in red.step_contexts)
    assert all(value.agent_id == "test-blue" for value in blue.step_contexts)
    assert not hasattr(red.step_contexts[0], "green")
    assert not hasattr(red.step_contexts[0], "frame_analysis")


def test_hidden_information_and_mutable_agent_inputs_are_isolated(
    tmp_path: Path,
) -> None:
    class MutatingAgent(NoOpAgent):
        def __init__(self, agent_id: str) -> None:
            super().__init__(agent_id)
            self.received: list[SideObservation] = []

        def act(self, *, observation, valid_actions, context):
            del valid_actions, context
            self.received.append(deepcopy(observation))
            observation.mission["agent_mutation"] = self.agent_id
            if observation.friendly_operators:
                observation.friendly_operators[0].raw_attributes.clear()
            return None

    red = MutatingAgent("mutating-red")
    blue = MutatingAgent("mutating-blue")
    environment = _new_environment()
    result = LocalAgentMatchRunner(
        environment=environment,
        red_agent=red,
        blue_agent=blue,
    ).run(tmp_path / "isolated", seed=42, max_steps=1)

    assert result.status == "truncated"
    assert [item.operator_id for item in red.received[0].friendly_operators] == [1]
    assert [item.operator_id for item in blue.received[0].friendly_operators] == [2]
    assert red.received[0].visible_enemies == ()
    assert blue.received[0].visible_enemies == ()
    assert "agent_mutation" not in environment.session.observe("red").mission
    assert "agent_mutation" not in environment.session.observe("blue").mission
    assert environment.session.observe("red").friendly_operators[0].raw_attributes
    assert environment.session.observe("blue").friendly_operators[0].raw_attributes


def test_agent_cannot_authorize_mutated_nested_action_parameters(
    tmp_path: Path,
) -> None:
    class MutatingActionAgent(NoOpAgent):
        def act(self, *, observation, valid_actions, context):
            del observation, context
            move = next(
                item for item in valid_actions
                if item.action.action_type is ActionType.MOVE
            )
            authorized_id = move.action_id
            original_target = int(move.action.parameters["path"][0])
            move.action.parameters["path"][0] = 999999
            self.original_target = original_target
            return AgentAction(move.action, authorized_id)

    red = MutatingActionAgent("mutating-action-red")
    environment = _new_environment()
    result = LocalAgentMatchRunner(
        environment=environment,
        red_agent=red,
        blue_agent=NoOpAgent("noop-blue"),
    ).run(tmp_path / "immutable-authorization", max_steps=1)
    archive = SourceReplayArchiveImporter().import_path(result.replay_path)
    submitted = archive.steps[0]["ordered_actions"]
    red_action = next(item for item in submitted if item["side"] == "red")

    assert red_action["parameters"]["path"] == [red.original_target]
    assert 999999 not in red_action["parameters"]["path"]


def test_each_agent_and_underlying_step_called_once_with_deterministic_merge(
    tmp_path: Path,
) -> None:
    environment = _new_environment()
    original_step = environment.session.step
    environment.session.step = Mock(wraps=original_step)  # type: ignore[method-assign]
    red = _RecordingPreferredAgent("red-once", (ActionType.MOVE, ActionType.WAIT))
    blue = _RecordingPreferredAgent("blue-once", (ActionType.WAIT,))

    result = LocalAgentMatchRunner(
        environment=environment,
        red_agent=red,
        blue_agent=blue,
    ).run(tmp_path / "one-step", max_steps=1)
    archive = SourceReplayArchiveImporter().import_path(result.replay_path)

    assert red.act_calls == blue.act_calls == 1
    assert environment.session.step.call_count == 1  # type: ignore[attr-defined]
    assert [item["side"] for item in archive.steps[0]["ordered_actions"]] == [
        "red",
        "blue",
    ]


def test_empty_joint_action_advances_once_and_records_empty_actions(
    tmp_path: Path,
) -> None:
    environment = _new_environment()
    original_step = environment.session.step
    environment.session.step = Mock(wraps=original_step)  # type: ignore[method-assign]
    result = LocalAgentMatchRunner(
        environment=environment,
        red_agent=NoOpAgent("empty-red"),
        blue_agent=NoOpAgent("empty-blue"),
    ).run(tmp_path / "empty", max_steps=1)
    archive = SourceReplayArchiveImporter().import_path(result.replay_path)
    assert environment.session.step.call_count == 1  # type: ignore[attr-defined]
    assert archive.steps[0]["ordered_actions"] == []
    assert result.status == "truncated"
    assert result.terminal is False
    assert result.truncated is True
    assert result.strict_replay_match is None
    assert result.frame_count == 2
    assert len(result.analysis_path.read_text(encoding="utf-8").splitlines()) == 2


def test_max_steps_zero_publishes_initial_frame_as_runner_truncated(
    tmp_path: Path,
) -> None:
    environment = _new_environment()
    original_step = environment.session.step
    environment.session.step = Mock(wraps=original_step)  # type: ignore[method-assign]
    result = LocalAgentMatchRunner(
        environment=environment,
        red_agent=NoOpAgent("zero-red"),
        blue_agent=NoOpAgent("zero-blue"),
    ).run(tmp_path / "zero", max_steps=0)
    archive = SourceReplayArchiveImporter().import_path(result.replay_path)
    assert result.status == "truncated"
    assert result.step_count == 0
    assert result.frame_count == 1
    assert result.terminal is False
    assert result.winner is None
    assert result.strict_replay_match is None
    assert environment.session.step.call_count == 0  # type: ignore[attr-defined]
    assert archive.frame_names == ("0.json",)
    assert archive.steps == ()


def test_wrong_side_action_is_rejected_before_environment_step(
    tmp_path: Path,
) -> None:
    class WrongSideAgent(NoOpAgent):
        def act(self, *, observation, valid_actions, context):
            del observation, valid_actions, context
            action = EngineAction("blue", None, ActionType.WAIT)
            return AgentAction(action, action.action_id)

    environment = _new_environment()
    original_step = environment.session.step
    environment.session.step = Mock(wraps=original_step)  # type: ignore[method-assign]
    output = tmp_path / "wrong-side"
    with pytest.raises(AgentExecutionError) as error:
        LocalAgentMatchRunner(
            environment=environment,
            red_agent=WrongSideAgent("wrong-red"),
            blue_agent=NoOpAgent("blue"),
        ).run(output)
    assert error.value.agent_id == "wrong-red"
    assert error.value.perspective == "RED"
    assert error.value.step_index == 0
    assert error.value.cause_type == "ValueError"
    assert "side_mismatch" in error.value.cause_message
    assert error.value.called_agent_ids == ("wrong-red",)
    assert environment.session.step.call_count == 0  # type: ignore[attr-defined]
    assert not output.exists()


def test_invalid_valid_action_identity_is_rejected_before_step(
    tmp_path: Path,
) -> None:
    class InvalidIdentityAgent(NoOpAgent):
        def act(self, *, observation, valid_actions, context):
            del observation, context
            return AgentAction(valid_actions[0].action, "not-currently-valid")

    environment = _new_environment()
    original_step = environment.session.step
    environment.session.step = Mock(wraps=original_step)  # type: ignore[method-assign]
    with pytest.raises(AgentExecutionError) as error:
        LocalAgentMatchRunner(
            environment=environment,
            red_agent=InvalidIdentityAgent("invalid-red"),
            blue_agent=NoOpAgent("blue"),
        ).run(tmp_path / "invalid-id")
    assert "valid-action identity mismatch" in error.value.cause_message
    assert environment.session.step.call_count == 0  # type: ignore[attr-defined]


@pytest.mark.parametrize("failed_side", ["red", "blue"])
def test_agent_act_failure_stops_before_step_and_records_call_order(
    tmp_path: Path,
    failed_side: str,
) -> None:
    class ExplodingAgent(NoOpAgent):
        def act(self, *, observation, valid_actions, context):
            del observation, valid_actions, context
            raise LookupError("agent exploded")

    environment = _new_environment()
    original_step = environment.session.step
    environment.session.step = Mock(wraps=original_step)  # type: ignore[method-assign]
    red = ExplodingAgent("red-fail") if failed_side == "red" else NoOpAgent("red-ok")
    blue = ExplodingAgent("blue-fail") if failed_side == "blue" else NoOpAgent("blue-ok")
    output = tmp_path / f"act-{failed_side}"
    with pytest.raises(AgentExecutionError) as error:
        LocalAgentMatchRunner(
            environment=environment,
            red_agent=red,
            blue_agent=blue,
        ).run(output)
    expected = (red.agent_id,) if failed_side == "red" else (
        red.agent_id,
        blue.agent_id,
    )
    assert error.value.called_agent_ids == expected
    assert error.value.phase == "act"
    assert error.value.cause_type == "LookupError"
    assert environment.session.step.call_count == 0  # type: ignore[attr-defined]
    assert not output.exists()


@pytest.mark.parametrize("failed_side", ["red", "blue"])
def test_agent_reset_failure_is_ordered_and_not_published(
    tmp_path: Path,
    failed_side: str,
) -> None:
    class ResetFailure(NoOpAgent):
        def reset(self, context):
            del context
            raise RuntimeError("reset exploded")

    environment = _new_environment()
    red = ResetFailure("red-reset") if failed_side == "red" else NoOpAgent("red-ok")
    blue = ResetFailure("blue-reset") if failed_side == "blue" else NoOpAgent("blue-ok")
    output = tmp_path / f"reset-{failed_side}"
    with pytest.raises(AgentExecutionError) as error:
        LocalAgentMatchRunner(
            environment=environment,
            red_agent=red,
            blue_agent=blue,
        ).run(output)
    expected = (red.agent_id,) if failed_side == "red" else (
        red.agent_id,
        blue.agent_id,
    )
    assert error.value.called_agent_ids == expected
    assert error.value.phase == "reset"
    assert error.value.step_index == 0
    assert not output.exists()


def test_live_analysis_desynchronization_stops_match_without_stale_output(
    tmp_path: Path,
) -> None:
    delegate = EightFactorAnalyzer()

    class FailSecondAnalysis:
        def __init__(self) -> None:
            self.calls = 0

        def analyze_green_frame(self, item):
            self.calls += 1
            if self.calls == 2:
                raise ValueError("analysis exploded")
            return delegate.analyze_green_frame(item)

    environment = _new_environment()
    original_step = environment.session.step
    environment.session.step = Mock(wraps=original_step)  # type: ignore[method-assign]
    output = tmp_path / "desynchronized"
    with pytest.raises(LocalAgentMatchError) as error:
        LocalAgentMatchRunner(
            environment=environment,
            red_agent=NoOpAgent("red"),
            blue_agent=NoOpAgent("blue"),
            analyzer=FailSecondAnalysis(),  # type: ignore[arg-type]
        ).run(output)
    assert error.value.status == "analysis_desynchronized"
    assert error.value.error == {
        "phase": "step",
        "cause_type": "ValueError",
        "cause_message": "analysis exploded",
    }
    assert environment.session.step.call_count == 1  # type: ignore[attr-defined]
    assert not output.exists()


def test_scripted_agent_is_deterministic_and_does_not_mutate_script() -> None:
    action = EngineAction("red", 1, ActionType.WAIT, parameters={"nested": [1]})
    source = [action, None]
    agent = ScriptedAgent("script", source)
    source[0].parameters["nested"].append(2)
    context = AgentResetContext("session", "scenario", "red", 42, "1", "script")
    step_context = AgentStepContext("session", "red", 0, _stage(), 42, "script")
    observation = _new_environment().setup_views(42)
    side_observation = _new_environment()
    side_observation.setup_views(42)
    observed = side_observation.session.observe("red")

    agent.reset(context)
    first = agent.act(observation=observed, valid_actions=(), context=step_context)
    second = agent.act(observation=observed, valid_actions=(), context=step_context)
    third = agent.act(observation=observed, valid_actions=(), context=step_context)
    assert first is not None
    assert first.action.parameters == {"nested": [1]}
    assert second is None
    assert third is None
    del observation


def test_existing_output_is_rejected_before_environment_setup(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_bytes(b"unchanged")
    environment = _new_environment()
    original_setup = environment.setup_views
    environment.setup_views = Mock(wraps=original_setup)  # type: ignore[method-assign]
    with pytest.raises(FileExistsError, match="match output already exists"):
        LocalAgentMatchRunner(
            environment=environment,
            red_agent=NoOpAgent("red"),
            blue_agent=NoOpAgent("blue"),
        ).run(output)
    assert environment.setup_views.call_count == 0  # type: ignore[attr-defined]
    assert sentinel.read_bytes() == b"unchanged"


def test_overwrite_replaces_complete_output_directory(tmp_path: Path) -> None:
    output = tmp_path / "overwrite"
    output.mkdir()
    (output / "old.txt").write_bytes(b"old")
    result = LocalAgentMatchRunner(
        environment=_new_environment(),
        red_agent=NoOpAgent("red"),
        blue_agent=NoOpAgent("blue"),
    ).run(output, max_steps=0, overwrite=True)
    assert result.status == "truncated"
    assert {item.name for item in output.iterdir()} == {
        "replay.zip",
        "frame_analysis.native.jsonl",
        "match_summary.native.json",
    }


def test_agent_failure_never_publishes_success_artifacts(tmp_path: Path) -> None:
    class ExplodingAgent(NoOpAgent):
        def act(self, *, observation, valid_actions, context):
            del observation, valid_actions, context
            raise RuntimeError("no action available")

    output = tmp_path / "failed-match"
    with pytest.raises(AgentExecutionError) as error:
        LocalAgentMatchRunner(
            environment=_new_environment(),
            red_agent=ExplodingAgent("red-failure"),
            blue_agent=NoOpAgent("blue"),
        ).run(output)
    assert error.value.status == "agent_error"
    assert error.value.error["agent_id"] == "red-failure"
    assert error.value.error["cause_type"] == "RuntimeError"
    assert not output.exists()


def test_runner_is_deterministic_and_matches_direct_environment_execution(
    completed_match,
    tmp_path: Path,
) -> None:
    repeated_red = _RecordingPreferredAgent(
        "test-red",
        (ActionType.SHOOT, ActionType.MOVE, ActionType.WAIT),
    )
    repeated_blue = _RecordingPreferredAgent("test-blue", (ActionType.WAIT,))
    repeated = LocalAgentMatchRunner(
        environment=_new_environment(),
        red_agent=repeated_red,
        blue_agent=repeated_blue,
        delta_time_seconds=20,
    ).run(tmp_path / "repeated", seed=42)

    direct_red = _RecordingPreferredAgent(
        "test-red",
        (ActionType.SHOOT, ActionType.MOVE, ActionType.WAIT),
    )
    direct_blue = _RecordingPreferredAgent("test-blue", (ActionType.WAIT,))
    direct = _direct_run(tmp_path / "direct", direct_red, direct_blue)
    original = completed_match["result"]

    assert repeated.replay_path.read_bytes() == original.replay_path.read_bytes()
    assert repeated.analysis_path.read_bytes() == original.analysis_path.read_bytes()
    assert direct["replay"].read_bytes() == original.replay_path.read_bytes()
    assert direct["analysis"].read_bytes() == original.analysis_path.read_bytes()
    assert direct["result"]["winner"] == original.winner
    assert direct["result"]["world_hash"] == original.summary["final_state_hash"]
    assert repeated_red.observation_hashes == direct_red.observation_hashes
    assert repeated_blue.observation_hashes == direct_blue.observation_hashes


def _new_environment() -> SourceCompatibleEnvironment:
    scenario = load_scenario(SCENARIO_PATH)
    return SourceCompatibleEnvironment(
        GameSession(NativeEngineBackend(scenario)),
        template=_template(scenario),
    )


def _stage():
    environment = _new_environment()
    environment.setup_views(42)
    return environment.session.stage()


def _direct_run(root: Path, red, blue) -> dict[str, Any]:
    root.mkdir()
    environment = _new_environment()
    analyzer = EightFactorAnalyzer()
    live = LiveAnalyzedSession(environment, analyzer=analyzer)
    update = live.setup(42)
    recorder = SourceFrameRecorder(root / "facts")
    recorder.start(
        _metadata(environment, update.green_frame, red.agent_id, blue.agent_id),
        started_at=STARTED_AT,
    )
    recorder.record_initial(
        update.green_frame,
        initial_state_hash=environment.initial_state_hash,
    )
    for side, agent in (("red", red), ("blue", blue)):
        agent.reset(AgentResetContext(
            environment.session.session_id,
            environment.session.scenario.scenario_id,
            side,
            42,
            environment.session.backend.ruleset.ruleset_version,
            agent.agent_id,
        ))
    while not bool(update.done):
        actions = []
        for side, agent in (("red", red), ("blue", blue)):
            observation = environment.session.observe(side)
            selected = agent.act(
                observation=observation,
                valid_actions=environment.session.valid_actions(side),
                context=AgentStepContext(
                    environment.session.session_id,
                    side,
                    observation.tick,
                    observation.stage,
                    42,
                    agent.agent_id,
                ),
            )
            if selected is not None:
                actions.append(selected.action)
        update = live.step(actions, delta_time_seconds=20)
        step_record = environment.last_step_record
        assert step_record is not None
        recorder.record_step(update.green_frame, step_record)
    result = environment.session.result().to_dict()
    recorder.finalize(result, final_state_hash=result["world_hash"])
    replay = recorder.package_zip(root / "replay.zip")
    analysis = root / "frame_analysis.native.jsonl"
    analysis.write_bytes(live.analysis_jsonl().encode("utf-8"))
    return {"replay": replay, "analysis": analysis, "result": result}


def _metadata(environment, green, red_agent_id: str, blue_agent_id: str):
    native = green["extra"]["native"]
    scenario = environment.session.scenario
    return {
        "scenario_id": scenario.scenario_id,
        "scenario_hash": scenario.scenario_hash,
        "terrain_id": green["terrain_id"],
        "episode_id": environment.session.session_id,
        "engine_version": ENGINE_VERSION,
        "ruleset_id": native["ruleset_id"],
        "ruleset_version": native["ruleset_version"],
        "ruleset_hash": native["ruleset_hash"],
        "adjudication_table_set_id": native["adjudication_table_set_id"],
        "adjudication_table_set_version": native["adjudication_table_set_version"],
        "adjudication_table_set_hash": native["adjudication_table_set_hash"],
        "movement_policy_id": native["movement_policy_id"],
        "movement_policy_version": native["movement_policy_version"],
        "movement_policy_hash": native["movement_policy_hash"],
        "perspective_policy_id": native["perspective_policy_id"],
        "perspective_policy_version": native["perspective_policy_version"],
        "perspective_policy_hash": native["perspective_policy_hash"],
        "episode_seed": 42,
        "max_ticks": environment.session.max_ticks,
        "red_agent_id": red_agent_id,
        "blue_agent_id": blue_agent_id,
    }
