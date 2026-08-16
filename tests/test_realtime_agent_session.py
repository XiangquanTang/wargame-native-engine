from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock

import pytest

from wargame_native_engine.agents import (
    CommandStatus,
    ManualRealtimeClock,
    RealtimeAgentSession,
    RealtimeAgentSessionError,
    RealtimeSeatBinding,
    SeatDecision,
    SeatOperation,
)
from wargame_native_engine.data import (
    DataProvenance,
    ScenarioBundle,
    ScenarioCatalog,
)
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.operations import WargameFacade


SCENARIO = Path("examples/source_movement_scenario.json")


class MatchFactory:
    def __init__(
        self,
        root: Path,
        *,
        tick_interval_ms: int = 50,
        bundle: ScenarioBundle | None = None,
        deadline_mode: str = "synchronous",
    ) -> None:
        self.root = root
        self.tick_interval_ms = tick_interval_ms
        self.bundle = bundle or ScenarioBundle.from_path(SCENARIO)
        self.deadline_mode = deadline_mode
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
            red_controller_id="realtime-red",
            blue_controller_id="realtime-blue",
            seed=seed,
            decision_interval_ms=self.tick_interval_ms,
            frame_interval_ms=self.tick_interval_ms,
            deadline_mode=self.deadline_mode,
            deadline_seconds=(1.0 if self.deadline_mode != "synchronous" else None),
        )
        self.matches.append(match)
        return match


def _environment(
    tmp_path: Path,
    *,
    tick_interval_ms: int = 50,
    bundle: ScenarioBundle | None = None,
    observation_history_size: int = 64,
):
    clock = ManualRealtimeClock()
    value = RealtimeAgentSession(
        MatchFactory(
            tmp_path,
            tick_interval_ms=tick_interval_ms,
            bundle=bundle,
        ),
        run_namespace="realtime-test-worker-000",
        tick_interval_ms=tick_interval_ms,
        clock=clock,
        observation_history_size=observation_history_size,
    )
    return value, clock


def _max_tick_bundle(max_ticks: int) -> ScenarioBundle:
    record = json.loads(SCENARIO.read_text(encoding="utf-8"))
    record["scenario_id"] = f"realtime-max-tick-{max_ticks}"
    record["terminal_rules"]["max_ticks"] = max_ticks
    return ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256=canonical_hash(record),
            source_kind="deterministic_test_derivative",
        ),
    )


def _terminal_bundle() -> ScenarioBundle:
    record = json.loads(SCENARIO.read_text(encoding="utf-8"))
    record["scenario_id"] = "realtime-terminal-red"
    loser = next(
        value for value in record["operators"] if value["side"] == "blue"
    )
    loser.update({"health": 0, "blood": 0, "alive": False})
    return ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256=canonical_hash(record),
            source_kind="deterministic_test_derivative",
        ),
    )


def test_one_side_or_no_side_never_blocks_an_authoritative_tick(
    tmp_path: Path,
) -> None:
    environment, _ = _environment(tmp_path)
    try:
        reset = environment.reset(seed=42)
        blue = environment.submit(
            reset.blue_binding,
            observation_version=reset.blue.observation_version,
            decision=SeatDecision(),
        )
        assert blue.accepted is True and blue.target_tick == 0
        first = environment.advance_tick()
        assert first.tick_index == 0
        assert first.frame_index == 1
        assert first.red_command_sequences == ()
        assert first.blue_command_sequences == (blue.command_sequence,)
        assert environment.command_events[0].status is CommandStatus.APPLIED

        second = environment.advance_tick()
        assert second.tick_index == 1
        assert second.frame_index == 2
        assert second.red_command_sequences == ()
        assert second.blue_command_sequences == ()
    finally:
        environment.close()


def test_ten_millisecond_arrival_difference_stays_in_one_tick(
    tmp_path: Path,
) -> None:
    environment, clock = _environment(tmp_path)
    try:
        reset = environment.reset(seed=42)
        blue = environment.submit(
            reset.blue_binding,
            observation_version=reset.blue.observation_version,
            decision=SeatDecision(),
        )
        clock.advance_ms(10)
        red = environment.submit(
            reset.red_binding,
            observation_version=reset.red.observation_version,
            decision=SeatDecision(),
        )
        assert blue.target_tick == red.target_tick == 0
        tick = environment.advance_tick()
        assert tick.red_command_sequences == (red.command_sequence,)
        assert tick.blue_command_sequences == (blue.command_sequence,)
        assert environment._require_match().status.frame_index == 1  # noqa: SLF001
    finally:
        environment.close()


def test_commands_on_opposite_sides_of_cutoff_target_adjacent_ticks(
    tmp_path: Path,
) -> None:
    environment, clock = _environment(tmp_path)
    try:
        reset = environment.reset(seed=42)
        clock.advance_ms(49)
        blue = environment.submit(
            reset.blue_binding,
            observation_version=reset.blue.observation_version,
            decision=SeatDecision(),
        )
        clock.advance_ms(2)
        red = environment.submit(
            reset.red_binding,
            observation_version=reset.red.observation_version,
            decision=SeatDecision(),
        )
        assert blue.target_tick == 0
        assert red.target_tick == 1
        first = environment.advance_tick()
        second = environment.advance_tick()
        assert first.blue_command_sequences == (blue.command_sequence,)
        assert first.red_command_sequences == ()
        assert second.red_command_sequences == (red.command_sequence,)
        assert second.blue_command_sequences == ()
    finally:
        environment.close()


def test_same_seat_later_decision_supersedes_only_that_seat(
    tmp_path: Path,
) -> None:
    environment, _ = _environment(tmp_path)
    try:
        reset = environment.reset(seed=42)
        first = environment.submit(
            reset.red_binding,
            observation_version=reset.red.observation_version,
            decision=SeatDecision.single(SeatOperation("WAIT", (1,))),
        )
        second = environment.submit(
            reset.red_binding,
            observation_version=reset.red.observation_version,
            decision=SeatDecision(),
        )
        blue = environment.submit(
            reset.blue_binding,
            observation_version=reset.blue.observation_version,
            decision=SeatDecision(),
        )
        tick = environment.advance_tick()
        events = environment.command_events
        assert events[first.command_sequence].status is CommandStatus.SUPERSEDED
        assert events[second.command_sequence].status is CommandStatus.APPLIED
        assert events[blue.command_sequence].status is CommandStatus.APPLIED
        assert tick.red_command_sequences == (second.command_sequence,)
        assert tick.blue_command_sequences == (blue.command_sequence,)
    finally:
        environment.close()


def test_observation_versions_are_side_bound_bounded_and_revalidated(
    tmp_path: Path,
) -> None:
    environment, _ = _environment(tmp_path, observation_history_size=2)
    try:
        reset = environment.reset(seed=42)
        cross = environment.submit(
            reset.red_binding,
            observation_version=reset.blue.observation_version,
            decision=SeatDecision(),
        )
        future = environment.submit(
            reset.red_binding,
            observation_version=999,
            decision=SeatDecision(),
        )
        assert cross.accepted is False
        assert cross.error_code == "observation_version_side_mismatch"
        assert future.accepted is False
        assert future.error_code == "observation_version_future"

        old = reset.red.observation_version
        environment.advance_tick()
        still_known = environment.submit(
            reset.red_binding,
            observation_version=old,
            decision=SeatDecision(),
        )
        assert still_known.accepted is True
        environment.advance_tick()
        expired = environment.submit(
            reset.red_binding,
            observation_version=old,
            decision=SeatDecision(),
        )
        assert expired.accepted is False
        assert expired.error_code == "observation_version_expired_or_unknown"
    finally:
        environment.close()


def test_stale_operation_is_rejected_at_apply_without_blocking_tick(
    tmp_path: Path,
) -> None:
    environment, _ = _environment(tmp_path)
    try:
        reset = environment.reset(seed=42)
        rejected = environment.submit(
            reset.red_binding,
            observation_version=reset.red.observation_version,
            decision=SeatDecision.single(SeatOperation("STOP_MOVE", (1,))),
        )
        blue = environment.submit(
            reset.blue_binding,
            observation_version=reset.blue.observation_version,
            decision=SeatDecision(),
        )
        tick = environment.advance_tick()
        events = environment.command_events
        assert events[rejected.command_sequence].status is CommandStatus.REJECTED_AT_APPLY
        assert events[rejected.command_sequence].error_code == "preview_not_executable"
        assert events[blue.command_sequence].status is CommandStatus.APPLIED
        assert tick.red_command_sequences == ()
        assert tick.blue_command_sequences == (blue.command_sequence,)
        assert tick.frame_index == 1
    finally:
        environment.close()


def test_binding_seat_is_server_authoritative_and_payload_cannot_switch_it(
    tmp_path: Path,
) -> None:
    environment, _ = _environment(tmp_path)
    try:
        reset = environment.reset(seed=42)
        forged = RealtimeSeatBinding(reset.red_binding.binding_id, "blue")
        receipt = environment.submit(
            forged,
            observation_version=reset.red.observation_version,
            decision=SeatDecision(),
        )
        assert receipt.accepted is False
        assert receipt.seat == "red"
        assert receipt.error_code == "realtime_binding_side_mismatch"
        with pytest.raises(RealtimeAgentSessionError, match="binding_unknown"):
            environment.submit(
                RealtimeSeatBinding("unknown-binding", "red"),
                observation_version=reset.red.observation_version,
                decision=SeatDecision(),
            )
    finally:
        environment.close()


def test_concurrent_submit_only_mutates_queue_and_tick_advances_once(
    tmp_path: Path,
) -> None:
    environment, _ = _environment(tmp_path)
    try:
        reset = environment.reset(seed=42)
        scheduler = environment._require_match().gateway.scheduler  # noqa: SLF001
        scheduler.advance_window = Mock(  # type: ignore[method-assign]
            wraps=scheduler.advance_window
        )

        def send(pair):
            binding, version = pair
            return environment.submit(
                binding,
                observation_version=version,
                decision=SeatDecision(),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = tuple(pool.map(send, (
                (reset.red_binding, reset.red.observation_version),
                (reset.blue_binding, reset.blue.observation_version),
            )))
        assert all(value.accepted for value in receipts)
        assert scheduler.advance_window.call_count == 0  # type: ignore[attr-defined]
        environment.advance_tick()
        assert scheduler.advance_window.call_count == 1  # type: ignore[attr-defined]
    finally:
        environment.close()


def test_submit_during_tick_processing_is_routed_to_next_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _ = _environment(tmp_path)
    entered = Event()
    release = Event()
    try:
        reset = environment.reset(seed=42)
        service = environment._services["red"]  # noqa: SLF001
        original = service.preview_decision

        def blocking_preview(*args, **kwargs):
            entered.set()
            assert release.wait(2)
            return original(*args, **kwargs)

        monkeypatch.setattr(service, "preview_decision", blocking_preview)
        result = []
        thread = Thread(target=lambda: result.append(environment.advance_tick()))
        thread.start()
        assert entered.wait(2)
        receipt = environment.submit(
            reset.blue_binding,
            observation_version=reset.blue.observation_version,
            decision=SeatDecision(),
        )
        assert receipt.target_tick == 1
        assert thread.is_alive()
        release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert result[0].tick_index == 0
        second = environment.advance_tick()
        assert second.blue_command_sequences == (receipt.command_sequence,)
    finally:
        release.set()
        environment.close()


def test_terminal_cancels_future_pending_command_and_stops_ticks(
    tmp_path: Path,
) -> None:
    environment, clock = _environment(
        tmp_path, tick_interval_ms=1_000, bundle=_max_tick_bundle(1)
    )
    try:
        reset = environment.reset(seed=42)
        current = environment.submit(
            reset.red_binding,
            observation_version=reset.red.observation_version,
            decision=SeatDecision(),
        )
        clock.advance_ms(1_001)
        future = environment.submit(
            reset.blue_binding,
            observation_version=reset.blue.observation_version,
            decision=SeatDecision(),
        )
        result = environment.advance_tick()
        assert result.truncated is True
        assert result.terminated is False
        events = environment.command_events
        assert events[current.command_sequence].status is CommandStatus.APPLIED
        assert events[future.command_sequence].status is CommandStatus.CANCELLED_ON_TERMINAL
        with pytest.raises(RealtimeAgentSessionError, match="session_ended"):
            environment.advance_tick()
    finally:
        environment.close()


def test_native_terminal_cancels_future_command_without_post_terminal_frame(
    tmp_path: Path,
) -> None:
    environment, clock = _environment(
        tmp_path, tick_interval_ms=1_000, bundle=_terminal_bundle()
    )
    try:
        reset = environment.reset(seed=42)
        current = environment.submit(
            reset.red_binding,
            observation_version=reset.red.observation_version,
            decision=SeatDecision(),
        )
        clock.advance_ms(1_001)
        future = environment.submit(
            reset.blue_binding,
            observation_version=reset.blue.observation_version,
            decision=SeatDecision(),
        )
        result = environment.advance_tick()
        assert result.terminated is True
        assert result.truncated is False
        assert result.winner == "red"
        assert result.frame_index == 1
        events = environment.command_events
        assert events[current.command_sequence].status is CommandStatus.APPLIED
        assert (
            events[future.command_sequence].status
            is CommandStatus.CANCELLED_ON_TERMINAL
        )
        with pytest.raises(RealtimeAgentSessionError, match="session_ended"):
            environment.advance_tick()
        assert len(environment.tick_results) == 1
    finally:
        environment.close()


def test_move_persists_without_new_command_and_stop_move_is_explicit(
    tmp_path: Path,
) -> None:
    environment, _ = _environment(tmp_path, tick_interval_ms=1_000)
    try:
        reset = environment.reset(seed=42)
        move = environment.submit(
            reset.red_binding,
            observation_version=reset.red.observation_version,
            decision=SeatDecision.single(
                SeatOperation("MOVE", (1,), destination_hex=105)
            ),
        )
        first = environment.advance_tick()
        first_unit = next(
            value for value in first.red.observation["friendly_operators"]
            if value["operator_id"] == 1
        )
        second = environment.advance_tick()
        second_unit = next(
            value for value in second.red.observation["friendly_operators"]
            if value["operator_id"] == 1
        )
        assert environment.command_events[move.command_sequence].status is CommandStatus.APPLIED
        assert first_unit["state"] == "MOVING"
        assert second_unit["state"] == "MOVING"
        assert second.red_command_sequences == ()

        stop = environment.submit(
            reset.red_binding,
            observation_version=second.red.observation_version,
            decision=SeatDecision.single(SeatOperation("STOP_MOVE", (1,))),
        )
        stopped = environment.advance_tick()
        assert environment.command_events[stop.command_sequence].status is CommandStatus.APPLIED
        assert stopped.red_command_sequences == (stop.command_sequence,)
    finally:
        environment.close()


def test_realtime_requires_one_frame_synchronous_internal_gateway(
    tmp_path: Path,
) -> None:
    asynchronous = RealtimeAgentSession(
        MatchFactory(tmp_path / "deadline", deadline_mode="server_monotonic"),
        run_namespace="invalid-deadline",
        tick_interval_ms=50,
        clock=ManualRealtimeClock(),
    )
    mismatch = RealtimeAgentSession(
        MatchFactory(tmp_path / "schedule", tick_interval_ms=100),
        run_namespace="invalid-schedule",
        tick_interval_ms=50,
        clock=ManualRealtimeClock(),
    )
    try:
        with pytest.raises(RealtimeAgentSessionError, match="requires_synchronous"):
            asynchronous.reset(seed=42)
        with pytest.raises(RealtimeAgentSessionError, match="schedule_mismatch"):
            mismatch.reset(seed=42)
    finally:
        asynchronous.close()
        mismatch.close()


def test_returned_observation_and_decision_are_detached(tmp_path: Path) -> None:
    environment, _ = _environment(tmp_path)
    try:
        reset = environment.reset(seed=42)
        original = deepcopy(reset.red.observation)
        reset.red.observation["mission"]["mutated"] = True
        decision = SeatDecision()
        environment.submit(
            reset.red_binding,
            observation_version=reset.red.observation_version,
            decision=decision,
        )
        assert environment.latest_view(reset.red_binding).observation == original
        record = environment.command_events[0].to_record()
        assert "green" not in record
        assert "frame_analysis" not in record
        assert "world_state" not in record
    finally:
        environment.close()
