from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

from scripts.run_minimal_source_replay import _template
from wargame_native_engine.agents import (
    AgentAction,
    AgentResetContext,
    AgentStepContext,
    LocalAgentMatchRunner,
    NoOpAgent,
)
from wargame_native_engine.analysis import analyze_replay_to_jsonl
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.core.action import ActionType, ValidAction, valid_action_sort_key
from wargame_native_engine.core.observation import SideObservation
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import load_scenario, scenario_from_dict
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)
from wargame_native_engine.training import (
    NativeActionEncoder,
    NativeTrainingEnv,
    NativeTrainingError,
)


SCENARIO_PATH = Path("examples/source_movement_scenario.json")


class EnvironmentFactory:
    def __init__(self, scenario=None) -> None:
        self.scenario = scenario or load_scenario(SCENARIO_PATH)
        self.environments: list[SourceCompatibleEnvironment] = []

    def __call__(self) -> SourceCompatibleEnvironment:
        environment = SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(self.scenario)),
            template=_template(self.scenario),
        )
        original = environment.session.step
        environment.session.step = Mock(  # type: ignore[method-assign]
            wraps=original
        )
        self.environments.append(environment)
        return environment


class RecordingNoOpAgent(NoOpAgent):
    def __init__(self, agent_id: str = "recording-opponent") -> None:
        super().__init__(agent_id)
        self.reset_contexts: list[AgentResetContext] = []
        self.observations: list[SideObservation] = []
        self.valid_actions_seen: list[tuple[ValidAction, ...]] = []
        self.closed_count = 0

    def reset(self, context: AgentResetContext) -> None:
        self.reset_contexts.append(deepcopy(context))

    def act(self, *, observation, valid_actions, context):
        del context
        self.observations.append(deepcopy(observation))
        self.valid_actions_seen.append(deepcopy(tuple(valid_actions)))
        return None

    def close(self) -> None:
        self.closed_count += 1


class PreferredAgent:
    def __init__(self, agent_id: str, preferences: tuple[ActionType, ...]) -> None:
        self.agent_id = agent_id
        self.preferences = preferences

    def reset(self, context: AgentResetContext) -> None:
        del context

    def act(
        self,
        *,
        observation: SideObservation,
        valid_actions,
        context: AgentStepContext,
    ) -> AgentAction | None:
        del context
        legal = set(observation.legal_action_ids)
        values = tuple(sorted(
            (item for item in valid_actions if item.action_id in legal),
            key=valid_action_sort_key,
        ))
        for action_type in self.preferences:
            matching = tuple(
                item for item in values if item.action.action_type is action_type
            )
            if not matching:
                continue
            selected = (
                max(
                    matching,
                    key=lambda item: tuple(
                        item.action.parameters.get("path", ())
                    ),
                )
                if action_type is ActionType.MOVE
                else matching[0]
            )
            return AgentAction.from_valid_action(selected)
        return None

    def close(self) -> None:
        return None


def _training_env(
    tmp_path: Path,
    *,
    side: str = "red",
    opponent=None,
    max_steps: int | None = None,
    factory: EnvironmentFactory | None = None,
    decision_interval_ms: int | None = None,
    frame_interval_ms: int | None = None,
):
    factory = factory or EnvironmentFactory()
    agent = opponent or RecordingNoOpAgent()
    environment = NativeTrainingEnv(
        factory,
        controlled_side=side,
        opponent=agent,
        storage_root=tmp_path / "training-facts",
        max_actions=16,
        max_steps=max_steps,
        delta_time_seconds=20,
        decision_interval_ms=decision_interval_ms,
        frame_interval_ms=frame_interval_ms,
    )
    return environment, factory, agent


@pytest.mark.parametrize("side", ["red", "blue"])
def test_reset_returns_only_controlled_side_and_exact_valid_action_mask(
    tmp_path: Path,
    side: str,
) -> None:
    environment, factory, opponent = _training_env(tmp_path, side=side)
    try:
        result = environment.reset(seed=42)
        session = factory.environments[0].session
        expected = tuple(item.to_dict() for item in session.valid_actions(side))

        assert result.observation["side"] == side
        assert "green" not in result.observation
        assert "frame_analysis" not in result.observation
        assert result.info["perspective"] == side.upper()
        assert result.info["referee_labels_exposed"] is False
        assert result.action_space.actions == expected
        assert result.action_space.action_ids == tuple(
            item["action_id"] for item in expected
        )
        assert result.action_space.mask[:len(expected)] == (True,) * len(expected)
        assert not any(result.action_space.mask[len(expected):])
        assert environment.valid_action_mask == result.action_space.mask
        assert opponent.reset_contexts[0].side != side
        assert opponent.reset_contexts[0].seed == 42
    finally:
        environment.close()


def test_observation_action_and_opponent_inputs_are_deeply_detached(
    tmp_path: Path,
) -> None:
    environment, factory, opponent = _training_env(tmp_path)
    try:
        initial = environment.reset()
        initial.observation["mission"]["mutated"] = True
        initial.action_space.actions[0]["action"]["parameters"]["mutated"] = True

        result = environment.step(None)

        assert "mutated" not in result.observation["mission"]
        current = factory.environments[0].session.valid_actions("red")
        assert all(
            "mutated" not in item.action.parameters for item in current
        )
        opponent.observations[0].mission["opponent_mutation"] = True
        assert "opponent_mutation" not in factory.environments[0].session.observe(
            "blue"
        ).mission
        assert opponent.observations[0].side == "blue"
    finally:
        environment.close()


def test_each_training_step_calls_opponent_and_underlying_step_once(
    tmp_path: Path,
) -> None:
    environment, factory, opponent = _training_env(tmp_path)
    try:
        initial = environment.reset()
        result = environment.step(0 if initial.action_space.action_ids else None)

        assert factory.environments[0].session.step.call_count == 1  # type: ignore[attr-defined]
        assert len(opponent.observations) == 1
        assert result.info["step_index"] == 1
        assert result.reward == 0.0
        assert result.terminated is False
        assert result.truncated is False
        assert result.observation["side"] == "red"
    finally:
        environment.close()


@pytest.mark.parametrize("invalid", [True, -1, 16, "0"])
def test_invalid_action_index_is_rejected_before_opponent_or_environment(
    tmp_path: Path,
    invalid,
) -> None:
    environment, factory, opponent = _training_env(tmp_path)
    try:
        environment.reset()
        with pytest.raises((TypeError, ValueError), match="action_id"):
            environment.step(invalid)
        assert not opponent.observations
        assert factory.environments[0].session.step.call_count == 0  # type: ignore[attr-defined]
    finally:
        environment.close()


def test_opponent_failure_never_advances_and_requires_reset(tmp_path: Path) -> None:
    class ExplodingOpponent(RecordingNoOpAgent):
        def act(self, *, observation, valid_actions, context):
            del observation, valid_actions, context
            raise RuntimeError("opponent unavailable")

    opponent = ExplodingOpponent()
    environment, factory, _ = _training_env(tmp_path, opponent=opponent)
    try:
        environment.reset()
        with pytest.raises(
            NativeTrainingError,
            match="training_opponent_act_failed",
        ):
            environment.step(None)
        assert factory.environments[0].session.step.call_count == 0  # type: ignore[attr-defined]
        with pytest.raises(NativeTrainingError, match="training_episode_failed"):
            environment.step(None)
        recovered = environment.reset(seed=43)
        assert recovered.info["step_index"] == 0
        assert len(factory.environments) == 2
    finally:
        environment.close()


def test_max_steps_is_training_truncation_not_native_terminal_and_can_publish(
    tmp_path: Path,
) -> None:
    environment, factory, _ = _training_env(tmp_path, max_steps=1)
    output = tmp_path / "truncated"
    try:
        environment.reset()
        result = environment.step(None)

        assert result.terminated is False
        assert result.truncated is True
        assert result.info["winner"] is None
        assert result.info["truncation_source"] == "training_max_steps"
        assert factory.environments[0].session.is_terminal() is False
        with pytest.raises(NativeTrainingError, match="ended_reset_required"):
            environment.step(None)

        publication = environment.publish(output)
        archive = SourceReplayArchiveImporter().import_path(publication.replay_path)
        assert archive.manifest["termination"] is False
        assert archive.manifest["truncated"] is True
        assert archive.frame_names == ("0.json", "1.json")
        assert len(publication.analysis_path.read_text(
            encoding="utf-8"
        ).splitlines()) == 2
        with pytest.raises(NativeTrainingError, match="ended_reset_required"):
            environment.step(None)
    finally:
        environment.close()


def test_terminal_sparse_reward_strict_replay_and_offline_analysis_match(
    tmp_path: Path,
) -> None:
    opponent = PreferredAgent("blue-wait", (ActionType.WAIT,))
    environment, factory, _ = _training_env(tmp_path, opponent=opponent)
    output = tmp_path / "terminal"
    try:
        state = environment.reset(seed=42)
        final = None
        for _ in range(100):
            selected = _preferred_index(
                state.action_space.actions,
                (ActionType.SHOOT, ActionType.MOVE, ActionType.WAIT),
            )
            final = environment.step(selected)
            if final.terminated:
                break
            state = final
        assert final is not None and final.terminated
        assert final.truncated is False
        assert final.reward == 1.0
        assert final.info["winner"] == "red"

        publication = environment.publish(output)
        session = factory.environments[0].session
        verification = replay_source_archive(
            factory.scenario,
            publication.replay_path,
            ruleset=session.backend.ruleset,
            strict=True,
        )
        assert verification.matches
        offline = tmp_path / "offline.jsonl"
        analyze_replay_to_jsonl(publication.replay_path, offline)
        assert offline.read_bytes() == publication.analysis_path.read_bytes()
    finally:
        environment.close()


def test_matches_local_agent_runner_fact_trace_and_analysis(tmp_path: Path) -> None:
    training_opponent = PreferredAgent("blue-wait", (ActionType.WAIT,))
    environment, factory, _ = _training_env(
        tmp_path / "training",
        opponent=training_opponent,
    )
    training_output = tmp_path / "training-output"
    try:
        state = environment.reset(seed=42)
        while True:
            selected = _preferred_index(
                state.action_space.actions,
                (ActionType.SHOOT, ActionType.MOVE, ActionType.WAIT),
            )
            step = environment.step(selected)
            if step.terminated:
                break
            state = step
        training_publication = environment.publish(training_output)
    finally:
        environment.close()

    runner_output = tmp_path / "runner-output"
    runner = LocalAgentMatchRunner(
        environment=_new_environment(),
        red_agent=PreferredAgent(
            "training-controlled-red",
            (ActionType.SHOOT, ActionType.MOVE, ActionType.WAIT),
        ),
        blue_agent=PreferredAgent("blue-wait", (ActionType.WAIT,)),
        delta_time_seconds=20,
    ).run(runner_output, seed=42)

    with ZipFile(training_publication.replay_path) as training_zip, ZipFile(
        runner.replay_path
    ) as runner_zip:
        comparable = tuple(
            name for name in training_zip.namelist()
            if name != "replay_manifest.native.jsonl"
        )
        assert comparable == tuple(
            name for name in runner_zip.namelist()
            if name != "replay_manifest.native.jsonl"
        )
        assert all(
            training_zip.read(name) == runner_zip.read(name)
            for name in comparable
        )
    assert training_publication.analysis_path.read_bytes() == (
        runner.analysis_path.read_bytes()
    )
    assert factory.environments[0].session.result().world_hash == (
        runner.summary["final_state_hash"]
    )


def test_reset_determinism_and_multiple_env_instances_do_not_share_state(
    tmp_path: Path,
) -> None:
    first, first_factory, _ = _training_env(tmp_path / "one")
    second, second_factory, _ = _training_env(tmp_path / "two")
    try:
        one = first.reset(seed=7)
        two = second.reset(seed=7)
        assert one.observation == two.observation
        assert one.action_space == two.action_space

        first.step(None)
        assert first_factory.environments[0].session.step.call_count == 1  # type: ignore[attr-defined]
        assert second_factory.environments[0].session.step.call_count == 0  # type: ignore[attr-defined]

        repeated = first.reset(seed=7)
        assert repeated.observation == one.observation
        assert repeated.action_space == one.action_space
        assert repeated.info["step_index"] == 0
    finally:
        first.close()
        second.close()


def test_action_encoder_preserves_upstream_order_and_fixed_capacity() -> None:
    records = (
        {"action_id": "second", "priority": 2, "action": {"value": [2]}},
        {"action_id": "first", "priority": 1, "action": {"value": [1]}},
    )
    encoder = NativeActionEncoder(max_actions=4)

    encoded = encoder.encode(records)

    assert encoded.action_ids == ("second", "first")
    assert encoded.mask == (True, True, False, False)
    assert encoder.resolve(encoded, 0) == "second"
    assert encoder.resolve(encoded, 1) == "first"
    assert encoder.resolve_batch(encoded, (1, 0)) == ("first", "second")
    assert encoder.resolve_batch(encoded, ()) == ()
    with pytest.raises(ValueError, match="more than once"):
        encoder.resolve_batch(encoded, (0, 0))
    records[0]["action"]["value"].append(99)
    assert encoded.actions[0]["action"]["value"] == [2]


def test_one_training_decision_can_generate_ten_fact_frames(
    tmp_path: Path,
) -> None:
    environment, factory, _ = _training_env(
        tmp_path,
        decision_interval_ms=10_000,
        frame_interval_ms=1_000,
    )
    try:
        initial = environment.reset(seed=42)
        result = environment.step_batch(())

        assert initial.info["decision_window_index"] == 0
        assert result.info["processed_decision_window_index"] == 0
        assert result.info["decision_window_index"] == 1
        assert result.info["frame_range"] == [1, 10]
        assert result.info["generated_frame_count"] == 10
        assert result.info["sim_time_range_ms"] == [0, 10_000]
        assert result.info["frame_index"] == 10
        assert result.info["sim_time_ms"] == 10_000
        assert factory.environments[0].session.step.call_count == 10  # type: ignore[attr-defined]
    finally:
        environment.close()


def test_training_accepts_ordered_multi_actor_batch_and_records_only_first_frame(
    tmp_path: Path,
) -> None:
    document = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    second = deepcopy(document["operators"][0])
    second.update({"operator_id": 3, "current_hex": 102, "x": 2})
    document["operators"].append(second)
    document["deployment"]["positions"]["red"]["3"] = [2, 1]
    factory = EnvironmentFactory(scenario_from_dict(document))
    environment, _, _ = _training_env(
        tmp_path,
        factory=factory,
        decision_interval_ms=3_000,
        frame_interval_ms=1_000,
    )
    output = tmp_path / "published"
    try:
        initial = environment.reset(seed=42)
        waits = [
            (index, value["action"]["actor_id"])
            for index, value in enumerate(initial.action_space.actions)
            if value["action"]["action_type"] == ActionType.WAIT.value
        ]
        assert {actor for _, actor in waits} == {1, 3}
        selected = tuple(index for index, _ in reversed(waits))

        result = environment.step_batch(selected)

        assert result.info["frame_range"] == [1, 3]
        publication = environment.publish(output)
        with ZipFile(publication.replay_path) as archive:
            steps = [
                json.loads(line)
                for line in archive.read("steps.jsonl").decode("utf-8").splitlines()
            ]
        assert [
            item["actor_id"] for item in steps[0]["ordered_actions"]
        ] == [1, 3]
        assert steps[1]["ordered_actions"] == []
        assert steps[2]["ordered_actions"] == []
    finally:
        environment.close()


def test_opponent_batch_extension_uses_same_frozen_gateway_contract(
    tmp_path: Path,
) -> None:
    class BatchNoOpOpponent(RecordingNoOpAgent):
        def act(self, *, observation, valid_actions, context):
            raise AssertionError("legacy single-action method must not be used")

        def act_batch(self, *, observation, valid_actions, context):
            del observation, valid_actions, context
            return ()

    environment, factory, _ = _training_env(
        tmp_path,
        opponent=BatchNoOpOpponent(),
    )
    try:
        environment.reset(seed=42)
        environment.step_batch(())
        assert factory.environments[0].session.step.call_count == 1  # type: ignore[attr-defined]
    finally:
        environment.close()


def _preferred_index(actions, preferences: tuple[ActionType, ...]) -> int | None:
    for action_type in preferences:
        matching = tuple(
            (index, value)
            for index, value in enumerate(actions)
            if value["action"]["action_type"] == action_type.value
        )
        if not matching:
            continue
        if action_type is ActionType.MOVE:
            return max(
                matching,
                key=lambda item: tuple(
                    item[1]["action"]["parameters"].get("path", ())
                ),
            )[0]
        return matching[0][0]
    return None


def _new_environment() -> SourceCompatibleEnvironment:
    scenario = load_scenario(SCENARIO_PATH)
    return SourceCompatibleEnvironment(
        GameSession(NativeEngineBackend(scenario)),
        template=_template(scenario),
    )
