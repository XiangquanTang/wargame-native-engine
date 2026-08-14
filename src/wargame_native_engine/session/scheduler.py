"""Decision-period scheduling over the sole SessionManager advance path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from wargame_native_engine.core.action import EngineAction


@dataclass(frozen=True, slots=True)
class SimulationSchedule:
    decision_interval_ms: int
    frame_interval_ms: int

    def __post_init__(self) -> None:
        for name, value in (
            ("decision_interval_ms", self.decision_interval_ms),
            ("frame_interval_ms", self.frame_interval_ms),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive exact int")
        if self.decision_interval_ms % self.frame_interval_ms:
            raise ValueError(
                "decision_interval_ms must be divisible by frame_interval_ms"
            )

    @property
    def frames_per_decision(self) -> int:
        return self.decision_interval_ms // self.frame_interval_ms


@dataclass(frozen=True, slots=True)
class SimulationAdvance:
    decision_window_index: int
    first_frame_index: int
    last_frame_index: int
    generated_frame_count: int
    sim_time_before_ms: int
    sim_time_after_ms: int
    terminal: bool


class SimulationScheduler:
    """Advance a closed control window as one or more fact frames.

    This class never receives an Engine or WorldState.  It can only invoke the
    existing ``SessionManager.advance`` observer/recorder chain.
    """

    def __init__(self, manager, schedule: SimulationSchedule) -> None:
        self.manager = manager
        self.schedule = schedule

    def advance_window(
        self,
        session_id: str,
        *,
        decision_window_index: int,
        ordered_actions: Sequence[EngineAction],
    ) -> SimulationAdvance:
        before = self.manager.descriptor(session_id)
        first_frame = before.frame_index + 1
        sim_before = before.sim_time_ms
        current = before
        generated = 0
        seconds = self.schedule.frame_interval_ms / 1000
        for offset in range(self.schedule.frames_per_decision):
            current = self.manager.advance(
                session_id,
                tuple(ordered_actions) if offset == 0 else (),
                delta_time_seconds=seconds,
                decision_window_index=decision_window_index,
                frame_interval_offset=offset,
            )
            generated += 1
            if current.terminal:
                break
        return SimulationAdvance(
            decision_window_index=decision_window_index,
            first_frame_index=first_frame,
            last_frame_index=current.frame_index,
            generated_frame_count=generated,
            sim_time_before_ms=sim_before,
            sim_time_after_ms=current.sim_time_ms,
            terminal=current.terminal,
        )
