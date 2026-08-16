from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from wargame_native_engine.agents import AgentDecisionContext, SeatDecision, WaitSeatAgent
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.gateway import (
    CommandGatewayError,
    ControllerView,
    SessionDescriptor,
    SessionPublication,
    SimulationAdvance,
)
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.training import (
    NativeMultiSeatEnv,
    NativeMultiSeatError,
    SingleSeatTrainingAdapter,
    TrainingDecisionSnapshotMismatchError,
    UnsupportedTrainingDecisionModeError,
)


SCENARIO = Path("examples/source_movement_scenario.json")


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class MatchFactory:
    def __init__(
        self,
        root: Path,
        *,
        deadline_mode: str = "synchronous",
        clock: FakeClock | None = None,
    ) -> None:
        self.root = root
        self.bundle = ScenarioBundle.from_path(SCENARIO)
        self.deadline_mode = deadline_mode
        self.clock = clock
        self.matches = []

    def __call__(self, seed: int, episode_sequence: int):
        app = WargameFacade(
            ScenarioCatalog.from_bundles({
                self.bundle.scenario_identity.scenario_id: self.bundle,
            }),
            storage_root=self.root / f"managed-{episode_sequence}",
        )
        match = app.create_match(
            scenario_id=self.bundle.scenario_identity.scenario_id,
            red_controller_id="hardening-red",
            blue_controller_id="hardening-blue",
            seed=seed,
            decision_interval_ms=1_000,
            frame_interval_ms=1_000,
            deadline_mode=self.deadline_mode,
            deadline_seconds=(1.0 if self.deadline_mode != "synchronous" else None),
            clock=self.clock,
        )
        self.matches.append(match)
        return match


class RecordingWaitAgent(WaitSeatAgent):
    def __init__(self) -> None:
        super().__init__("hardening-opponent")
        self.contexts: list[AgentDecisionContext] = []

    def act(self, *, context: AgentDecisionContext) -> SeatDecision:
        self.contexts.append(deepcopy(context))
        return SeatDecision()


def test_native_multi_seat_refuses_server_monotonic_before_reset_result(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    factory = MatchFactory(
        tmp_path, deadline_mode="server_monotonic", clock=clock
    )
    environment = NativeMultiSeatEnv(
        factory, run_namespace="deadline-refusal"
    )
    try:
        with pytest.raises(
            UnsupportedTrainingDecisionModeError,
            match="requires_synchronous_decision_windows",
        ):
            environment.reset(seed=42)
        match = factory.matches[0]
        with pytest.raises(CommandGatewayError, match="session_not_found"):
            match.gateway.manager.descriptor(match.session_id)
        clock.advance(10)
        assert not any(tmp_path.glob("*.native.jsonl"))
    finally:
        environment.close()


def test_server_monotonic_gateway_still_advances_for_non_training_clients(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    factory = MatchFactory(
        tmp_path, deadline_mode="server_monotonic", clock=clock
    )
    match = factory(42, 1)
    try:
        assert match.status.frame_index == 0
        clock.advance(2)
        match.bind_controller(
            "red", controller_id=match.red_controller_id
        ).observe()
        window = match.gateway.window(
            match.session_id, match.red_controller_id
        )
        assert window.decision_window_index == 1
        assert match.status.frame_index == 1
    finally:
        match.close()


def test_pre_step_external_decision_drift_requires_reset_without_extra_submit(
    tmp_path: Path,
) -> None:
    factory = MatchFactory(tmp_path)
    environment = NativeMultiSeatEnv(
        factory, run_namespace="snapshot-drift"
    )
    try:
        initial = environment.reset(seed=42)
        match = environment.match
        for side, controller in (
            ("red", match.red_controller_id),
            ("blue", match.blue_controller_id),
        ):
            match.gateway.submit_batch(
                match.session_id,
                controller,
                request_id=f"external-{side}",
                expected_window=initial.decision_index,
                ordered_action_ids=(),
            )
        assert match.status.frame_index == 1
        audit_before = match.gateway.control_audit(
            match.session_id, match.red_controller_id
        )
        with pytest.raises(
            TrainingDecisionSnapshotMismatchError,
            match=r"expected=0;actual=1",
        ):
            environment.step(SeatDecision(), SeatDecision())
        assert match.status.frame_index == 1
        assert environment.transitions == ()
        assert match.gateway.control_audit(
            match.session_id, match.red_controller_id
        ) == audit_before
        with pytest.raises(NativeMultiSeatError, match="reset_required"):
            environment.step(SeatDecision(), SeatDecision())
        recovered = environment.reset(seed=42)
        assert recovered.decision_index == 0
    finally:
        environment.close()


def test_post_step_advance_identity_mismatch_emits_no_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = NativeMultiSeatEnv(
        MatchFactory(tmp_path), run_namespace="post-step-mismatch"
    )
    try:
        environment.reset(seed=42)
        gateway = environment.match.gateway
        original = gateway.last_advance

        def wrong_index(*args, **kwargs):
            value = original(*args, **kwargs)
            assert value is not None
            return replace(value, decision_window_index=99)

        monkeypatch.setattr(gateway, "last_advance", wrong_index)
        with pytest.raises(TrainingDecisionSnapshotMismatchError):
            environment.step(SeatDecision(), SeatDecision())
        assert environment.match.status.frame_index == 1
        assert environment.transitions == ()
        with pytest.raises(NativeMultiSeatError, match="reset_required"):
            environment.step(SeatDecision(), SeatDecision())
    finally:
        environment.close()


def test_red_blue_view_index_disagreement_is_snapshot_mismatch_before_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = NativeMultiSeatEnv(
        MatchFactory(tmp_path), run_namespace="side-index-mismatch"
    )
    try:
        environment.reset(42)
        session = environment._session  # noqa: SLF001 - protocol boundary probe
        assert session is not None
        original = session.controllers["blue"].observe
        monkeypatch.setattr(
            session.controllers["blue"],
            "observe",
            lambda: replace(original(), step_index=1),
        )
        audit_before = environment.match.gateway.control_audit(
            environment.match.session_id,
            environment.match.red_controller_id,
        )
        with pytest.raises(TrainingDecisionSnapshotMismatchError):
            environment.step(SeatDecision(), SeatDecision())
        assert environment.match.status.frame_index == 0
        assert environment.transitions == ()
        assert environment.match.gateway.control_audit(
            environment.match.session_id,
            environment.match.red_controller_id,
        ) == audit_before
    finally:
        environment.close()


@pytest.mark.parametrize("invalid", [0, 1, "true", "false", None])
def test_gateway_and_scheduler_status_models_require_exact_bool(invalid) -> None:
    with pytest.raises(TypeError, match="exact bool"):
        SessionDescriptor("s", 0, invalid)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="exact bool"):
        ControllerView(
            "s", "RED", 0, {}, {}, (), invalid, None, None  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="exact bool"):
        SessionPublication(
            Path("x"), Path("r"), Path("a"), 1, 0, invalid, False  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="exact bool"):
        SimulationAdvance(0, 1, 1, 1, 0, 1, invalid)  # type: ignore[arg-type]


def test_namespace_identity_is_reproducible_distinct_and_reset_incremented(
    tmp_path: Path,
) -> None:
    first = NativeMultiSeatEnv(
        MatchFactory(tmp_path / "first"), run_namespace="worker-000"
    )
    second = NativeMultiSeatEnv(
        MatchFactory(tmp_path / "second"), run_namespace="worker-000"
    )
    other = NativeMultiSeatEnv(
        MatchFactory(tmp_path / "other"), run_namespace="worker-001"
    )
    reproduced = NativeMultiSeatEnv(
        MatchFactory(tmp_path / "reproduced"), run_namespace="worker-000"
    )
    try:
        first_one = first.reset(42)
        second_one = second.reset(42)
        other_one = other.reset(42)
        assert first_one.episode_id == second_one.episode_id
        assert first_one.episode_id != other_one.episode_id
        assert first_one.run_namespace == "worker-000"
        first_two = first.reset(42)
        reproduced.reset(42)
        reproduced_two = reproduced.reset(42)
        assert first_two.episode_id != first_one.episode_id
        assert first_two.episode_id == reproduced_two.episode_id
    finally:
        first.close()
        second.close()
        other.close()
        reproduced.close()


def test_single_seat_cache_is_cleared_when_snapshot_drifts(tmp_path: Path) -> None:
    opponent = RecordingWaitAgent()
    environment = NativeMultiSeatEnv(
        MatchFactory(tmp_path), run_namespace="single-cache-drift"
    )
    adapter = SingleSeatTrainingAdapter(
        environment, controlled_side="red", opponent=opponent
    )
    try:
        initial = adapter.reset(42)
        match = environment.match
        for side, controller in (
            ("red", match.red_controller_id),
            ("blue", match.blue_controller_id),
        ):
            match.gateway.submit_batch(
                match.session_id,
                controller,
                request_id=f"cache-drift-{side}",
                expected_window=initial.capabilities.decision_index,
                ordered_action_ids=(),
            )
        with pytest.raises(TrainingDecisionSnapshotMismatchError):
            adapter.step(SeatDecision())
        assert opponent.contexts == []
        assert adapter._opponent_cache_key is None  # noqa: SLF001
        with pytest.raises(NativeMultiSeatError, match="not_reset"):
            adapter.step(SeatDecision())
    finally:
        adapter.close()
