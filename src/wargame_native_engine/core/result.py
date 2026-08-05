from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from wargame_native_engine.core.action import ActionDecision, EngineAction
from wargame_native_engine.core.event import EngineEvent
from wargame_native_engine.core.world import Stage
from wargame_native_engine.io.serialization import json_clone


@dataclass(frozen=True, slots=True)
class StepResult:
    tick_before: int
    tick_after: int
    stage_before: Stage
    stage_after: Stage
    actions: tuple[EngineAction, ...]
    decisions: tuple[ActionDecision, ...]
    events: tuple[EngineEvent, ...]
    terminal: bool
    truncated: bool
    action_sequences: tuple[int, ...] = ()
    delta_time_ms: int = 1000
    ordered_action_input: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"tick_before": self.tick_before, "tick_after": self.tick_after,
                "stage_before": self.stage_before.value,
                "stage_after": self.stage_after.value,
                "actions": [item.to_dict() for item in self.actions],
                "decisions": [item.to_dict() for item in self.decisions],
                "events": [item.to_dict() for item in self.events],
                "terminal": self.terminal, "truncated": self.truncated,
                "action_sequences": list(self.action_sequences),
                "delta_time_ms": self.delta_time_ms,
                "ordered_action_input": self.ordered_action_input}


@dataclass(frozen=True, slots=True)
class GameResult:
    terminal: bool
    truncated: bool
    winner: str | None
    loser: str | None
    outcome: str
    reason: str | None
    final_stage: Stage
    ticks: int
    action_counts: Mapping[str, int] = field(default_factory=dict)
    error_counts: Mapping[str, int] = field(default_factory=dict)
    seed: int = 0
    ruleset_id: str = "native-default"
    ruleset_version: str = "1.0.0"
    ruleset_hash: str = ""
    world_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return json_clone({"terminal": self.terminal,
                           "truncated": self.truncated,
                           "winner": self.winner, "loser": self.loser,
                           "outcome": self.outcome, "reason": self.reason,
                           "final_stage": self.final_stage.value,
                           "ticks": self.ticks,
                           "action_counts": self.action_counts,
                           "error_counts": self.error_counts,
                           "seed": self.seed,
                           "ruleset_id": self.ruleset_id,
                           "ruleset_version": self.ruleset_version,
                           "ruleset_hash": self.ruleset_hash,
                           "world_hash": self.world_hash})
