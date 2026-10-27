"""Minimal JSON-in/action-ID-out policy boundary, independent of model vendors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class EpisodeContext:
    case_id: str
    seed: int
    side: str
    training_interface: str


class EvaluationPolicy(Protocol):
    """Return one current catalog action ID, or None for an empty decision.

    A factory creates a fresh instance for each episode. The input is detached,
    side-filtered JSON, never an environment handle or authoritative world.
    Implement network timeouts inside model clients: this in-process protocol
    is not a sandbox and cannot interrupt an indefinitely blocked call.
    """

    def decide(self, observation: Mapping[str, Any]) -> str | None: ...

    def close(self) -> None: ...


class NoOpPolicy:
    """A plumbing baseline, not evidence of task-solving ability."""

    def decide(self, observation: Mapping[str, Any]) -> None:
        return None

    def close(self) -> None:
        pass


class FirstLegalPolicy:
    """Select the first catalog item, with no domain-specific planning."""

    def decide(self, observation: Mapping[str, Any]) -> str | None:
        actions = observation["action_space"]["action_ids"]
        return actions[0] if actions else None

    def close(self) -> None:
        pass
