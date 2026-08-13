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

    def to_record(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "step_index": self.step_index,
            "terminal": self.terminal,
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
        }


@dataclass(frozen=True, slots=True)
class CommandResult:
    request_id: str
    accepted: bool
    advanced: bool
    pending_perspectives: tuple[str, ...]
    view: ControllerView

    def to_record(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "accepted": self.accepted,
            "advanced": self.advanced,
            "pending_perspectives": list(self.pending_perspectives),
            "view": self.view.to_record(),
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
