"""Detached records crossing the local command-gateway boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.io.serialization import json_clone


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

    def to_record(self) -> dict[str, object]:
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
