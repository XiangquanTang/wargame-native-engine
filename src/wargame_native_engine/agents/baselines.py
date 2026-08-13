"""Deterministic in-process Agent baselines."""

from __future__ import annotations

from copy import deepcopy
from typing import Sequence

from wargame_native_engine.agents.protocol import (
    AgentAction,
    AgentResetContext,
    AgentStepContext,
)
from wargame_native_engine.core.action import EngineAction, ValidAction
from wargame_native_engine.core.observation import SideObservation


class NoOpAgent:
    """A deterministic Agent that submits no action."""

    def __init__(self, agent_id: str = "no-op") -> None:
        self.agent_id = str(agent_id)

    def reset(self, context: AgentResetContext) -> None:
        del context

    def act(
        self,
        *,
        observation: SideObservation,
        valid_actions: Sequence[ValidAction],
        context: AgentStepContext,
    ) -> AgentAction | None:
        del observation, valid_actions, context
        return None

    def close(self) -> None:
        return None


class ScriptedAgent:
    """Return one predeclared action per step, then no action when exhausted."""

    def __init__(
        self,
        agent_id: str,
        script: Sequence[AgentAction | EngineAction | None],
    ) -> None:
        self.agent_id = str(agent_id)
        self._script = tuple(
            None
            if item is None
            else deepcopy(item)
            if isinstance(item, AgentAction)
            else AgentAction(deepcopy(item), item.action_id)
            for item in script
        )
        self._index = 0

    def reset(self, context: AgentResetContext) -> None:
        del context
        self._index = 0

    def act(
        self,
        *,
        observation: SideObservation,
        valid_actions: Sequence[ValidAction],
        context: AgentStepContext,
    ) -> AgentAction | None:
        del observation, valid_actions, context
        if self._index >= len(self._script):
            return None
        selected = self._script[self._index]
        self._index += 1
        return deepcopy(selected)

    def close(self) -> None:
        return None
