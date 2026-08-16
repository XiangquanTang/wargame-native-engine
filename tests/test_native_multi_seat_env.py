from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from wargame_native_engine.agents import (
    AgentDecisionContext,
    SeatDecision,
    SeatOperation,
    WaitSeatAgent,
)
from wargame_native_engine.data import (
    DataProvenance,
    ScenarioBundle,
    ScenarioCatalog,
)
from wargame_native_engine.gateway import CommandGatewayError
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.operations import ProductOperationService, WargameFacade
from wargame_native_engine.training import (
    NativeMultiSeatEnv,
    NativeMultiSeatError,
    SingleSeatTrainingAdapter,
)


SCENARIO = Path("examples/source_movement_scenario.json")


class RecordingWaitSeatAgent(WaitSeatAgent):
    def __init__(self, agent_id: str) -> None:
        super().__init__(agent_id)
        self.reset_count = 0
        self.contexts: list[AgentDecisionContext] = []

    def reset(self, context) -> None:
        del context
        self.reset_count += 1

    def act(self, *, context: AgentDecisionContext) -> SeatDecision:
        self.contexts.append(deepcopy(context))
        return SeatDecision()


class MatchFactory:
    def __init__(
        self,
        tmp_path: Path,
        *,
        bundle: ScenarioBundle | None = None,
        decision_interval_ms: int = 10_000,
        frame_interval_ms: int = 1_000,
    ) -> None:
        self.tmp_path = tmp_path
        self.bundle = bundle or ScenarioBundle.from_path(SCENARIO)
        self.decision_interval_ms = decision_interval_ms
        self.frame_interval_ms = frame_interval_ms
        self.matches = []

    def __call__(self, seed: int, episode_sequence: int):
        app = WargameFacade(
            ScenarioCatalog.from_bundles({
                self.bundle.scenario_identity.scenario_id: self.bundle,
            }),
            storage_root=self.tmp_path / f"managed-{episode_sequence}",
        )
        match = app.create_match(
            scenario_id=self.bundle.scenario_identity.scenario_id,
            red_controller_id="native-multi-red",
            blue_controller_id="native-multi-blue",
            seed=seed,
            decision_interval_ms=self.decision_interval_ms,
            frame_interval_ms=self.frame_interval_ms,
        )
        self.matches.append(match)
        return match


def _terminal_bundle(winner: str = "red") -> ScenarioBundle:
    record = json.loads(SCENARIO.read_text(encoding="utf-8"))
    record["scenario_id"] = f"native-multi-terminal-{winner}"
    losing_side = "blue" if winner == "red" else "red"
    loser = next(
        value for value in record["operators"] if value["side"] == losing_side
    )
    loser.update({"health": 0, "blood": 0, "alive": False})
    return ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256=canonical_hash(record),
            source_kind="deterministic_test_derivative",
        ),
    )


def _multi_actor_bundle() -> ScenarioBundle:
    record = json.loads(SCENARIO.read_text(encoding="utf-8"))
    second = deepcopy(record["operators"][0])
    second.update({"operator_id": 3, "current_hex": 102, "x": 2})
    record["operators"].append(second)
    record["deployment"]["positions"]["red"]["3"] = [2, 1]
    record["scenario_id"] = "native-multi-atomic-operations"
    return ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256=canonical_hash(record),
            source_kind="deterministic_test_derivative",
        ),
    )


def test_seat_decision_is_ordered_explicit_noop_and_rejects_actor_conflict() -> None:
    move = SeatOperation("MOVE", (1, 2), destination_hex=105)
    shoot = SeatOperation("SHOOT", (3,), target_id=8)
    decision = SeatDecision((move, shoot))

    assert decision.operations == (move, shoot)
    assert SeatDecision().operations == ()
    assert SeatDecision.single(move).operations == (move,)
    with pytest.raises(ValueError, match="multiple SeatDecision"):
        SeatDecision((move, SeatOperation("WAIT", (2,))))
    with pytest.raises(ValueError, match="only SeatDecision operation"):
        SeatDecision((SeatOperation.wait(), shoot))
    with pytest.raises(ValueError, match="selected actors"):
        SeatOperation("SHOOT", (3,), target_id=8, weapon_choices=((4, 9),))


def test_product_decision_previews_all_operations_then_submits_side_once(
    tmp_path: Path,
) -> None:
    match = MatchFactory(
        tmp_path,
        bundle=_multi_actor_bundle(),
        decision_interval_ms=1_000,
        frame_interval_ms=1_000,
    )(42, 1)
    original = match.gateway.submit_batch
    match.gateway.submit_batch = Mock(wraps=original)  # type: ignore[method-assign]
    red = ProductOperationService(match.bind_controller(
        "red", controller_id="native-multi-red"
    ))
    blue = ProductOperationService(match.bind_controller(
        "blue", controller_id="native-multi-blue"
    ))
    decision = SeatDecision((
        SeatOperation("MOVE", (1,), destination_hex=104),
        SeatOperation("WAIT", (3,)),
    ))
    try:
        preview = red.preview_decision(decision, request_id="red-preview")
        assert preview.operation_count == 2
        assert preview.executable_count == 2
        assert match.status.frame_index == 0

        submitted = red.execute_decision(
            preview.preview_token, request_id="red-execute"
        )
        assert submitted.advanced is False
        assert match.gateway.submit_batch.call_count == 1  # type: ignore[attr-defined]
        assert red.execute_decision(
            preview.preview_token, request_id="red-execute"
        ) == submitted
        assert match.gateway.submit_batch.call_count == 1  # type: ignore[attr-defined]
        blue_preview = blue.preview_decision(
            SeatDecision(), request_id="blue-preview"
        )
        completed = blue.execute_decision(
            blue_preview.preview_token, request_id="blue-execute"
        )
        assert completed.advanced is True
        assert match.gateway.submit_batch.call_count == 2  # type: ignore[attr-defined]
        step = match.gateway.manager._managed(  # noqa: SLF001
            match.session_id
        ).environment.last_step_record
        assert {
            value["action_type"] for value in step["ordered_actions"]
        } == {"MOVE", "WAIT"}
        assert {
            value["actor_id"] for value in step["ordered_actions"]
        } == {1, 3}
    finally:
        match.close()


def test_product_decision_invalid_later_operation_submits_nothing(
    tmp_path: Path,
) -> None:
    match = MatchFactory(tmp_path)(42, 1)
    original = match.gateway.submit_batch
    match.gateway.submit_batch = Mock(wraps=original)  # type: ignore[method-assign]
    red = ProductOperationService(match.bind_controller(
        "red", controller_id="native-multi-red"
    ))
    decision = SeatDecision((
        SeatOperation("MOVE", (1,), destination_hex=104),
        SeatOperation("STOP_MOVE", (2,)),
    ))
    try:
        with pytest.raises(CommandGatewayError, match="preview_not_executable"):
            red.preview_decision(decision, request_id="invalid-decision")
        assert match.gateway.submit_batch.call_count == 0  # type: ignore[attr-defined]
        assert match.status.frame_index == 0
        assert match.gateway.window(
            match.session_id, "native-multi-red"
        ).pending_perspectives == ("RED", "BLUE")
    finally:
        match.close()


def test_product_decision_combines_real_move_and_stop_move_in_one_submit(
    tmp_path: Path,
) -> None:
    match = MatchFactory(
        tmp_path,
        bundle=_multi_actor_bundle(),
        decision_interval_ms=1_000,
        frame_interval_ms=1_000,
    )(42, 1)
    red = ProductOperationService(match.bind_controller(
        "red", controller_id="native-multi-red"
    ))
    blue = ProductOperationService(match.bind_controller(
        "blue", controller_id="native-multi-blue"
    ))
    try:
        first = red.preview_decision(
            SeatDecision.single(
                SeatOperation("MOVE", (1,), destination_hex=105)
            ),
            request_id="start-move-red",
        )
        red.execute_decision(first.preview_token, request_id="start-move-execute")
        waiting = blue.preview_decision(SeatDecision(), request_id="start-wait-blue")
        blue.execute_decision(
            waiting.preview_token, request_id="start-wait-execute"
        )

        original = match.gateway.submit_batch
        match.gateway.submit_batch = Mock(  # type: ignore[method-assign]
            wraps=original
        )
        combined = red.preview_decision(
            SeatDecision((
                SeatOperation("STOP_MOVE", (1,)),
                SeatOperation("MOVE", (3,), destination_hex=104),
            )),
            request_id="combined-preview",
        )
        assert combined.operation_count == 2
        assert combined.executable_count == 2
        red.execute_decision(
            combined.preview_token, request_id="combined-execute"
        )
        assert match.gateway.submit_batch.call_count == 1  # type: ignore[attr-defined]
        waiting = blue.preview_decision(SeatDecision(), request_id="combined-wait")
        blue.execute_decision(
            waiting.preview_token, request_id="combined-wait-execute"
        )
        step = match.gateway.manager._managed(  # noqa: SLF001
            match.session_id
        ).environment.last_step_record
        assert [
            action["action_type"] for action in step["ordered_actions"]
            if action["side"] == "red"
        ] == ["MOVE", "STOP_MOVE"]
    finally:
        match.close()


def test_native_multi_seat_step_is_one_decision_and_ten_fact_frames(
    tmp_path: Path,
) -> None:
    factory = MatchFactory(tmp_path)
    environment = NativeMultiSeatEnv(
        factory, run_namespace="test-native-multi", max_decision_windows=1
    )
    try:
        initial = environment.reset(seed=42)
        assert initial.decision_index == 0
        assert initial.fact_frame_index == 0
        assert initial.observations["red"]["side"] == "red"
        assert initial.observations["blue"]["side"] == "blue"
        assert initial.capabilities["red"].side == "red"
        assert initial.capabilities["blue"].side == "blue"
        assert "green" not in str(initial.to_record()).lower()

        result = environment.step(SeatDecision(), SeatDecision())

        assert result.decision_index == 0
        assert result.fact_frame_start == 1
        assert result.fact_frame_end == 10
        assert result.sim_time_start_ms == 0
        assert result.sim_time_end_ms == 10_000
        assert result.terminated is False
        assert result.truncated is True
        assert result.rewards == {"red": 0.0, "blue": 0.0}
        assert len(result.transitions) == 2
        assert [value.side for value in result.transitions] == ["red", "blue"]
        assert all(value.decision_index == 0 for value in result.transitions)
        assert all(value.fact_frame_start == 1 for value in result.transitions)
        assert all(value.fact_frame_end == 10 for value in result.transitions)
        assert all(value.truncated for value in result.transitions)
        assert environment.match.status.frame_index == 10
    finally:
        environment.close()


def test_multi_seat_preflight_failure_is_retryable_and_creates_no_transition(
    tmp_path: Path,
) -> None:
    environment = NativeMultiSeatEnv(
        MatchFactory(
            tmp_path,
            decision_interval_ms=1_000,
            frame_interval_ms=1_000,
        ),
        run_namespace="test-native-multi",
    )
    try:
        environment.reset(seed=42)
        with pytest.raises(NativeMultiSeatError) as captured:
            environment.step(
                SeatDecision.single(SeatOperation("STOP_MOVE", (1,))),
                SeatDecision(),
            )
        assert captured.value.code == "decision_preview_failed"
        assert environment.match.status.frame_index == 0
        assert environment.transitions == ()

        recovered = environment.step(SeatDecision(), SeatDecision())
        assert recovered.fact_frame_start == recovered.fact_frame_end == 1
    finally:
        environment.close()


def test_execution_failure_after_red_submit_requires_reset_and_never_advances(
    tmp_path: Path,
) -> None:
    factory = MatchFactory(
        tmp_path,
        decision_interval_ms=1_000,
        frame_interval_ms=1_000,
    )
    environment = NativeMultiSeatEnv(
        factory, run_namespace="test-native-multi"
    )
    try:
        environment.reset(seed=42)
        session = environment._session  # noqa: SLF001 - failure boundary probe
        assert session is not None
        session.product_services["blue"].execute_decision = Mock(  # type: ignore[method-assign]
            side_effect=RuntimeError("blue submit fault")
        )

        with pytest.raises(NativeMultiSeatError) as captured:
            environment.step(SeatDecision(), SeatDecision())
        assert captured.value.code == "decision_execution_failed_reset_required"
        assert captured.value.side == "blue"
        assert environment.match.status.frame_index == 0
        assert environment.transitions == ()
        window = environment.match.gateway.window(
            environment.match.session_id, "native-multi-red"
        )
        assert window.pending_perspectives == ("BLUE",)
        with pytest.raises(NativeMultiSeatError, match="reset_required"):
            environment.step(SeatDecision(), SeatDecision())

        recovered = environment.reset(seed=42)
        assert recovered.decision_index == 0
        result = environment.step(SeatDecision(), SeatDecision())
        assert result.fact_frame_start == result.fact_frame_end == 1
    finally:
        environment.close()


def test_native_terminal_rewards_publish_and_strict_replay(tmp_path: Path) -> None:
    factory = MatchFactory(
        tmp_path,
        bundle=_terminal_bundle("red"),
        decision_interval_ms=10_000,
        frame_interval_ms=1_000,
    )
    environment = NativeMultiSeatEnv(
        factory, run_namespace="test-native-multi"
    )
    output = tmp_path / "published"
    try:
        environment.reset(seed=42)
        result = environment.step(SeatDecision(), SeatDecision())
        assert result.terminated is True
        assert result.truncated is False
        assert result.rewards == {"red": 1.0, "blue": -1.0}
        assert result.winner == "red"
        assert result.fact_frame_start == result.fact_frame_end == 1
        publication = environment.publish(output)
        environment.write_transitions(
            output / "decision_transitions.native.jsonl"
        )
        archive = SourceReplayArchiveImporter().import_path(publication.replay_path)
        assert archive.manifest["termination"] is True
        assert archive.manifest["truncated"] is False
        assert replay_source_archive(
            factory.bundle.scenario,
            publication.replay_path,
            strict=True,
        ).matches
    finally:
        environment.close()


@pytest.mark.parametrize("controlled_side", ["red", "blue"])
def test_single_seat_adapter_reuses_multi_seat_and_calls_opponent_once(
    tmp_path: Path,
    controlled_side: str,
) -> None:
    opponent = RecordingWaitSeatAgent(f"opponent-{controlled_side}")
    multi = NativeMultiSeatEnv(
        MatchFactory(tmp_path),
        run_namespace="test-native-multi",
        max_decision_windows=1,
    )
    adapter = SingleSeatTrainingAdapter(
        multi,
        controlled_side=controlled_side,
        opponent=opponent,
    )
    try:
        initial = adapter.reset(seed=42)
        assert initial.observation["side"] == controlled_side
        result = adapter.step(SeatDecision())
        assert result.observation["side"] == controlled_side
        assert result.reward == 0.0
        assert result.terminated is False
        assert result.truncated is True
        assert result.transition.side == controlled_side
        assert opponent.reset_count == 1
        assert len(opponent.contexts) == 1
        assert opponent.contexts[0].side != controlled_side
    finally:
        adapter.close()


def test_single_seat_retry_reuses_one_opponent_decision_for_the_window(
    tmp_path: Path,
) -> None:
    opponent = RecordingWaitSeatAgent("retry-opponent")
    adapter = SingleSeatTrainingAdapter(
        NativeMultiSeatEnv(MatchFactory(
            tmp_path,
            decision_interval_ms=1_000,
            frame_interval_ms=1_000,
        ), run_namespace="test-native-multi"),
        controlled_side="red",
        opponent=opponent,
    )
    try:
        adapter.reset(seed=42)
        with pytest.raises(NativeMultiSeatError, match="decision_preview_failed"):
            adapter.step(SeatDecision.single(SeatOperation("STOP_MOVE", (1,))))
        result = adapter.step(SeatDecision())
        assert result.transition.decision_index == 0
        assert len(opponent.contexts) == 1
    finally:
        adapter.close()


def test_single_seat_opponent_failure_requires_reset_and_is_not_retried(
    tmp_path: Path,
) -> None:
    class FailingOpponent(RecordingWaitSeatAgent):
        def act(self, *, context: AgentDecisionContext) -> SeatDecision:
            self.contexts.append(deepcopy(context))
            raise LookupError("opponent failed")

    opponent = FailingOpponent("failing-opponent")
    adapter = SingleSeatTrainingAdapter(
        NativeMultiSeatEnv(
            MatchFactory(tmp_path), run_namespace="test-native-multi"
        ),
        controlled_side="red",
        opponent=opponent,
    )
    try:
        adapter.reset(seed=42)
        with pytest.raises(NativeMultiSeatError, match="reset_required"):
            adapter.step(SeatDecision())
        with pytest.raises(NativeMultiSeatError, match="not_reset"):
            adapter.step(SeatDecision())
        assert len(opponent.contexts) == 1
        assert adapter.environment.match.status.frame_index == 0
    finally:
        adapter.close()
