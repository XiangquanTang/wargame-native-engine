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
from wargame_native_engine.projection.decision_semantic import (
    CapabilityAvailabilityStatus,
    DecisionCapabilityFamily,
)


_PRODUCT_ACTION_TYPES = frozenset({
    "MOVE",
    "SHOOT",
    "BOARD",
    "UNLOAD",
    "CAPTURE",
    "ENTER_CONCEALMENT",
    "WEAPON_LOCK",
    "WEAPON_UNLOCK",
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
        if context.decision_semantic is not None and context.action_catalog is not None:
            allowed_types = _available_operation_types(context)
            controllable = {
                value.operator_reference
                for value in context.decision_semantic.own_operators
            }
            action_records = tuple(
                _catalog_action_record(value, context.side)
                for value in context.action_catalog.entries
            )
        else:
            allowed_types = set(context.operation_capabilities.operation_types)
            controllable = set(
                context.operation_capabilities.controllable_actor_ids
            )
            action_records = context.operation_capabilities.frozen_actions
        candidates: list[tuple[tuple[object, ...], SeatOperation]] = []
        for record in action_records:
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


def _available_operation_types(context: AgentDecisionContext) -> set[str]:
    result: set[str] = set()
    assert context.decision_semantic is not None
    for operator in context.decision_semantic.capabilities.operators:
        for value in operator.families:
            if value.status is not CapabilityAvailabilityStatus.AVAILABLE:
                continue
            result.add(
                "SHOOT"
                if value.family is DecisionCapabilityFamily.DIRECT_FIRE
                else value.family.value
            )
    return result


def _catalog_action_record(value, side: str) -> dict[str, object]:
    return {
        "action_id": value.action_id,
        "action": {
            "side": side,
            "actor_id": value.actor_reference,
            "action_type": value.action_type,
            "target_id": value.target_reference,
            "target_position": value.target_position,
            "parameters": value.parameters(),
        },
    }


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
    if operation_type == "WEAPON_LOCK":
        weapon_id = parameters.get("weapon_id")
        if type(weapon_id) is not int:
            return None
        return SeatOperation("WEAPON_LOCK", (actor_id,), weapon_choices=((actor_id, weapon_id),))
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
    if operation_type in {"BOARD", "UNLOAD", "CAPTURE"}:
        target_id = action.get("target_id")
        if type(target_id) is not int:
            return None
        return SeatOperation(
            operation_type,
            (actor_id,),
            target_id=target_id,
        )
    return SeatOperation(operation_type, (actor_id,))
