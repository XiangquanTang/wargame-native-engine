from __future__ import annotations

import random
from typing import Sequence

from wargame_native_engine.agents.protocol import (
    AgentAction, AgentResetContext, AgentStepContext,
)
from wargame_native_engine.core.action import ValidAction, valid_action_sort_key
from wargame_native_engine.core.observation import SideObservation


class RandomLegalAgent:
    agent_id = "random-legal"

    def __init__(self, seed: int = 0) -> None:
        self.seed = int(seed)
        self._rng = random.Random(self.seed)

    def reset(self, context: AgentResetContext) -> None:
        del context
        self._rng = random.Random(self.seed)

    def act(self, *, observation: SideObservation,
            valid_actions: Sequence[ValidAction],
            context: AgentStepContext) -> AgentAction | None:
        del context
        legal = set(observation.legal_action_ids)
        values = sorted((item for item in valid_actions
                         if item.action_id in legal), key=valid_action_sort_key)
        return None if not values else AgentAction.from_valid_action(
            self._rng.choice(values))

    def close(self) -> None:
        return None
