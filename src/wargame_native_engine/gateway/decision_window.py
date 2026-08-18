"""Deterministic, side-isolated decision-window authorization state."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Sequence

from wargame_native_engine.core.action import (
    ACTION_PRIORITY,
    EngineAction,
    ValidAction,
)
from wargame_native_engine.gateway.models import CommandGatewayError


class DecisionWindowState(str, Enum):
    OPEN = "OPEN"
    READY = "READY"
    CLOSED = "CLOSED"
    ADVANCING = "ADVANCING"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class SideActionSubmission:
    side: str
    request_id: str
    ordered_action_ids: tuple[str, ...]


@dataclass(slots=True)
class DecisionWindow:
    """One mutable window held exclusively under its Gateway session lock.

    Valid actions are detached at open time.  Callers can submit identifiers
    only; the eventual engine batch is resolved from these private snapshots.
    """

    session_id: str
    decision_window_index: int
    opened_at_sim_time_ms: int
    valid_action_snapshots: Mapping[str, Mapping[str, ValidAction]]
    deadline_policy: str = "synchronous"
    closes_at_sim_time_ms: int | None = None
    opened_at_server_monotonic: float | None = None
    deadline_at_server_monotonic: float | None = None
    lifecycle_state: DecisionWindowState = DecisionWindowState.OPEN
    close_reason: str | None = None
    submissions: dict[str, SideActionSubmission] = field(default_factory=dict)
    merged_action_batch: tuple[EngineAction, ...] = ()

    @classmethod
    def open(
        cls,
        *,
        session_id: str,
        decision_window_index: int,
        opened_at_sim_time_ms: int,
        red_valid_actions: Sequence[ValidAction],
        blue_valid_actions: Sequence[ValidAction],
        deadline_policy: str = "synchronous",
        opened_at_server_monotonic: float | None = None,
        deadline_at_server_monotonic: float | None = None,
    ) -> "DecisionWindow":
        return cls(
            session_id=str(session_id),
            decision_window_index=decision_window_index,
            opened_at_sim_time_ms=opened_at_sim_time_ms,
            valid_action_snapshots=MappingProxyType({
                "red": _freeze_side_actions("red", red_valid_actions),
                "blue": _freeze_side_actions("blue", blue_valid_actions),
            }),
            deadline_policy=str(deadline_policy),
            opened_at_server_monotonic=opened_at_server_monotonic,
            deadline_at_server_monotonic=deadline_at_server_monotonic,
        )

    def submit(
        self,
        *,
        side: str,
        request_id: str,
        ordered_action_ids: Sequence[str],
    ) -> None:
        if self.lifecycle_state is not DecisionWindowState.OPEN:
            raise CommandGatewayError(
                "decision_window_closed",
                f"window {self.decision_window_index} is {self.lifecycle_state.value}",
            )
        if side in self.submissions:
            raise CommandGatewayError(
                "side_already_submitted",
                f"{side.upper()} already submitted for window "
                f"{self.decision_window_index}",
            )
        action_ids = _validate_ordered_action_ids(ordered_action_ids)
        snapshot = self.valid_action_snapshots[side]
        actors: set[int] = set()
        for action_id in action_ids:
            value = snapshot.get(action_id)
            if value is None:
                raise CommandGatewayError(
                    "action_not_currently_valid",
                    f"action {action_id!r} is not a current {side.upper()} action "
                    "in the frozen window snapshot",
                )
            action = value.action
            if action.side != side:
                raise CommandGatewayError(
                    "action_side_mismatch",
                    f"action {action_id!r} belongs to {action.side!r}",
                )
            if action.actor_id is not None:
                if action.actor_id in actors:
                    raise CommandGatewayError(
                        "duplicate_actor_action",
                        f"actor {action.actor_id} has multiple actions in one window",
                    )
                actors.add(action.actor_id)
        self.submissions[side] = SideActionSubmission(
            side=side,
            request_id=request_id,
            ordered_action_ids=action_ids,
        )
        if set(self.submissions) == {"red", "blue"}:
            self.lifecycle_state = DecisionWindowState.READY

    def close(self, *, reason: str = "both_sides_submitted") -> None:
        if self.lifecycle_state is not DecisionWindowState.READY:
            raise CommandGatewayError(
                "decision_window_not_ready",
                f"window {self.decision_window_index} is not ready",
            )
        actions = []
        for side, submission in self.submissions.items():
            snapshot = self.valid_action_snapshots[side]
            actions.extend(
                deepcopy(snapshot[action_id].action)
                for action_id in submission.ordered_action_ids
            )
        self.merged_action_batch = tuple(sorted(actions, key=canonical_action_order_key))
        self.close_reason = str(reason)
        self.lifecycle_state = DecisionWindowState.CLOSED

    def expire(self) -> None:
        if self.lifecycle_state is not DecisionWindowState.READY:
            raise CommandGatewayError(
                "decision_window_not_ready",
                f"window {self.decision_window_index} is not ready for expiry",
            )
        self.close(reason="deadline")
        self.lifecycle_state = DecisionWindowState.EXPIRED

    def mark_advancing(self) -> None:
        if self.lifecycle_state not in {
            DecisionWindowState.CLOSED,
            DecisionWindowState.EXPIRED,
        }:
            raise RuntimeError("decision_window_not_closed")
        self.lifecycle_state = DecisionWindowState.ADVANCING

    def mark_completed(self) -> None:
        if self.lifecycle_state is not DecisionWindowState.ADVANCING:
            raise RuntimeError("decision_window_not_advancing")
        self.lifecycle_state = DecisionWindowState.COMPLETED

    def action_records(self, side: str) -> tuple[Mapping[str, object], ...]:
        snapshot = self.valid_action_snapshots[side]
        return tuple(deepcopy(value.to_dict()) for value in snapshot.values())

    def authorize_proposal(self, side: str, value: ValidAction) -> None:
        """Extend the frozen ID map with one server-generated proposal.

        Only a capability already present at window-open time may be extended.
        For destination MOVE proposals this means the actor had at least one
        Native MOVE capability in the original frozen action set.
        """

        if self.lifecycle_state is not DecisionWindowState.OPEN:
            raise CommandGatewayError(
                "decision_window_closed", "cannot authorize a closed proposal"
            )
        detached = deepcopy(value)
        action = detached.action
        if action.side != side:
            raise CommandGatewayError(
                "action_side_mismatch", "proposal belongs to another side"
            )
        if action.actor_id is None:
            raise CommandGatewayError(
                "proposal_actor_required", "proposal requires an actor"
            )
        snapshot = self.valid_action_snapshots[side]
        capability = any(
            item.action.actor_id == action.actor_id
            and item.action.action_type is action.action_type
            for item in snapshot.values()
        )
        if not capability:
            raise CommandGatewayError(
                "proposal_capability_not_frozen",
                f"actor {action.actor_id} lacks frozen {action.action_type.value}",
            )
        existing = snapshot.get(detached.action_id)
        if existing is not None:
            # A one-segment server-planned MOVE can be byte-for-byte the same
            # EngineAction already frozen at window open.  Its ValidAction
            # provenance changes from ``source_action_router`` to
            # ``operation_move_proposal``, but provenance is evidence rather
            # than action identity.  Reuse the frozen authorization when the
            # action and priority agree; never reject a legitimate adjacent
            # product MOVE merely because its source label differs.
            if (
                existing.action != detached.action
                or existing.priority != detached.priority
            ):
                raise CommandGatewayError(
                    "proposal_action_id_conflict", detached.action_id
                )
            return
        updated = dict(snapshot)
        updated[detached.action_id] = detached
        outer = dict(self.valid_action_snapshots)
        outer[side] = MappingProxyType(updated)
        self.valid_action_snapshots = MappingProxyType(outer)


def canonical_action_order_key(action: EngineAction) -> tuple[object, ...]:
    """Order a joint batch independently of submission arrival order.

    The key starts with the Native action priority already used by adjudication,
    then stable semantic identifiers.  Side is only a tie-breaker; this is not a
    network-order or unconditional RED-first policy.
    """

    return (
        ACTION_PRIORITY[action.action_type],
        action.action_type.value,
        -1 if action.actor_id is None else action.actor_id,
        action.side,
        -1 if action.target_id is None else action.target_id,
        () if action.target_position is None else action.target_position,
        action.action_id,
    )


def _freeze_side_actions(
    side: str,
    values: Sequence[ValidAction],
) -> Mapping[str, ValidAction]:
    frozen: dict[str, ValidAction] = {}
    for source in values:
        value = deepcopy(source)
        if value.action.side != side:
            raise ValueError(f"valid_action_side_mismatch:{side}")
        action_id = value.action_id
        if action_id in frozen:
            raise ValueError(f"duplicate_valid_action_id:{action_id}")
        frozen[action_id] = value
    return MappingProxyType(frozen)


def _validate_ordered_action_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise CommandGatewayError(
            "invalid_action_batch", "ordered_action_ids must be a sequence"
        )
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise CommandGatewayError(
                "invalid_action_id", "every action ID must be a non-empty string"
            )
        if value in seen:
            raise CommandGatewayError(
                "duplicate_action_id", f"action {value!r} occurs more than once"
            )
        seen.add(value)
        result.append(value)
    return tuple(result)
