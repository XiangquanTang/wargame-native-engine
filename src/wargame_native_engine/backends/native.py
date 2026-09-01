from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Sequence
from pathlib import Path

from wargame_native_engine.core.action import EngineAction, ValidAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.fire_capability import IndirectFireCapability
from wargame_native_engine.core.observation import SideObservation
from wargame_native_engine.core.result import GameResult, StepResult
from wargame_native_engine.core.tactical_overlay import UnitTacticalOverlay
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.io.scenario import NativeScenario
from wargame_native_engine.movement.models import MovementMode, RoutePlan
from wargame_native_engine.rules.config import Ruleset
from wargame_native_engine.projection.operator_lifecycle import (
    AuthoritativeOperatorLifecycleView,
    project_operator_lifecycle,
)

if TYPE_CHECKING:
    from wargame_native_engine.data.map_runtime import MapRuntimeData


class NativeEngineBackend:
    """Thin backend adapter; it contains no second rules implementation."""

    backend_id = "native-engine"

    def __init__(self, scenario: NativeScenario, *,
                 max_ticks: int | None = None,
                 ruleset: Ruleset | str | Path | None = None,
                 map_runtime_data: "MapRuntimeData | None" = None) -> None:
        self.scenario = scenario
        self.engine = NativeEngine(scenario, max_ticks=max_ticks,
                                   ruleset=ruleset,
                                   map_runtime_data=map_runtime_data)
        self.ruleset = self.engine.ruleset
        self.max_ticks = self.engine.terminal_rules.max_ticks

    @property
    def determinism_identity(self) -> Mapping[str, object]:
        runtime = self.engine.map_runtime_data
        return {} if runtime is None else {
            "map_runtime_data": runtime.identity_record()
        }

    def reset(self, seed: int = 42) -> WorldState:
        return self.engine.reset(seed)

    def observe(self, side: str) -> SideObservation:
        return self.engine.observe(side)

    def valid_actions(self, side: str) -> tuple[ValidAction, ...]:
        return self.engine.valid_actions(side)

    def plan_move(self, side: str, unit_id: int, destination_hex: int,
                  movement_mode: MovementMode | str = MovementMode.NORMAL
                  ) -> RoutePlan:
        return self.engine.plan_move(
            side, unit_id, destination_hex, movement_mode
        )

    def query_unit_tactical_overlay(
        self, side: str, unit_id: int,
    ) -> UnitTacticalOverlay:
        return self.engine.query_unit_tactical_overlay(side, unit_id)

    def query_unit_tactical_overlay_from_projection(
        self, side: str, unit_id: int,
        observation: Mapping[str, object],
        source_frame: Mapping[str, object],
        valid_actions: Sequence[Mapping[str, object]],
    ) -> UnitTacticalOverlay:
        return self.engine.query_unit_tactical_overlay_from_projection(
            side, unit_id, observation, source_frame, valid_actions
        )

    def get_indirect_fire_capabilities(
        self, side: str,
    ) -> tuple[IndirectFireCapability, ...]:
        return self.engine.get_indirect_fire_capabilities(side)

    def get_guided_fire_capabilities(self, side: str):
        return self.engine.get_guided_fire_capabilities(side)

    def get_board_capabilities(self, side: str):
        return self.engine.get_board_capabilities(side)

    def get_unload_capabilities(self, side: str):
        return self.engine.get_unload_capabilities(side)

    def get_concealment_capabilities(self, side: str):
        return self.engine.get_concealment_capabilities(side)

    def authoritative_operator_lifecycle_views(
        self, side: str,
    ) -> tuple[AuthoritativeOperatorLifecycleView, ...]:
        """Project OWN lifecycle values without exposing mutable WorldState."""

        self.engine._require_side(side)  # noqa: SLF001
        world = self.engine._require_world()  # noqa: SLF001
        return tuple(
            project_operator_lifecycle(world, operator.operator_id)
            for operator in sorted(
                world.operators.values(), key=lambda value: value.operator_id
            )
            if operator.side == side
        )

    def get_indirect_fire_active_areas(self, side: str):
        return self.engine.get_indirect_fire_active_areas(side)

    def indirect_fire_action(
        self,
        capability: IndirectFireCapability,
        planned_hex: int,
    ) -> EngineAction:
        return self.engine.indirect_fire_action(capability, planned_hex)

    def step(self, actions: Mapping[str, EngineAction | None] | Sequence[EngineAction],
             *, delta_time_seconds: int | float | None = None,
             delta_time_ms: int | None = None) -> StepResult:
        return self.engine.step(
            actions,
            delta_time_seconds=delta_time_seconds,
            delta_time_ms=delta_time_ms,
        )

    def step_ms(self, actions: Mapping[str, EngineAction | None]
                | Sequence[EngineAction], *, delta_time_ms: int) -> StepResult:
        return self.engine.step_ms(actions, delta_time_ms=delta_time_ms)

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
