from __future__ import annotations

from typing import Mapping, Sequence

from wargame_native_engine.agents.protocol import AgentAction, AgentStepContext
from wargame_native_engine.backends.protocol import EngineBackend
from wargame_native_engine.core.action import EngineAction, ValidAction
from wargame_native_engine.core.observation import SideObservation
from wargame_native_engine.core.result import GameResult, StepResult
from wargame_native_engine.core.world import Stage, WorldState
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.movement.models import MovementMode, RoutePlan


class GameSession:
    """Unified native lifecycle for Agents, CLI, replay, UI and benchmarks."""

    def __init__(self, backend: EngineBackend) -> None:
        self.backend = backend
        self.scenario = backend.scenario
        self.seed = 0
        self.session_id = "session-uninitialized"
        self._reset = False
        self._closed = False

    def reset(self, seed: int = 42) -> WorldState:
        if self._closed:
            raise RuntimeError("session_closed")
        self.seed = int(seed)
        self.session_id = "session-" + canonical_hash({
            "scenario_hash": self.scenario.scenario_hash,
            "seed": self.seed,
        })[:24]
        result = self.backend.reset(self.seed)
        self._reset = True
        return result

    def current_seats(self) -> tuple[str, ...]:
        self._require_active()
        return () if self.backend.is_terminal() else self.scenario.side_order

    @property
    def max_ticks(self) -> int:
        return self.backend.max_ticks

    def observe(self, side: str) -> SideObservation:
        self._require_active()
        return self.backend.observe(side)

    def valid_actions(self, side: str) -> tuple[ValidAction, ...]:
        self._require_active()
        return self.backend.valid_actions(side)

    def plan_move(self, side: str, unit_id: int, destination_hex: int,
                  movement_mode: MovementMode | str = MovementMode.NORMAL
                  ) -> RoutePlan:
        self._require_active()
        return self.backend.plan_move(
            side, unit_id, destination_hex, movement_mode
        )

    def step(self, action_batch: Mapping[
            str, AgentAction | EngineAction | None] | Sequence[EngineAction], *,
             delta_time_seconds: int | float = 1) -> StepResult:
        self._require_active()
        if isinstance(action_batch, Sequence) and not isinstance(
                action_batch, (str, bytes)):
            if not all(isinstance(item, EngineAction) for item in action_batch):
                raise TypeError("ordered session actions must be EngineAction values")
            return self.backend.step(tuple(action_batch),
                                     delta_time_seconds=delta_time_seconds)
        values: dict[str, EngineAction | None] = {}
        for side, item in action_batch.items():
            if isinstance(item, AgentAction):
                if item.valid_action_id != item.action.action_id:
                    raise ValueError("agent valid-action identity mismatch")
                values[side] = item.action
            elif isinstance(item, EngineAction) or item is None:
                values[side] = item
            else:
                raise TypeError("session actions must be AgentAction, EngineAction or None")
        return self.backend.step(values, delta_time_seconds=delta_time_seconds)

    def stage(self) -> Stage:
        self._require_active()
        return self.backend.result().final_stage

    def is_terminal(self) -> bool:
        self._require_active()
        return self.backend.is_terminal()

    def result(self) -> GameResult:
        self._require_active(allow_closed=True)
        return self.backend.result()

    def snapshot(self) -> Mapping[str, object]:
        self._require_active()
        return self.backend.snapshot()

    def restore(self, snapshot: Mapping[str, object]) -> WorldState:
        if self._closed:
            raise RuntimeError("session_closed")
        result = self.backend.restore(snapshot)
        self.seed = int(snapshot["seed"])
        self.session_id = "session-" + canonical_hash({
            "scenario_hash": self.scenario.scenario_hash,
            "seed": self.seed,
        })[:24]
        self._reset = True
        return result

    def agent_context(self, side: str) -> AgentStepContext:
        observation = self.observe(side)
        return AgentStepContext(self.session_id, side, observation.tick,
                                observation.stage, self.seed)

    def close(self) -> None:
        if not self._closed:
            self.backend.close()
            self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_active(self, *, allow_closed: bool = False) -> None:
        if not self._reset:
            raise RuntimeError("session_not_reset")
        if self._closed and not allow_closed:
            raise RuntimeError("session_closed")
