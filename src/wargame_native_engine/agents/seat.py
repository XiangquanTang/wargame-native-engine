"""Detached local-Agent seat contract over product operations.

The values in this module describe controller intent only.  They never carry
``EngineAction`` objects, server action identifiers, GREEN frames, or mutable
session services.  A runner must resolve every decision through the existing
``ProductOperationService`` before it can reach a decision window.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from wargame_native_engine.agents.protocol import AgentResetContext


@dataclass(frozen=True, slots=True)
class AgentDecisionContext:
    """One detached RED or BLUE product-decision input."""

    side: str
    decision_index: int
    frame_index: int
    sim_time_ms: int
    observation: Mapping[str, Any]
    operation_capabilities: Mapping[str, Any]

    @property
    def perspective(self) -> str:
        return self.side.upper()


@dataclass(frozen=True, slots=True)
class SeatOperation:
    """Transport-independent input to ``preview_operation``.

    Paths and low-level actions are deliberately absent.  MOVE supplies only
    a destination request; the server planner owns the resulting path.
    """

    operation_type: str
    actor_ids: tuple[int, ...] = ()
    destination_hex: int | None = None
    target_id: int | None = None
    movement_mode: str = "NORMAL"
    weapon_choices: tuple[tuple[int, int], ...] = ()
    whole_side_wait: bool = False
    confirm_partial: bool = False

    def __post_init__(self) -> None:
        operation = _nonempty_text(self.operation_type, "operation_type").upper()
        actors = _exact_nonnegative_ints(self.actor_ids, "actor_ids")
        choices = _weapon_choices(self.weapon_choices)
        if self.destination_hex is not None:
            _exact_nonnegative_int(self.destination_hex, "destination_hex")
        if self.target_id is not None:
            _exact_nonnegative_int(self.target_id, "target_id")
        if type(self.whole_side_wait) is not bool:
            raise TypeError("whole_side_wait must be bool")
        if type(self.confirm_partial) is not bool:
            raise TypeError("confirm_partial must be bool")
        object.__setattr__(self, "operation_type", operation)
        object.__setattr__(self, "actor_ids", actors)
        object.__setattr__(self, "movement_mode", _nonempty_text(
            self.movement_mode, "movement_mode"
        ).upper())
        object.__setattr__(self, "weapon_choices", choices)

    @classmethod
    def wait(cls) -> "SeatOperation":
        """Explicit whole-side NoOp through the product WAIT contract."""

        return cls("WAIT", whole_side_wait=True)

    def preview_arguments(self) -> dict[str, object]:
        return {
            "actor_ids": self.actor_ids,
            "operation_type": self.operation_type,
            "destination_hex": self.destination_hex,
            "target_id": self.target_id,
            "movement_mode": self.movement_mode,
            "weapon_choices": dict(self.weapon_choices),
            "whole_side_wait": self.whole_side_wait,
        }


class LocalSeatAgent(Protocol):
    """A deterministic in-process controller for one incomplete-information seat."""

    agent_id: str

    def reset(self, context: AgentResetContext) -> None: ...

    def act(self, *, context: AgentDecisionContext) -> SeatOperation: ...

    def close(self) -> None: ...


class WaitSeatAgent:
    """Deterministically submit an explicit whole-side WAIT every window."""

    def __init__(self, agent_id: str = "wait-seat-agent") -> None:
        self.agent_id = _nonempty_text(agent_id, "agent_id")

    def reset(self, context: AgentResetContext) -> None:
        del context

    def act(self, *, context: AgentDecisionContext) -> SeatOperation:
        del context
        return SeatOperation.wait()

    def close(self) -> None:
        return None


class ScriptedSeatAgent:
    """Return one immutable product operation per decision window."""

    def __init__(
        self,
        agent_id: str,
        script: Sequence[SeatOperation],
        *,
        wait_when_exhausted: bool = True,
    ) -> None:
        self.agent_id = _nonempty_text(agent_id, "agent_id")
        if isinstance(script, (str, bytes)) or not isinstance(script, Sequence):
            raise TypeError("script must be a sequence of SeatOperation values")
        if not all(isinstance(item, SeatOperation) for item in script):
            raise TypeError("script must contain only SeatOperation values")
        if type(wait_when_exhausted) is not bool:
            raise TypeError("wait_when_exhausted must be bool")
        self._script = tuple(deepcopy(item) for item in script)
        self._wait_when_exhausted = wait_when_exhausted
        self._index = 0

    def reset(self, context: AgentResetContext) -> None:
        del context
        self._index = 0

    def act(self, *, context: AgentDecisionContext) -> SeatOperation:
        del context
        if self._index >= len(self._script):
            if self._wait_when_exhausted:
                return SeatOperation.wait()
            raise RuntimeError("scripted_seat_agent_exhausted")
        result = self._script[self._index]
        self._index += 1
        return deepcopy(result)

    def close(self) -> None:
        return None


def _exact_nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise TypeError(f"{field} must be a non-negative exact int")
    return value


def _exact_nonnegative_ints(
    values: Sequence[int], field: str,
) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{field} must be a sequence")
    result = tuple(_exact_nonnegative_int(value, field) for value in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def _weapon_choices(
    values: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("weapon_choices must be a sequence")
    result: list[tuple[int, int]] = []
    seen: set[int] = set()
    for value in values:
        if not isinstance(value, tuple) or len(value) != 2:
            raise TypeError("weapon_choices entries must be (unit_id, weapon_id)")
        unit_id = _exact_nonnegative_int(value[0], "weapon choice unit_id")
        weapon_id = _exact_nonnegative_int(value[1], "weapon choice weapon_id")
        if unit_id in seen:
            raise ValueError("weapon_choices must not repeat unit_id")
        seen.add(unit_id)
        result.append((unit_id, weapon_id))
    return tuple(sorted(result))


def _nonempty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field} must be a non-empty string")
    return value.strip()
