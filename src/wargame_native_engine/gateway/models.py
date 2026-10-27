"""Detached records crossing the local command-gateway boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.io.detached import detached_record
from wargame_native_engine.io.serialization import json_clone
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.projection.decision_capability import (
    ActionCatalogSnapshot,
)
from wargame_native_engine.projection.decision_semantic import (
    DecisionProjectionContractError,
    SideDecisionSemanticProjection,
)


class CommandGatewayError(RuntimeError):
    """A stable, non-adjudication failure at the local control boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}:{self.message}")


@dataclass(frozen=True, slots=True)
class SessionDescriptor:
    session_id: str
    step_index: int
    terminal: bool
    finalized: bool = False
    frame_index: int = 0
    sim_time_ms: int = 0
    lifecycle_state: str = "Running"
    terminated: bool = False
    truncated: bool = False

    def __post_init__(self) -> None:
        _exact_bool(self.terminal, "terminal")
        _exact_bool(self.finalized, "finalized")
        _exact_bool(self.terminated, "terminated")
        _exact_bool(self.truncated, "truncated")

    def to_record(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "step_index": self.step_index,
            "terminal": self.terminal,
            "finalized": self.finalized,
            "frame_index": self.frame_index,
            "sim_time_ms": self.sim_time_ms,
            "lifecycle_state": self.lifecycle_state,
            "terminated": self.terminated,
            "truncated": self.truncated,
        }


@detached_record
@dataclass(frozen=True, slots=True)
class DecisionWindowView:
    session_id: str
    decision_window_index: int
    opened_at_sim_time_ms: int
    closes_at_sim_time_ms: int | None
    opened_at_server_monotonic: float | None
    deadline_at_server_monotonic: float | None
    deadline_policy: str
    lifecycle_state: str
    close_reason: str | None
    submitted: bool
    pending_perspectives: tuple[str, ...]
    valid_actions: tuple[Mapping[str, Any], ...]

    def to_record(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "decision_window_index": self.decision_window_index,
            "opened_at_sim_time_ms": self.opened_at_sim_time_ms,
            "closes_at_sim_time_ms": self.closes_at_sim_time_ms,
            "opened_at_server_monotonic": self.opened_at_server_monotonic,
            "deadline_at_server_monotonic": self.deadline_at_server_monotonic,
            "deadline_policy": self.deadline_policy,
            "lifecycle_state": self.lifecycle_state,
            "close_reason": self.close_reason,
            "submitted": self.submitted,
            "pending_perspectives": list(self.pending_perspectives),
            "valid_actions": json_clone(self.valid_actions),
        }


@detached_record
@dataclass(frozen=True, slots=True)
class ControllerView:
    session_id: str
    perspective: str
    step_index: int
    observation: Mapping[str, Any]
    source_frame: Mapping[str, Any]
    valid_actions: tuple[Mapping[str, Any], ...]
    terminal: bool
    winner: str | None
    outcome: str | None
    frame_index: int = 0
    sim_time_ms: int = 0
    terminated: bool = False
    truncated: bool = False
    decision_semantic: SideDecisionSemanticProjection | None = None
    action_catalog: ActionCatalogSnapshot | None = None
    own_weapon_inventory: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        _exact_bool(self.terminal, "terminal")
        _exact_bool(self.terminated, "terminated")
        _exact_bool(self.truncated, "truncated")
        if (self.decision_semantic is None) != (self.action_catalog is None):
            raise DecisionProjectionContractError(
                "ControllerView decision core must be complete or legacy-only"
            )
        object.__setattr__(
            self,
            "own_weapon_inventory",
            tuple(json_clone(self.own_weapon_inventory)),
        )
        if self.decision_semantic is not None:
            self._validate_decision_core()

    @property
    def has_decision_core(self) -> bool:
        return self.decision_semantic is not None

    def _validate_decision_core(self) -> None:
        decision = self.decision_semantic
        catalog = self.action_catalog
        if type(decision) is not SideDecisionSemanticProjection:
            raise TypeError("decision_semantic must be an exact Decision projection")
        if type(catalog) is not ActionCatalogSnapshot:
            raise TypeError("action_catalog must be an exact ActionCatalogSnapshot")
        snapshot = decision.snapshot
        if self.perspective.lower() != snapshot.side.value:
            raise DecisionProjectionContractError("ControllerView side mismatch")
        if self.step_index != snapshot.decision_window_index:
            raise DecisionProjectionContractError("ControllerView window mismatch")
        if self.frame_index != snapshot.frame_index:
            raise DecisionProjectionContractError("ControllerView frame mismatch")
        if self.sim_time_ms != snapshot.sim_time_ms:
            raise DecisionProjectionContractError("ControllerView sim time mismatch")
        if (
            self.terminated != snapshot.terminated
            or self.truncated != snapshot.truncated
        ):
            raise DecisionProjectionContractError("ControllerView terminal mismatch")
        if decision.action_space != catalog.reference():
            raise DecisionProjectionContractError(
                "ControllerView Decision/catalog reference mismatch"
            )
        if len(self.valid_actions) != catalog.action_count:
            raise DecisionProjectionContractError(
                "ControllerView valid action/catalog count mismatch"
            )
        for index, (legacy, entry) in enumerate(
            zip(self.valid_actions, catalog.entries)
        ):
            if not isinstance(legacy, Mapping):
                raise TypeError("ControllerView valid actions must be mappings")
            if legacy.get("action_id") != entry.action_id:
                raise DecisionProjectionContractError(
                    f"ControllerView action identity mismatch at index {index}"
                )
            action = legacy.get("action")
            if not isinstance(action, Mapping):
                raise DecisionProjectionContractError(
                    f"ControllerView action record missing at index {index}"
                )
            if canonical_hash(action) != entry.operation_hash:
                raise DecisionProjectionContractError(
                    f"ControllerView operation mismatch at index {index}"
                )

    def to_record(self) -> dict[str, object]:
        """Serialize the explicit legacy compatibility surface only."""

        return {
            "session_id": self.session_id,
            "perspective": self.perspective,
            "step_index": self.step_index,
            "observation": json_clone(self.observation),
            "source_frame": json_clone(self.source_frame),
            "valid_actions": json_clone(self.valid_actions),
            "terminal": self.terminal,
            "winner": self.winner,
            "outcome": self.outcome,
            "frame_index": self.frame_index,
            "sim_time_ms": self.sim_time_ms,
            "terminated": self.terminated,
            "truncated": self.truncated,
        }

    def decision_record(self) -> dict[str, object]:
        """Serialize only the perspective-safe machine decision interface."""

        if self.decision_semantic is None or self.action_catalog is None:
            raise DecisionProjectionContractError(
                "legacy ControllerView has no Decision semantic core"
            )
        return {
            "decision_semantic": self.decision_semantic.to_dict(),
            "action_catalog": self.action_catalog.to_dict(),
        }


@detached_record
@dataclass(frozen=True, slots=True)
class CommandResult:
    request_id: str
    accepted: bool
    advanced: bool
    pending_perspectives: tuple[str, ...]
    view: ControllerView
    decision_window: DecisionWindowView | None = None

    def to_record(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "accepted": self.accepted,
            "advanced": self.advanced,
            "pending_perspectives": list(self.pending_perspectives),
            "view": self.view.to_record(),
            "decision_window": (
                None
                if self.decision_window is None
                else self.decision_window.to_record()
            ),
        }


@dataclass(frozen=True, slots=True)
class SessionPublication:
    output_dir: Path
    replay_path: Path
    analysis_path: Path
    frame_count: int
    step_count: int
    terminal: bool
    truncated: bool
    control_audit_path: Path | None = None

    def __post_init__(self) -> None:
        _exact_bool(self.terminal, "terminal")
        _exact_bool(self.truncated, "truncated")


def _exact_bool(value: bool, field: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field} must be an exact bool")
    return value
