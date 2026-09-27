"""Window-scoped immutable server action proposals."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Mapping

from wargame_native_engine.core.action import EngineAction, ValidAction
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.movement.models import RoutePlan


class OperationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}:{self.message}")


@dataclass(frozen=True, slots=True)
class ActionProposal:
    proposal_id: str
    session_id: str
    decision_window_index: int
    side: str
    unit_id: int
    source_hex: int | None
    destination_hex: int
    movement_mode: str
    ordered_path: tuple[int, ...]
    estimated_duration_ms: int | None
    segment_details: tuple[Mapping[str, object], ...]
    reachable: bool
    blocked_reason: str | None
    created_from_state_hash: str
    action_id: str | None

    @classmethod
    def from_route(
        cls,
        *,
        session_id: str,
        decision_window_index: int,
        side: str,
        route: RoutePlan,
        state_hash: str,
        action: EngineAction | None,
    ) -> "ActionProposal":
        identity = {
            "session_id": str(session_id),
            "decision_window_index": decision_window_index,
            "side": side,
            "unit_id": route.unit_id,
            "source_hex": route.source_hex,
            "destination_hex": route.destination_hex,
            "movement_mode": route.movement_mode.value,
            "ordered_path": list(route.ordered_path),
            "estimated_duration_ms": route.estimated_duration_ms,
            "created_from_state_hash": state_hash,
        }
        return cls(
            proposal_id="proposal-" + canonical_hash(identity)[:24],
            session_id=str(session_id),
            decision_window_index=decision_window_index,
            side=side,
            unit_id=route.unit_id,
            source_hex=route.source_hex,
            destination_hex=route.destination_hex,
            movement_mode=route.movement_mode.value,
            ordered_path=tuple(route.ordered_path),
            estimated_duration_ms=route.estimated_duration_ms,
            segment_details=tuple(
                json_clone(item.to_dict()) for item in route.segments
            ),
            reachable=route.reachable,
            blocked_reason=None if route.reachable else route.reason_code,
            created_from_state_hash=state_hash,
            action_id=None if action is None else action.action_id,
        )

    @classmethod
    def from_indirect_fire(
        cls,
        *,
        session_id: str,
        decision_window_index: int,
        side: str,
        shooter_id: int,
        target_hex: int,
        state_hash: str,
        action: EngineAction,
    ) -> "ActionProposal":
        """Wrap one server-issued fire action in the existing window registry.

        ``ActionProposal`` predates non-movement proposals.  Its movement-only
        presentation fields remain inert here; authority is carried solely by
        the embedded ``ValidAction`` kept in the private registry.
        """

        identity = {
            "operation_type": "INDIRECT_FIRE",
            "session_id": str(session_id),
            "decision_window_index": decision_window_index,
            "side": side,
            "shooter_id": shooter_id,
            "target_hex": target_hex,
            "action_id": action.action_id,
            "created_from_state_hash": state_hash,
        }
        return cls(
            proposal_id="proposal-" + canonical_hash(identity)[:24],
            session_id=str(session_id),
            decision_window_index=decision_window_index,
            side=side,
            unit_id=shooter_id,
            source_hex=None,
            destination_hex=target_hex,
            movement_mode="INDIRECT_FIRE",
            ordered_path=(),
            estimated_duration_ms=None,
            segment_details=(),
            reachable=True,
            blocked_reason=None,
            created_from_state_hash=state_hash,
            action_id=action.action_id,
        )

    @classmethod
    def from_direct_fire_intent(
        cls,
        *,
        session_id: str,
        decision_window_index: int,
        side: str,
        actor_id: int,
        target_id: int,
        state_hash: str,
        action: EngineAction,
    ) -> "ActionProposal":
        """Wrap an identity-authorized, not-yet-adjudicated DIRECT intent."""

        identity = {
            "operation_type": "DIRECT_FIRE_INTENT",
            "session_id": str(session_id),
            "decision_window_index": decision_window_index,
            "side": side,
            "actor_id": actor_id,
            "target_id": target_id,
            "action_id": action.action_id,
            "created_from_state_hash": state_hash,
        }
        return cls(
            proposal_id="proposal-" + canonical_hash(identity)[:24],
            session_id=str(session_id),
            decision_window_index=decision_window_index,
            side=side,
            unit_id=actor_id,
            source_hex=None,
            destination_hex=target_id,
            movement_mode="DIRECT_FIRE_INTENT",
            ordered_path=(),
            estimated_duration_ms=None,
            segment_details=(),
            reachable=True,
            blocked_reason=None,
            created_from_state_hash=state_hash,
            action_id=action.action_id,
        )

    def to_record(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "session_id": self.session_id,
            "decision_window_index": self.decision_window_index,
            "side": self.side,
            "unit_id": self.unit_id,
            "source_hex": self.source_hex,
            "destination_hex": self.destination_hex,
            "movement_mode": self.movement_mode,
            "ordered_path": list(self.ordered_path),
            "estimated_duration_ms": self.estimated_duration_ms,
            "segment_details": json_clone(self.segment_details),
            "reachable": self.reachable,
            "blocked_reason": self.blocked_reason,
            "created_from_state_hash": self.created_from_state_hash,
            "action_id": self.action_id,
        }


class ActionProposalRegistry:
    """Private registry owned by one Session's active DecisionWindow."""

    def __init__(self, session_id: str, decision_window_index: int) -> None:
        self.session_id = str(session_id)
        self.decision_window_index = decision_window_index
        self._proposals: dict[str, tuple[ActionProposal, ValidAction | None]] = {}
        self._requests: dict[str, tuple[tuple[object, ...], str]] = {}

    def cached(
        self,
        request_id: str,
        signature: tuple[object, ...],
    ) -> ActionProposal | None:
        cached = self._requests.get(str(request_id))
        if cached is None:
            return None
        if cached[0] != signature:
            raise OperationError(
                "proposal_request_conflict",
                f"request_id {request_id!r} was reused with another plan",
            )
        return deepcopy(self._proposals[cached[1]][0])

    def register(
        self,
        *,
        request_id: str,
        signature: tuple[object, ...],
        proposal: ActionProposal,
        valid_action: ValidAction | None,
    ) -> ActionProposal:
        if proposal.session_id != self.session_id:
            raise OperationError("proposal_session_mismatch", proposal.session_id)
        if proposal.decision_window_index != self.decision_window_index:
            raise OperationError(
                "proposal_window_mismatch", str(proposal.decision_window_index)
            )
        existing = self._proposals.get(proposal.proposal_id)
        if existing is not None:
            if existing[0] != proposal:
                raise OperationError(
                    "proposal_identity_conflict", proposal.proposal_id
                )
        else:
            self._proposals[proposal.proposal_id] = (
                deepcopy(proposal), deepcopy(valid_action)
            )
        self._requests[str(request_id)] = (signature, proposal.proposal_id)
        return deepcopy(proposal)

    def get(self, proposal_id: str, *, side: str) -> ActionProposal:
        try:
            proposal = self._proposals[str(proposal_id)][0]
        except KeyError as exc:
            raise OperationError(
                "proposal_not_found", f"unknown proposal {proposal_id!r}"
            ) from exc
        if proposal.side != side:
            raise OperationError(
                "proposal_side_mismatch", "proposal belongs to another side"
            )
        return deepcopy(proposal)

    def valid_action(self, proposal_id: str, *, side: str) -> ValidAction:
        proposal = self.get(proposal_id, side=side)
        value = self._proposals[proposal.proposal_id][1]
        if value is None:
            raise OperationError(
                "proposal_not_actionable", proposal.blocked_reason or "unreachable"
            )
        return deepcopy(value)
