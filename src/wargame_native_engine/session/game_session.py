from __future__ import annotations

from typing import Mapping, Sequence

from wargame_native_engine.agents.protocol import AgentAction, AgentStepContext
from wargame_native_engine.backends.protocol import EngineBackend
from wargame_native_engine.adjudication.fire_effect_query import FireEffectQuery
from wargame_native_engine.core.action import EngineAction, ValidAction
from wargame_native_engine.core.fire_capability import IndirectFireCapability
from wargame_native_engine.core.observation import SideObservation
from wargame_native_engine.core.result import GameResult, GameStatus, StepResult
from wargame_native_engine.core.tactical_overlay import UnitTacticalOverlay
from wargame_native_engine.core.region_visibility import (
    RegionVisibilityQuery, RegionVisibilityResult,
)
from wargame_native_engine.core.world import Stage, WorldState
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.movement.models import MovementMode, RoutePlan
from wargame_native_engine.core.time import (
    require_positive_milliseconds,
    resolve_delta_time_ms,
)


class GameSession:
    """Unified native lifecycle for Agents, CLI, replay, UI and benchmarks."""

    def __init__(self, backend: EngineBackend) -> None:
        self.backend = backend
        self.scenario = backend.scenario
        self.seed = 0
        self.session_id = "session-uninitialized"
        self._reset = False
        self._closed = False
        # Monotonic for this GameSession instance.  Unlike tick/frame numbers,
        # this also changes when reset/restore installs a state with the same
        # visible clock.  Consumers may use it only to invalidate derived
        # data; WorldState remains authoritative.
        self._state_revision = 0

    def reset(self, seed: int = 42) -> WorldState:
        if self._closed:
            raise RuntimeError("session_closed")
        self.seed = int(seed)
        identity = {
            "scenario_hash": self.scenario.scenario_hash,
            "seed": self.seed,
        }
        backend_identity = getattr(self.backend, "determinism_identity", {})
        if backend_identity:
            identity["backend_determinism_identity"] = backend_identity
        self.session_id = "session-" + canonical_hash(identity)[:24]
        result = self.backend.reset(self.seed)
        self._reset = True
        self._state_revision += 1
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

    def query_region_visibility_from_projection(
        self, side: str, actor_id: int,
        observation: Mapping[str, object], source_frame: Mapping[str, object],
        query: RegionVisibilityQuery,
    ) -> RegionVisibilityResult:
        self._require_active()
        return self.backend.query_region_visibility_from_projection(
            side, actor_id, observation, source_frame, query
        )

    def query_unit_tactical_overlay(
        self, side: str, unit_id: int,
    ) -> UnitTacticalOverlay:
        self._require_active()
        return self.backend.query_unit_tactical_overlay(side, unit_id)

    def query_unit_tactical_overlay_from_projection(
        self, side: str, unit_id: int,
        observation: Mapping[str, object],
        source_frame: Mapping[str, object],
        valid_actions: Sequence[Mapping[str, object]],
    ) -> UnitTacticalOverlay:
        self._require_active()
        return self.backend.query_unit_tactical_overlay_from_projection(
            side, unit_id, observation, source_frame, valid_actions
        )

    def get_indirect_fire_capabilities(
        self, side: str,
    ) -> tuple[IndirectFireCapability, ...]:
        """Return detached authority records without creating an action."""

        self._require_active()
        return self.backend.get_indirect_fire_capabilities(side)

    def get_guided_fire_capabilities(self, side: str):
        self._require_active()
        return self.backend.get_guided_fire_capabilities(side)

    def get_board_capabilities(self, side: str):
        self._require_active()
        return self.backend.get_board_capabilities(side)

    def get_unload_capabilities(self, side: str):
        self._require_active()
        return self.backend.get_unload_capabilities(side)

    def get_boarding_capability_assessments(self, side: str):
        self._require_active()
        return self.backend.get_boarding_capability_assessments(side)

    def get_concealment_capabilities(self, side: str):
        self._require_active()
        return self.backend.get_concealment_capabilities(side)

    def authoritative_operator_lifecycle_views(self, side: str):
        """Return detached authoritative-derived OWN lifecycle values."""

        self._require_active()
        producer = getattr(
            self.backend, "authoritative_operator_lifecycle_views", None
        )
        if not callable(producer):
            raise RuntimeError("backend_lifecycle_projection_unavailable")
        return producer(side)

    def get_indirect_fire_active_areas(self, side: str):
        """Return detached own-side ACTIVE artillery areas."""

        self._require_active()
        return self.backend.get_indirect_fire_active_areas(side)

    def get_indirect_fire_mission_areas(self, side: str):
        """Return detached own-side waiting and active artillery areas."""

        self._require_active()
        return self.backend.get_indirect_fire_mission_areas(side)

    def own_weapon_inventory(
        self, side: str,
    ) -> tuple[Mapping[str, object], ...]:
        """Return a detached OWN-only inventory from authoritative state."""

        self._require_active()
        return self.backend.own_weapon_inventory(side)

    def get_weapon_catalog(self) -> Mapping[str, object]:
        self._require_active()
        return self.backend.get_weapon_catalog()

    def query_fire_effect_from_projection(
        self, side: str, observation: Mapping[str, object],
        source_frame: Mapping[str, object],
        valid_actions: Sequence[Mapping[str, object]],
        own_weapon_inventory: Sequence[Mapping[str, object]],
        *, query: FireEffectQuery,
    ) -> Mapping[str, object]:
        self._require_active()
        return self.backend.query_fire_effect_from_projection(
            side, observation, source_frame, valid_actions,
            own_weapon_inventory, query=query,
        )

    def direct_fire_intent_action(
        self, side: str, actor_id: int, weapon_id: int,
        target_id: int | None = None, *, target_hex: int | None = None,
    ) -> EngineAction:
        """Build a side-authorized intent without pre-adjudicating rules."""

        self._require_active()
        return self.backend.direct_fire_intent_action(
            side, actor_id, weapon_id, target_id, target_hex=target_hex,
        )

    def indirect_fire_action(
        self,
        capability: IndirectFireCapability,
        planned_hex: int, *, target_id: int | None = None,
    ) -> EngineAction:
        """Build one Engine-validated explicit FIRE/INDIRECT action."""

        self._require_active()
        return self.backend.indirect_fire_action(capability, planned_hex, target_id=target_id)

    def step(self, action_batch: Mapping[
            str, AgentAction | EngineAction | None] | Sequence[EngineAction], *,
             delta_time_seconds: int | float | None = None,
             delta_time_ms: int | None = None) -> StepResult:
        milliseconds = resolve_delta_time_ms(
            delta_time_seconds=delta_time_seconds,
            delta_time_ms=delta_time_ms,
        )
        return self._step_ms(action_batch, delta_time_ms=milliseconds)

    def step_ms(self, action_batch: Mapping[
            str, AgentAction | EngineAction | None] | Sequence[EngineAction], *,
                delta_time_ms: int) -> StepResult:
        """Advance the backend once using exact integer milliseconds."""

        return self.step(
            action_batch,
            delta_time_ms=require_positive_milliseconds(delta_time_ms),
        )

    def _step_ms(self, action_batch: Mapping[
            str, AgentAction | EngineAction | None] | Sequence[EngineAction], *,
                 delta_time_ms: int) -> StepResult:
        self._require_active()
        if isinstance(action_batch, Sequence) and not isinstance(
                action_batch, (str, bytes)):
            if not all(isinstance(item, EngineAction) for item in action_batch):
                raise TypeError("ordered session actions must be EngineAction values")
            result = self.backend.step_ms(
                tuple(action_batch), delta_time_ms=delta_time_ms
            )
            self._state_revision += 1
            return result
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
        result = self.backend.step_ms(values, delta_time_ms=delta_time_ms)
        self._state_revision += 1
        return result

    def stage(self) -> Stage:
        self._require_active()
        return self.backend.result().final_stage

    def is_terminal(self) -> bool:
        self._require_active()
        return self.backend.is_terminal()

    def game_status(self) -> GameStatus:
        """Read lifecycle status, retaining compatibility with older backends.

        ``game_status`` is an optional backend hook, not a new requirement on
        EngineBackend. Backends without it retain their authoritative result.
        """
        self._require_active(allow_closed=True)
        # A custom session may override result independently of its backend.
        # Inherited status must preserve that existing authority boundary.
        if type(self) is not GameSession:
            return GameStatus.from_result(self.result())
        query = getattr(self.backend, "game_status", None)
        value = (query() if callable(query)
                 else GameStatus.from_result(self.result()))
        if type(value) is not GameStatus:
            raise TypeError("backend game_status must return an exact GameStatus")
        return value

    def result(self) -> GameResult:
        self._require_active(allow_closed=True)
        return self.backend.result()

    def sim_time_ms(self) -> int:
        """Return the current authoritative integer simulation clock."""

        self._require_active()
        return self.backend.sim_time_ms()

    def state_revision(self) -> int:
        """Return this session instance's monotonic derived-data epoch."""

        self._require_active()
        return self._state_revision

    def snapshot(self) -> Mapping[str, object]:
        self._require_active()
        snapshot = self.backend.snapshot()
        contract = getattr(self, "source_frame_compatibility_version", None)
        if contract is not None:
            snapshot = dict(snapshot)
            snapshot["source_frame_compatibility_version"] = contract
        return snapshot

    def restore(self, snapshot: Mapping[str, object]) -> WorldState:
        if self._closed:
            raise RuntimeError("session_closed")
        from wargame_native_engine.compat.source_frame.contract import (
            SOURCE_CONTRACT_KEY, LEGACY_SOURCE_CONTRACT, require_source_contract,
        )
        contract = require_source_contract(snapshot.get(SOURCE_CONTRACT_KEY, LEGACY_SOURCE_CONTRACT))
        result = self.backend.restore(snapshot)
        if SOURCE_CONTRACT_KEY in snapshot or hasattr(self, SOURCE_CONTRACT_KEY):
            self.source_frame_compatibility_version = contract
        self.seed = int(snapshot["seed"])
        identity = {
            "scenario_hash": self.scenario.scenario_hash,
            "seed": self.seed,
        }
        backend_identity = getattr(self.backend, "determinism_identity", {})
        if backend_identity:
            identity["backend_determinism_identity"] = backend_identity
        self.session_id = "session-" + canonical_hash(identity)[:24]
        self._reset = True
        self._state_revision += 1
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
