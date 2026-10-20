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
from wargame_native_engine.io.serialization import json_clone
from wargame_native_engine.projection.decision_capability import (
    ActionCatalogSnapshot,
)
from wargame_native_engine.projection.decision_semantic import (
    DecisionProjectionContractError,
    SideDecisionSemanticProjection,
)


@dataclass(frozen=True, slots=True)
class SeatActionCapabilities:
    """Detached, side-only capabilities for one decision window.

    The dataclass fixes the public description, while nested JSON values are
    detached copies rather than claims of recursive immutability.
    """

    side: str
    decision_index: int
    controllable_actor_ids: tuple[int, ...]
    operation_types: tuple[str, ...]
    frozen_actions: tuple[Mapping[str, Any], ...]
    operation_summary: Mapping[str, Any]

    def __post_init__(self) -> None:
        side = _nonempty_text(self.side, "side").lower()
        if side not in {"red", "blue"}:
            raise ValueError("side must be RED or BLUE")
        decision = _exact_nonnegative_int(
            self.decision_index, "decision_index"
        )
        actors = _exact_nonnegative_ints(
            self.controllable_actor_ids, "controllable_actor_ids"
        )
        operation_types = tuple(
            _nonempty_text(value, "operation_type").upper()
            for value in self.operation_types
        )
        if len(set(operation_types)) != len(operation_types):
            raise ValueError("operation_types must not contain duplicates")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "decision_index", decision)
        object.__setattr__(self, "controllable_actor_ids", actors)
        object.__setattr__(self, "operation_types", operation_types)
        object.__setattr__(
            self, "frozen_actions", tuple(json_clone(self.frozen_actions))
        )
        object.__setattr__(
            self, "operation_summary", json_clone(self.operation_summary)
        )

    def to_record(self) -> dict[str, object]:
        return {
            "side": self.side,
            "decision_index": self.decision_index,
            "controllable_actor_ids": list(self.controllable_actor_ids),
            "operation_types": list(self.operation_types),
            "frozen_actions": json_clone(self.frozen_actions),
            "operation_summary": json_clone(self.operation_summary),
        }


@dataclass(frozen=True, slots=True)
class AgentDecisionContext:
    """One detached RED or BLUE product-decision input."""

    side: str
    decision_index: int
    frame_index: int
    sim_time_ms: int
    observation: Mapping[str, Any]
    operation_capabilities: SeatActionCapabilities
    decision_semantic: SideDecisionSemanticProjection | None = None
    action_catalog: ActionCatalogSnapshot | None = None

    def __post_init__(self) -> None:
        if (self.decision_semantic is None) != (self.action_catalog is None):
            raise DecisionProjectionContractError(
                "Agent decision semantic/catalog must be supplied together"
            )
        if self.decision_semantic is None:
            return
        decision = self.decision_semantic
        catalog = self.action_catalog
        if type(decision) is not SideDecisionSemanticProjection:
            raise TypeError("decision_semantic must be a Decision projection")
        if type(catalog) is not ActionCatalogSnapshot:
            raise TypeError("action_catalog must be an ActionCatalogSnapshot")
        if (
            decision.snapshot.side.value != self.side
            or decision.snapshot.decision_window_index != self.decision_index
            or decision.snapshot.frame_index != self.frame_index
            or decision.snapshot.sim_time_ms != self.sim_time_ms
            or decision.action_space != catalog.reference()
        ):
            raise DecisionProjectionContractError(
                "Agent decision context snapshot identity mismatch"
            )

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
    target_hex: int | None = None
    target_id: int | None = None
    movement_mode: str = "NORMAL"
    weapon_choices: tuple[tuple[int, int], ...] = ()
    whole_side_wait: bool = False
    confirm_partial: bool = False
    fire_mode: str | None = None
    guider_id: int | None = None
    shooter_id: int | None = None
    weapon_id: int | None = None

    def __post_init__(self) -> None:
        operation = _nonempty_text(self.operation_type, "operation_type").upper()
        actors = _exact_nonnegative_ints(self.actor_ids, "actor_ids")
        choices = _weapon_choices(self.weapon_choices)
        if choices and operation != "SHOOT":
            raise ValueError("weapon_choices are valid only for SHOOT")
        if any(unit_id not in actors for unit_id, _ in choices):
            raise ValueError("weapon_choices must belong to selected actors")
        if self.destination_hex is not None:
            _exact_nonnegative_int(self.destination_hex, "destination_hex")
        if self.target_hex is not None:
            _exact_nonnegative_int(self.target_hex, "target_hex")
        if self.target_id is not None:
            _exact_nonnegative_int(self.target_id, "target_id")
        if type(self.whole_side_wait) is not bool:
            raise TypeError("whole_side_wait must be bool")
        if type(self.confirm_partial) is not bool:
            raise TypeError("confirm_partial must be bool")
        fire_values = (
            self.fire_mode,
            self.guider_id,
            self.shooter_id,
            self.weapon_id,
        )
        if operation == "GUIDED_FIRE":
            if self.fire_mode != "GUIDED":
                raise ValueError("GUIDED_FIRE requires fire_mode GUIDED")
            for name, value in (
                ("guider_id", self.guider_id),
                ("shooter_id", self.shooter_id),
                ("weapon_id", self.weapon_id),
            ):
                _exact_nonnegative_int(value, name)
            if actors != (self.guider_id,):
                raise ValueError(
                    "GUIDED_FIRE actor_ids must contain exactly guider_id"
                )
            if self.target_hex is not None:
                raise ValueError("GUIDED_FIRE does not accept target_hex")
        elif operation == "INDIRECT_FIRE":
            if self.fire_mode != "INDIRECT":
                raise ValueError("INDIRECT_FIRE requires fire_mode INDIRECT")
            _exact_nonnegative_int(self.shooter_id, "shooter_id")
            _exact_nonnegative_int(self.weapon_id, "weapon_id")
            _exact_nonnegative_int(self.target_hex, "target_hex")
            if actors != (self.shooter_id,):
                raise ValueError(
                    "INDIRECT_FIRE actor_ids must contain exactly shooter_id"
                )
            if self.guider_id is not None or self.target_id is not None:
                raise ValueError(
                    "INDIRECT_FIRE accepts target_hex only and no guider"
                )
        elif operation in {"BOARD", "UNLOAD", "CAPTURE"}:
            if len(actors) != 1:
                raise ValueError(f"{operation} requires exactly one actor")
            _exact_nonnegative_int(self.target_id, "target_id")
        elif any(value is not None for value in fire_values):
            raise ValueError(
                "fire contract fields require GUIDED_FIRE or INDIRECT_FIRE"
            )
        elif self.target_hex is not None:
            raise ValueError("target_hex is valid only for INDIRECT_FIRE")
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
        result: dict[str, object] = {
            "actor_ids": self.actor_ids,
            "operation_type": self.operation_type,
            "destination_hex": self.destination_hex,
            "target_id": self.target_id,
            "movement_mode": self.movement_mode,
            "weapon_choices": dict(self.weapon_choices),
            "whole_side_wait": self.whole_side_wait,
        }
        if self.operation_type == "GUIDED_FIRE":
            result.update({
                "fire_mode": self.fire_mode,
                "guider_id": self.guider_id,
                "shooter_id": self.shooter_id,
                "weapon_id": self.weapon_id,
            })
        elif self.operation_type == "INDIRECT_FIRE":
            result.update({
                "target_hex": self.target_hex,
                "fire_mode": self.fire_mode,
                "shooter_id": self.shooter_id,
                "weapon_id": self.weapon_id,
            })
        return result


@dataclass(frozen=True, slots=True)
class SeatDecision:
    """One side's complete, ordered intent for one decision window."""

    operations: tuple[SeatOperation, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.operations, (str, bytes)) or not isinstance(
            self.operations, Sequence
        ):
            raise TypeError("operations must be a sequence of SeatOperation values")
        operations = tuple(self.operations)
        if not all(isinstance(item, SeatOperation) for item in operations):
            raise TypeError("operations must contain only SeatOperation values")
        actors: set[int] = set()
        for operation in operations:
            if operation.whole_side_wait:
                if len(operations) != 1:
                    raise ValueError(
                        "whole-side WAIT must be the only SeatDecision operation"
                    )
                continue
            for actor_id in operation.actor_ids:
                if actor_id in actors:
                    raise ValueError(
                        f"actor {actor_id} occurs in multiple SeatDecision operations"
                    )
                actors.add(actor_id)
        object.__setattr__(self, "operations", operations)

    @classmethod
    def single(cls, operation: SeatOperation) -> "SeatDecision":
        if not isinstance(operation, SeatOperation):
            raise TypeError("operation must be a SeatOperation")
        return cls((operation,))

    def to_record(self) -> dict[str, object]:
        return {
            "operations": [item.preview_arguments() for item in self.operations],
        }


class LocalSeatAgent(Protocol):
    """A deterministic in-process controller for one incomplete-information seat."""

    agent_id: str

    def reset(self, context: AgentResetContext) -> None: ...

    def act(self, *, context: AgentDecisionContext) -> SeatDecision: ...

    def close(self) -> None: ...


class WaitSeatAgent:
    """Deterministically submit an explicit whole-side WAIT every window."""

    def __init__(self, agent_id: str = "wait-seat-agent") -> None:
        self.agent_id = _nonempty_text(agent_id, "agent_id")

    def reset(self, context: AgentResetContext) -> None:
        del context

    def act(self, *, context: AgentDecisionContext) -> SeatDecision:
        del context
        return SeatDecision()

    def close(self) -> None:
        return None


class ScriptedSeatAgent:
    """Return one immutable seat decision per decision window."""

    def __init__(
        self,
        agent_id: str,
        script: Sequence[SeatDecision],
        *,
        wait_when_exhausted: bool = True,
    ) -> None:
        self.agent_id = _nonempty_text(agent_id, "agent_id")
        if isinstance(script, (str, bytes)) or not isinstance(script, Sequence):
            raise TypeError("script must be a sequence of SeatDecision values")
        if not all(isinstance(item, SeatDecision) for item in script):
            raise TypeError("script must contain only SeatDecision values")
        if type(wait_when_exhausted) is not bool:
            raise TypeError("wait_when_exhausted must be bool")
        self._script = tuple(deepcopy(item) for item in script)
        self._wait_when_exhausted = wait_when_exhausted
        self._index = 0

    def reset(self, context: AgentResetContext) -> None:
        del context
        self._index = 0

    def act(self, *, context: AgentDecisionContext) -> SeatDecision:
        del context
        if self._index >= len(self._script):
            if self._wait_when_exhausted:
                return SeatDecision()
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
