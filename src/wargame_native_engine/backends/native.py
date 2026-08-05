from __future__ import annotations

from typing import Mapping, Sequence
from pathlib import Path

from wargame_native_engine.core.action import EngineAction, ValidAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.observation import SideObservation
from wargame_native_engine.core.result import GameResult, StepResult
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.io.scenario import NativeScenario
from wargame_native_engine.rules.config import Ruleset


class NativeEngineBackend:
    """Thin backend adapter; it contains no second rules implementation."""

    backend_id = "native-engine"

    def __init__(self, scenario: NativeScenario, *,
                 max_ticks: int | None = None,
                 ruleset: Ruleset | str | Path | None = None) -> None:
        self.scenario = scenario
        self.engine = NativeEngine(scenario, max_ticks=max_ticks,
                                   ruleset=ruleset)
        self.ruleset = self.engine.ruleset
        self.max_ticks = self.engine.terminal_rules.max_ticks

    def reset(self, seed: int = 42) -> WorldState:
        return self.engine.reset(seed)

    def observe(self, side: str) -> SideObservation:
        return self.engine.observe(side)

    def valid_actions(self, side: str) -> tuple[ValidAction, ...]:
        return self.engine.valid_actions(side)

    def step(self, actions: Mapping[str, EngineAction | None] | Sequence[EngineAction],
             *, delta_time_seconds: int | float = 1) -> StepResult:
        return self.engine.step(actions, delta_time_seconds=delta_time_seconds)

    def is_terminal(self) -> bool:
        return self.engine.is_terminal()

    def result(self) -> GameResult:
        return self.engine.result()

    def snapshot(self) -> Mapping[str, object]:
        return self.engine.snapshot()

    def restore(self, snapshot: Mapping[str, object]) -> WorldState:
        return self.engine.restore(snapshot)

    def close(self) -> None:
        self.engine.close()

    @property
    def closed(self) -> bool:
        return self.engine.closed
