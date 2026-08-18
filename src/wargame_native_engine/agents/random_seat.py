"""Deterministic random controller over the public product-seat protocol."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from wargame_native_engine.agents.protocol import AgentResetContext
from wargame_native_engine.agents.seat import (
    AgentDecisionContext,
    SeatDecision,
    SeatOperation,
)
from wargame_native_engine.core.rng import NativeRNG
from wargame_native_engine.io.serialization import canonical_hash


_PRODUCT_ACTION_TYPES = frozenset({
    "MOVE",
    "SHOOT",
    "WEAPON_LOCK",
    "WEAPON_UNFOLD",
})


class RandomProductSeatAgent:
    """Choose one random legal product operation from a side-only snapshot.

    The frozen low-level action records are used only as server-issued
    capabilities from which a high-level :class:`SeatOperation` is derived.
    The Agent never returns an action ID or a movement path: MOVE retains only
    the endpoint and is planned and validated again by ProductOperationService
    at the authoritative apply tick.  STOP_MOVE is deliberately not sampled:
    once a random MOVE starts, empty later decisions let the Engine's ongoing
    movement state advance instead of immediately cancelling it.
    """

    def __init__(
        self,
        agent_id: str = "random-product-seat-agent",
        *,
        seed: int = 0,
    ) -> None:
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")
        if type(seed) is not int:
            raise TypeError("seed must be an exact int")
        self.agent_id = agent_id.strip()
        self.seed = seed
        self._rng = NativeRNG(seed)

    def reset(self, context: AgentResetContext) -> None:
        if not isinstance(context, AgentResetContext):
            raise TypeError("context must be an AgentResetContext")
        derived = int(canonical_hash({
            "agent_seed": self.seed,
            "episode_seed": context.seed,
            "side": context.side,
        })[:16], 16)
        self._rng = NativeRNG(derived)

    def act(self, *, context: AgentDecisionContext) -> SeatDecision:
        if not isinstance(context, AgentDecisionContext):
            raise TypeError("context must be an AgentDecisionContext")
        legal_ids = {
            value
            for value in context.observation.get("legal_action_ids", ())
            if isinstance(value, str)
        }
        allowed_types = set(context.operation_capabilities.operation_types)
        controllable = set(
            context.operation_capabilities.controllable_actor_ids
        )
        candidates: list[tuple[tuple[object, ...], SeatOperation]] = []
        for record in context.operation_capabilities.frozen_actions:
            if not isinstance(record, Mapping):
                continue
            action_id = record.get("action_id")
            if legal_ids and action_id not in legal_ids:
                continue
            action = record.get("action")
            if not isinstance(action, Mapping):
                continue
            operation = _product_operation(
                action,
                expected_side=context.side,
                allowed_types=allowed_types,
                controllable_actor_ids=controllable,
            )
            if operation is None:
                continue
            candidates.append((
                (
                    operation.operation_type,
                    operation.actor_ids,
                    -1 if operation.target_id is None else operation.target_id,
                    -1 if operation.destination_hex is None
                    else operation.destination_hex,
                    "" if not isinstance(action_id, str) else action_id,
                ),
                operation,
            ))
        if not candidates:
            return SeatDecision()
        candidates.sort(key=lambda value: value[0])
        return SeatDecision.single(
            candidates[self._rng.randbelow(len(candidates))][1]
        )

    def close(self) -> None:
        return None


def _product_operation(
    action: Mapping[str, Any],
    *,
    expected_side: str,
    allowed_types: set[str],
    controllable_actor_ids: set[int],
) -> SeatOperation | None:
    if action.get("side") != expected_side:
        return None
    operation_type = action.get("action_type")
    if (
        not isinstance(operation_type, str)
        or operation_type not in _PRODUCT_ACTION_TYPES
        or operation_type not in allowed_types
    ):
        return None
    actor_id = action.get("actor_id")
    if type(actor_id) is not int or actor_id not in controllable_actor_ids:
        return None
    parameters = action.get("parameters", {})
    if not isinstance(parameters, Mapping):
        return None
    if operation_type == "MOVE":
        path = parameters.get("path")
        if (
            isinstance(path, (str, bytes))
            or not isinstance(path, Sequence)
            or not path
            or type(path[-1]) is not int
        ):
            return None
        movement_mode = parameters.get("movement_mode", "NORMAL")
        if not isinstance(movement_mode, str) or not movement_mode:
            return None
        return SeatOperation(
            "MOVE",
            (actor_id,),
            destination_hex=path[-1],
            movement_mode=movement_mode,
        )
    if operation_type == "SHOOT":
        target_id = action.get("target_id")
        if type(target_id) is not int:
            return None
        weapon_id = parameters.get("weapon_id")
        choices = () if type(weapon_id) is not int else ((actor_id, weapon_id),)
        return SeatOperation(
            "SHOOT",
            (actor_id,),
            target_id=target_id,
            weapon_choices=choices,
        )
    return SeatOperation(operation_type, (actor_id,))
