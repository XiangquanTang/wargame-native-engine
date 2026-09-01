from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from wargame_native_engine.core.action import EngineAction, ValidAction
from wargame_native_engine.core.observation import SideObservation
from wargame_native_engine.core.world import Stage
from wargame_native_engine.projection.decision_capability import (
    ActionCatalogSnapshot,
)
from wargame_native_engine.projection.decision_semantic import (
    SideDecisionSemanticProjection,
)


@dataclass(frozen=True, slots=True)
class AgentResetContext:
    session_id: str
    scenario_id: str
    side: str
    seed: int
    ruleset_version: str
    agent_id: str = ""

    @property
    def perspective(self) -> str:
        return self.side.upper()

    @property
    def episode_seed(self) -> int:
        return self.seed


@dataclass(frozen=True, slots=True)
class AgentStepContext:
    session_id: str
    side: str
    tick: int
    stage: Stage
    seed: int
    agent_id: str = ""

    @property
    def perspective(self) -> str:
        return self.side.upper()

    @property
    def episode_seed(self) -> int:
        return self.seed


@dataclass(frozen=True, slots=True)
class AgentAction:
    action: EngineAction
    valid_action_id: str

    @classmethod
    def from_valid_action(cls, value: ValidAction) -> "AgentAction":
        return cls(value.action, value.action_id)

    def to_dict(self) -> dict[str, object]:
        return {"valid_action_id": self.valid_action_id,
                "action": self.action.to_dict()}


class Agent(Protocol):
    agent_id: str

    def reset(self, context: AgentResetContext) -> None: ...

    def act(self, *, observation: SideObservation,
            valid_actions: Sequence[ValidAction],
            context: AgentStepContext) -> AgentAction | None: ...

    def close(self) -> None: ...


class DecisionAgent(Protocol):
    """Versioned Agent path over the unified machine semantic core."""

    agent_id: str

    def reset(self, context: AgentResetContext) -> None: ...

    def act_decision(
        self,
        *,
        decision_semantic: SideDecisionSemanticProjection,
        action_catalog: ActionCatalogSnapshot,
        context: AgentStepContext,
    ) -> AgentAction | None: ...

    def close(self) -> None: ...
