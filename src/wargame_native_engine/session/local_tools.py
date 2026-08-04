from __future__ import annotations

from typing import Any, Mapping

from wargame_native_engine.agents.protocol import AgentAction
from wargame_native_engine.core.action import EngineAction
from wargame_native_engine.session.game_session import GameSession


class LocalAgentTools:
    """In-process external-Agent boundary; no network or global-state access."""

    def __init__(self, session: GameSession) -> None:
        self.session = session
        self._pending: dict[str, AgentAction | EngineAction | None] = {}

    def get_episode_status(self) -> Mapping[str, Any]:
        result = self.session.result()
        return {"session_id": self.session.session_id,
                "stage": result.final_stage.value, "tick": result.ticks,
                "terminal": result.terminal, "truncated": result.truncated,
                "current_seats": list(self.session.current_seats())
                if not self.session.is_terminal() else []}

    def get_observation(self, side: str) -> Mapping[str, Any]:
        return self.session.observe(side).to_dict()

    def get_valid_actions(self, side: str) -> list[Mapping[str, Any]]:
        return [item.to_dict() for item in self.session.valid_actions(side)]

    def submit_action(self, side: str,
                      action: AgentAction | EngineAction | None) -> Mapping[str, Any]:
        seats = self.session.current_seats()
        if side not in seats:
            raise ValueError(f"side is not active: {side}")
        if side in self._pending:
            raise ValueError(f"side already submitted for current tick: {side}")
        self._pending[side] = action
        missing = [item for item in seats if item not in self._pending]
        if missing:
            return {"accepted": True, "advanced": False,
                    "pending_sides": missing}
        result = self.session.step(self._pending)
        self._pending = {}
        return {"accepted": True, "advanced": True,
                "pending_sides": [], "step_result": result.to_dict()}

    def get_episode_result(self) -> Mapping[str, Any]:
        return self.session.result().to_dict()
