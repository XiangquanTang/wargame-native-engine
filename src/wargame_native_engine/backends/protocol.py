from __future__ import annotations

from typing import Mapping, Protocol, Sequence

from wargame_native_engine.core.action import EngineAction, ValidAction
from wargame_native_engine.core.boarding import BoardCapability, UnloadCapability
from wargame_native_engine.core.concealment import ConcealmentCapability
from wargame_native_engine.core.fire_capability import (
    GuidedFireCapability,
    IndirectFireCapability,
)
from wargame_native_engine.core.indirect_fire import IndirectFireActiveAreaView
from wargame_native_engine.core.observation import SideObservation
from wargame_native_engine.core.result import GameResult, StepResult
from wargame_native_engine.core.tactical_overlay import UnitTacticalOverlay
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.io.scenario import NativeScenario
from wargame_native_engine.movement.models import MovementMode, RoutePlan
from wargame_native_engine.rules.config import Ruleset


class EngineBackend(Protocol):
    scenario: NativeScenario
    max_ticks: int
    ruleset: Ruleset

    def reset(self, seed: int = 42) -> WorldState: ...
    def observe(self, side: str) -> SideObservation: ...
    def valid_actions(self, side: str) -> tuple[ValidAction, ...]: ...
    def plan_move(self, side: str, unit_id: int, destination_hex: int,
                  movement_mode: MovementMode | str = MovementMode.NORMAL
                  ) -> RoutePlan: ...
    def query_unit_tactical_overlay(
        self, side: str, unit_id: int,
    ) -> UnitTacticalOverlay: ...
    def query_unit_tactical_overlay_from_projection(
        self, side: str, unit_id: int,
        observation: Mapping[str, object],
        source_frame: Mapping[str, object],
        valid_actions: Sequence[Mapping[str, object]],
    ) -> UnitTacticalOverlay: ...
    def get_indirect_fire_capabilities(
        self, side: str,
    ) -> tuple[IndirectFireCapability, ...]: ...
    def get_guided_fire_capabilities(
        self, side: str,
    ) -> tuple[GuidedFireCapability, ...]: ...
    def get_board_capabilities(
        self, side: str,
    ) -> tuple[BoardCapability, ...]: ...
    def get_unload_capabilities(
        self, side: str,
    ) -> tuple[UnloadCapability, ...]: ...
    def get_concealment_capabilities(
        self, side: str,
    ) -> tuple[ConcealmentCapability, ...]: ...
    def authoritative_operator_lifecycle_views(
        self, side: str,
    ) -> tuple[object, ...]: ...
    def get_indirect_fire_active_areas(
        self, side: str,
    ) -> tuple[IndirectFireActiveAreaView, ...]: ...
    def indirect_fire_action(
        self, capability: IndirectFireCapability, planned_hex: int,
    ) -> EngineAction: ...
    def step(self, actions: Mapping[str, EngineAction | None] | Sequence[EngineAction],
             *, delta_time_seconds: int | float | None = None,
             delta_time_ms: int | None = None) -> StepResult: ...
    def step_ms(self, actions: Mapping[str, EngineAction | None] | Sequence[EngineAction],
                *, delta_time_ms: int) -> StepResult: ...
    def is_terminal(self) -> bool: ...
    def result(self) -> GameResult: ...
    def snapshot(self) -> Mapping[str, object]: ...
    def restore(self, snapshot: Mapping[str, object]) -> WorldState: ...
    def close(self) -> None: ...
