from __future__ import annotations

from dataclasses import dataclass, field
import heapq
from typing import Any, Mapping

from wargame_native_engine.io.serialization import canonical_hash, json_clone


@dataclass(frozen=True, slots=True)
class ActionTimer:
    submitted_at: int
    duration: int

    def __post_init__(self) -> None:
        if self.submitted_at < 0 or self.duration < 0:
            raise ValueError("action timing values cannot be negative")

    @property
    def completes_at(self) -> int:
        return self.submitted_at + self.duration


@dataclass(order=True, frozen=True, slots=True)
class ScheduledCommand:
    event_time: int
    arrival_index: int
    action_id: str
    submitted_at: int = field(compare=False)
    payload: Mapping[str, Any] = field(compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {"event_time": self.event_time,
                "arrival_index": self.arrival_index,
                "action_id": self.action_id,
                "submitted_at": self.submitted_at,
                "payload": json_clone(self.payload)}


class EventQueue:
    def __init__(self) -> None:
        self._values: list[ScheduledCommand] = []

    def push(self, command: ScheduledCommand) -> None:
        heapq.heappush(self._values, command)

    def pop_ready(self, event_time: int) -> tuple[ScheduledCommand, ...]:
        values: list[ScheduledCommand] = []
        while self._values and self._values[0].event_time <= event_time:
            values.append(heapq.heappop(self._values))
        return tuple(values)

    def __len__(self) -> int:
        return len(self._values)


class CommandScheduler:
    """Deterministic first-arrival-first-adjudication scheduling boundary."""

    def __init__(self) -> None:
        self.queue = EventQueue()
        self._arrival_index = 0

    def submit(self, payload: Mapping[str, Any], *, submitted_at: int,
               duration: int = 0) -> ScheduledCommand:
        timer = ActionTimer(int(submitted_at), int(duration))
        action_id = "scheduled-" + canonical_hash({
            "payload": payload, "submitted_at": timer.submitted_at,
            "duration": timer.duration, "arrival_index": self._arrival_index,
        })[:24]
        command = ScheduledCommand(timer.completes_at, self._arrival_index,
                                   action_id, timer.submitted_at,
                                   json_clone(payload))
        self._arrival_index += 1
        self.queue.push(command)
        return command

    def advance_to(self, event_time: int) -> tuple[ScheduledCommand, ...]:
        if event_time < 0:
            raise ValueError("event_time cannot be negative")
        return self.queue.pop_ready(int(event_time))
