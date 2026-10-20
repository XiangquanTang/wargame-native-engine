"""In-process lifecycle ownership for isolated Native control sessions."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import tempfile
from threading import RLock
from typing import Any

from wargame_native_engine.analysis import (
    EightFactorAnalyzer,
    LiveAnalyzedSession,
)
from wargame_native_engine.analysis.models import FrameAnalysis
from wargame_native_engine.agents.protocol import (
    AgentResetContext,
    AgentStepContext,
)
from wargame_native_engine.core.action import EngineAction, ValidAction
from wargame_native_engine.core.fire_capability import IndirectFireCapability
from wargame_native_engine.core.observation import SideObservation
from wargame_native_engine.core.result import GameResult
from wargame_native_engine.core.tactical_overlay import UnitTacticalOverlay
from wargame_native_engine.gateway.models import (
    CommandGatewayError,
    ControllerView,
    SessionDescriptor,
    SessionPublication,
)
from wargame_native_engine.gateway.controller_decision import (
    build_controller_decision_core,
)
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.io.scenario import native_map_identity_hash
from wargame_native_engine.movement.models import MovementMode, RoutePlan
from wargame_native_engine.replay import SourceFrameRecorder
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)
from wargame_native_engine.core.time import (
    require_positive_milliseconds,
    seconds_to_milliseconds,
)
from wargame_native_engine.session.deployment import SessionLifecycleState
from wargame_native_engine.version import ENGINE_VERSION
from wargame_native_engine.projection.decision_semantic import (
    DecisionProjectionContractError,
    DecisionSide,
    DecisionSnapshotIdentity,
)


EnvironmentFactory = Callable[[], SourceCompatibleEnvironment]
AnalyzerFactory = Callable[[], EightFactorAnalyzer]
_STARTED_AT = "1970-01-01T00:00:00+00:00"


@dataclass(slots=True)
class _ManagedSession:
    external_id: str
    environment: SourceCompatibleEnvironment
    live: LiveAnalyzedSession
    recorder: SourceFrameRecorder | None
    seed: int
    step_index: int = 0
    failed: str | None = None
    first_failure: BaseException | None = None
    finalized: bool = False
    controller_view_cache: dict[
        str, tuple[tuple[object, ...], ControllerView]
    ] = field(default_factory=dict)
    controller_view_cache_revision: int | None = None
    controller_view_requests: int = 0
    controller_view_cache_hits: int = 0
    controller_view_cache_misses: int = 0
    controller_view_cache_bypasses: int = 0
    controller_view_builds: int = 0
    planning_world_hash_cache: tuple[int, str] | None = None
    planning_world_hash_cache_hits: int = 0
    planning_world_hash_cache_misses: int = 0
    planning_world_hash_computations: int = 0
    lock: RLock = field(default_factory=RLock)


class SessionManager:
    """Own isolated environments without exposing them to controllers."""

    def __init__(
        self,
        environment_factory: EnvironmentFactory,
        *,
        storage_root: str | Path,
        analyzer_factory: AnalyzerFactory | None = None,
        reuse_planning_world_hash: bool = True,
    ) -> None:
        if not callable(environment_factory):
            raise TypeError("environment_factory must be callable")
        self._environment_factory = environment_factory
        self._analyzer_factory = analyzer_factory or EightFactorAnalyzer
        if type(reuse_planning_world_hash) is not bool:
            raise TypeError("reuse_planning_world_hash must be an exact bool")
        self._reuse_planning_world_hash = reuse_planning_world_hash
        self.storage_root = Path(storage_root)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, _ManagedSession] = {}
        self._sequence = 0
        self._lock = RLock()

    def create_session(
        self,
        *,
        seed: int = 42,
        record: bool = True,
    ) -> SessionDescriptor:
        if type(seed) is not int:
            raise TypeError("seed must be an int (not bool)")
        with self._lock:
            self._sequence += 1
            external_id = f"native-session-{self._sequence:06d}"
        environment = self._environment_factory()
        if not isinstance(environment, SourceCompatibleEnvironment):
            raise TypeError(
                "environment_factory must return SourceCompatibleEnvironment"
            )
        live = LiveAnalyzedSession(
            environment,
            analyzer=self._analyzer_factory(),
            retain_update_timeline=False,
            retain_frame_analyses=False,
        )
        recorder: SourceFrameRecorder | None = None
        try:
            update = live.setup(seed)
            if record:
                recorder = SourceFrameRecorder(
                    self.storage_root / external_id / "facts"
                )
                recorder.start(
                    _recorder_metadata(environment, update.green_frame, seed),
                    started_at=_STARTED_AT,
                )
                recorder.record_initial(
                    update.green_frame,
                    initial_state_hash=environment.initial_state_hash,
                )
        except BaseException:
            environment.session.close()
            raise
        managed = _ManagedSession(
            external_id=external_id,
            environment=environment,
            live=live,
            recorder=recorder,
            seed=seed,
        )
        with self._lock:
            self._sessions[external_id] = managed
        native_result = environment.session.result()
        return SessionDescriptor(
            session_id=external_id,
            step_index=0,
            terminal=environment.session.is_terminal(),
            finalized=False,
            frame_index=0,
            sim_time_ms=self.sim_time_ms(external_id),
            lifecycle_state=_managed_lifecycle(managed).value,
            terminated=native_result.terminal,
            truncated=native_result.truncated,
        )

    def descriptor(self, session_id: str) -> SessionDescriptor:
        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            native_result = managed.environment.session.result()
            return SessionDescriptor(
                session_id=managed.external_id,
                step_index=managed.step_index,
                terminal=managed.environment.session.is_terminal(),
                finalized=managed.finalized,
                frame_index=managed.step_index,
                sim_time_ms=_managed_sim_time_ms(managed),
                lifecycle_state=_managed_lifecycle(managed).value,
                terminated=native_result.terminal,
                truncated=native_result.truncated,
            )

    def native_result(self, session_id: str) -> GameResult:
        """Return the immutable authoritative result without exposing World."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            return deepcopy(managed.environment.session.result())

    def frame_analysis(
        self, session_id: str, frame_index: int
    ) -> FrameAnalysis:
        """Return one detached committed GREEN analysis, never live World state."""

        if type(frame_index) is not int or frame_index < 0:
            raise TypeError("frame_index must be a non-negative exact int")
        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            try:
                return managed.live.frame_analysis_at(frame_index)
            except IndexError as exc:
                raise RuntimeError("committed_frame_analysis_missing") from exc

    def controller_view(
        self,
        session_id: str,
        side: str,
        *,
        decision_window_index: int | None = None,
        frozen_valid_actions: Sequence[ValidAction] | None = None,
        frozen_valid_actions_revision: int | None = None,
        frame_index: int | None = None,
        sim_time_ms: int | None = None,
    ) -> ControllerView:
        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            return self._controller_view(
                managed,
                side,
                decision_window_index=decision_window_index,
                frozen_valid_actions=frozen_valid_actions,
                frozen_valid_actions_revision=frozen_valid_actions_revision,
                frame_index=frame_index,
                sim_time_ms=sim_time_ms,
            )

    def controller_view_cache_stats(
        self, session_id: str,
    ) -> dict[str, int]:
        """Return small diagnostic counters without exposing cached views."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            return {
                "requests": managed.controller_view_requests,
                "hits": managed.controller_view_cache_hits,
                "misses": managed.controller_view_cache_misses,
                "bypasses": managed.controller_view_cache_bypasses,
                "builds": managed.controller_view_builds,
                "entries": len(managed.controller_view_cache),
            }

    def planning_world_hash_cache_stats(
        self, session_id: str,
    ) -> dict[str, int | bool]:
        """Return bounded counters for the per-session planning guard cache."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            return {
                "enabled": self._reuse_planning_world_hash,
                "hits": managed.planning_world_hash_cache_hits,
                "misses": managed.planning_world_hash_cache_misses,
                "hash_computations": managed.planning_world_hash_computations,
                "entries": int(managed.planning_world_hash_cache is not None),
            }

    def valid_actions(
        self,
        session_id: str,
        side: str,
    ) -> tuple[ValidAction, ...]:
        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            _require_side(managed, side)
            return tuple(managed.environment.session.valid_actions(side))

    def sim_time_ms(self, session_id: str) -> int:
        """Return the authoritative integer simulation clock, never wall time."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            return _managed_sim_time_ms(managed)

    def public_static_map(self, session_id: str) -> Mapping[str, object]:
        """Return the immutable declared Native hex set and its identity.

        This is static scenario data, not live WorldState.  Coordinate grids
        without explicit Native hex declarations are intentionally rejected;
        callers must not guess cells from width/height.
        """

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            native_map = managed.environment.session.scenario.map
            cells = tuple(sorted(item.hex_id for item in native_map.hexes))
            if not cells:
                raise CommandGatewayError(
                    "public_static_hex_map_unavailable",
                    "scenario has no explicit Native hex declarations",
                )
            if len(set(cells)) != len(cells):
                raise RuntimeError("scenario map contains duplicate Native hex IDs")
            return {
                "schema_version": "native-public-static-hex-map/1.0",
                "map_id": native_map.map_id,
                "map_hash": native_map_identity_hash(native_map),
                "cell_set_rule": "ALL_DECLARED_NATIVE_HEXES",
                "valid_hex_ids": cells,
            }

    def public_native_conventions(self, session_id: str) -> Mapping[str, object]:
        """Configured public Native conventions; no World or RNG access."""
        managed = self._managed(session_id)
        policy = getattr(managed.environment.session.backend, "mounted_position_policy", None)
        return {} if policy is None else {"mounted_position_policy": policy}

    def public_scenario_identity(self, session_id: str) -> Mapping[str, str]:
        """Return immutable scenario/map identity without exposing WorldState."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            scenario = managed.environment.session.scenario
            return {
                "scenario_id": scenario.scenario_id,
                "scenario_hash": scenario.scenario_hash,
                "map_id": scenario.map.map_id,
                "map_hash": native_map_identity_hash(scenario.map),
            }

    def plan_move(
        self,
        session_id: str,
        side: str,
        unit_id: int,
        destination_hex: int,
        movement_mode: MovementMode | str = MovementMode.NORMAL,
    ) -> tuple[RoutePlan, str]:
        """Plan under the Session lock without exposing mutable state."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            _require_side(managed, side)
            session = managed.environment.session
            revision = session.state_revision()
            cached = managed.planning_world_hash_cache
            if (
                self._reuse_planning_world_hash
                and cached is not None
                and cached[0] == revision
            ):
                managed.planning_world_hash_cache_hits += 1
                state_hash = cached[1]
            else:
                managed.planning_world_hash_cache_misses += 1
                state_hash = canonical_hash(session.snapshot()["world"])
                managed.planning_world_hash_computations += 1
            route = session.plan_move(
                side, unit_id, destination_hex, movement_mode
            )
            after_hash = canonical_hash(session.snapshot()["world"])
            managed.planning_world_hash_computations += 1
            if after_hash != state_hash:
                raise RuntimeError("move_planning_modified_world_state")
            after_revision = session.state_revision()
            if after_revision != revision:
                raise RuntimeError("move_planning_modified_state_revision")
            managed.planning_world_hash_cache = (
                (after_revision, after_hash)
                if self._reuse_planning_world_hash else None
            )
            return deepcopy(route), state_hash

    def query_unit_tactical_overlay(
        self,
        session_id: str,
        side: str,
        unit_id: int,
    ) -> UnitTacticalOverlay:
        """Run one detached, mutation-guarded tactical-space query."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            _require_side(managed, side)
            session = managed.environment.session
            before = deepcopy(session.snapshot())
            result = session.query_unit_tactical_overlay(side, unit_id)
            if session.snapshot() != before:
                raise RuntimeError("tactical_overlay_query_modified_engine_state")
            return deepcopy(result)

    def get_indirect_fire_capabilities(
        self,
        session_id: str,
        side: str,
    ) -> tuple[IndirectFireCapability, ...]:
        """Run one mutation-guarded, side-bound authority query."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            _require_side(managed, side)
            session = managed.environment.session
            before = deepcopy(session.snapshot())
            result = session.get_indirect_fire_capabilities(side)
            if session.snapshot() != before:
                raise RuntimeError(
                    "indirect_fire_capability_query_modified_engine_state"
                )
            return deepcopy(result)

    def get_indirect_fire_active_areas(
        self,
        session_id: str,
        side: str,
    ) -> tuple[object, ...]:
        """Return a mutation-guarded, side-filtered ACTIVE-area projection."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            _require_side(managed, side)
            session = managed.environment.session
            before = deepcopy(session.snapshot())
            result = session.get_indirect_fire_active_areas(side)
            if session.snapshot() != before:
                raise RuntimeError(
                    "indirect_fire_active_area_query_modified_engine_state"
                )
            return deepcopy(result)

    def get_indirect_fire_mission_areas(
        self,
        session_id: str,
        side: str,
    ) -> tuple[object, ...]:
        """Return a mutation-guarded, side-filtered mission-area projection."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            _require_side(managed, side)
            session = managed.environment.session
            before = deepcopy(session.snapshot())
            result = session.get_indirect_fire_mission_areas(side)
            if session.snapshot() != before:
                raise RuntimeError(
                    "indirect_fire_mission_area_query_modified_engine_state"
                )
            return deepcopy(result)

    def _operation_execution_facts(
        self,
        session_id: str,
        side: str,
        *,
        first_frame_index: int | None = None,
        last_frame_index: int | None = None,
    ) -> Mapping[str, object]:
        """Return committed per-frame OWN facts for the trusted tracker.

        The projection was built while the already-required post-step World
        was current.  This read therefore performs no snapshot or World
        reconstruction and deliberately omits raw attributes, RNG, enemy
        records, damage evidence, and full replay history.
        """

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            _require_side(managed, side)
            frames = managed.environment.execution_fact_frames(
                side,
                first_frame_index=first_frame_index,
                last_frame_index=last_frame_index,
            )
            if first_frame_index is None and frames:
                frames = (frames[-1],)
            expected = None
            if first_frame_index is not None and last_frame_index is not None:
                expected = tuple(range(first_frame_index, last_frame_index + 1))
            actual = tuple(int(value["frame_index"]) for value in frames)
            return {
                "schema_version": "native-operation-execution-fact-batch/1.0",
                "side": side,
                "first_frame_index": first_frame_index,
                "last_frame_index": last_frame_index,
                "complete_range": expected is None or actual == expected,
                "frames": frames,
            }

    def plan_indirect_fire(
        self,
        session_id: str,
        side: str,
        shooter_id: int,
        weapon_id: int,
        target_hex: int,
    ) -> tuple[EngineAction, str]:
        """Create one mutation-guarded, Engine-authorized INDIRECT action."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            _require_side(managed, side)
            session = managed.environment.session
            before = deepcopy(session.snapshot())
            state_hash = canonical_hash(before["world"])
            capability = next((
                value
                for value in session.get_indirect_fire_capabilities(side)
                if value.shooter_id == shooter_id
                and value.weapon_id == weapon_id
                and value.status.value == "AVAILABLE"
            ), None)
            if capability is None:
                raise ValueError("CAPABILITY_BLOCKED")
            action = session.indirect_fire_action(capability, target_hex)
            if session.snapshot() != before:
                raise RuntimeError(
                    "indirect_fire_planning_modified_engine_state"
                )
            return deepcopy(action), state_hash

    def plan_direct_fire_intent(
        self,
        session_id: str,
        side: str,
        actor_id: int,
        weapon_id: int,
        target_id: int,
    ) -> tuple[EngineAction, str]:
        """Create an OWN/visibility-authorized DIRECT intent without rule preview."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            _require_side(managed, side)
            session = managed.environment.session
            revision = session.state_revision()
            action = session.direct_fire_intent_action(
                side, actor_id, weapon_id, target_id,
            )
            if session.state_revision() != revision:
                raise RuntimeError("direct_fire_intent_query_modified_engine_state")
            identity_hash = canonical_hash({
                "session_id": session_id,
                "state_revision": revision,
                "side": side,
            })
            return deepcopy(action), identity_hash

    def query_unit_tactical_overlay_from_projection(
        self,
        session_id: str,
        side: str,
        unit_id: int,
        view: ControllerView,
    ) -> UnitTacticalOverlay:
        """Evaluate a frozen side view without acquiring the tick lock.

        The Engine implementation used here reads only immutable scenario,
        LOS, weapon-profile data and the detached ``ControllerView``.  It does
        not inspect live ``WorldState``; consequently it remains safe while a
        realtime tick owns the managed-session advancement lock.
        """

        managed = self._managed(session_id)
        self._require_usable(managed)
        _require_side(managed, side)
        if view.session_id != managed.external_id:
            raise ValueError("controller view belongs to another session")
        if view.perspective.lower() != side:
            raise ValueError("controller view belongs to another side")
        session = managed.environment.session
        result = session.query_unit_tactical_overlay_from_projection(
            side, unit_id, view.observation, view.source_frame, view.valid_actions
        )
        return deepcopy(result)

    def agent_reset_context(
        self,
        session_id: str,
        side: str,
        agent_id: str,
    ) -> AgentResetContext:
        """Build one detached side-only reset context for a trusted Agent host."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            _require_side(managed, side)
            session = managed.environment.session
            return AgentResetContext(
                session.session_id,
                session.scenario.scenario_id,
                side,
                managed.seed,
                session.backend.ruleset.ruleset_version,
                str(agent_id),
            )

    def agent_step_input(
        self,
        session_id: str,
        side: str,
        agent_id: str,
    ) -> tuple[SideObservation, tuple[ValidAction, ...], AgentStepContext]:
        """Return detached side observation/actions without GREEN state."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            _require_side(managed, side)
            session = managed.environment.session
            observation = deepcopy(session.observe(side))
            valid_actions = deepcopy(tuple(session.valid_actions(side)))
            context = AgentStepContext(
                session.session_id,
                side,
                observation.tick,
                observation.stage,
                managed.seed,
                str(agent_id),
            )
            return observation, valid_actions, context

    def advance(
        self,
        session_id: str,
        actions: Sequence[EngineAction],
        *,
        delta_time_seconds: int | float,
        decision_window_index: int | None = None,
        frame_interval_offset: int | None = None,
    ) -> SessionDescriptor:
        return self.advance_ms(
            session_id,
            actions,
            delta_time_ms=seconds_to_milliseconds(delta_time_seconds),
            decision_window_index=decision_window_index,
            frame_interval_offset=frame_interval_offset,
        )

    def advance_ms(
        self,
        session_id: str,
        actions: Sequence[EngineAction],
        *,
        delta_time_ms: int,
        decision_window_index: int | None = None,
        frame_interval_offset: int | None = None,
    ) -> SessionDescriptor:
        delta_time_ms = require_positive_milliseconds(delta_time_ms)
        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            _clear_controller_view_cache(managed)
            if managed.finalized:
                raise CommandGatewayError(
                    "session_finalized",
                    f"session {session_id} facts were already published",
                )
            if managed.environment.session.is_terminal():
                raise CommandGatewayError(
                    "session_terminal",
                    f"session {session_id} is already terminal",
                )
            revision_before = managed.environment.session.state_revision()
            failure_reason = "committed_step_interrupted"
            try:
                update = managed.live.step(
                    tuple(actions), delta_time_ms=delta_time_ms
                )
                if type(update.done) is not bool:
                    raise TypeError("live update done must be an exact bool")
                step_record = managed.environment.last_step_record
                if step_record is None:
                    failure_reason = "missing_committed_step_record"
                    raise RuntimeError(
                        "environment did not expose committed step record"
                    )
                step_record = json_clone(step_record)
                if decision_window_index is not None:
                    step_record["decision_window_index"] = decision_window_index
                if frame_interval_offset is not None:
                    step_record["frame_interval_offset"] = frame_interval_offset
                if managed.recorder is not None:
                    failure_reason = "recorder_desynchronized"
                    managed.recorder.record_step(
                        update.green_frame,
                        step_record,
                    )
                failure_reason = "committed_step_interrupted"
                managed.step_index = update.frame_index
                native_result = managed.environment.session.result()
            except BaseException as exc:
                advanced = (
                    managed.environment.session.state_revision()
                    != revision_before
                )
                if managed.live.desynchronized:
                    _mark_failed(managed, "analysis_desynchronized", exc)
                elif advanced:
                    _mark_failed(managed, failure_reason, exc)
                raise
            return SessionDescriptor(
                session_id=managed.external_id,
                step_index=managed.step_index,
                terminal=update.done,
                finalized=False,
                frame_index=managed.step_index,
                sim_time_ms=_managed_sim_time_ms(managed),
                lifecycle_state=_managed_lifecycle(managed).value,
                terminated=native_result.terminal,
                truncated=native_result.truncated,
            )

    def last_action_results(
        self, session_id: str, side: str,
    ) -> tuple[Mapping[str, Any], ...]:
        """Return only one side's ephemeral results from the latest step."""

        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            _require_side(managed, side)
            return tuple(
                json_clone(value)
                for value in managed.environment.last_action_results
                if value.get("side") == side
            )

    def publish_session(
        self,
        session_id: str,
        output_dir: str | Path,
        *,
        truncated: bool = False,
        overwrite: bool = False,
        control_audit_payload: bytes | None = None,
    ) -> SessionPublication:
        managed = self._managed(session_id)
        target = Path(output_dir)
        with managed.lock:
            self._require_usable(managed)
            if managed.recorder is None:
                raise CommandGatewayError(
                    "recording_disabled", "session has no Recorder"
                )
            done = managed.environment.session.is_terminal()
            if not done and not truncated:
                raise CommandGatewayError(
                    "session_active",
                    "active session publication requires truncated=True",
                )
            if managed.finalized:
                raise CommandGatewayError(
                    "session_already_published",
                    f"session {session_id} was already finalized",
                )
            if os.path.lexists(target) and not overwrite:
                raise FileExistsError(f"session output already exists: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            outcome = dict(managed.environment.session.result().to_dict())
            if truncated and not done:
                outcome["truncated"] = True
            manifest = managed.recorder.finalize(
                outcome,
                final_state_hash=str(outcome["world_hash"]),
            )
            staging = Path(tempfile.mkdtemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
            ))
            try:
                replay_path = managed.recorder.package_zip(staging / "replay.zip")
                managed.live.write_analysis_jsonl(
                    staging / "frame_analysis.native.jsonl"
                )
                if control_audit_payload is not None:
                    _write_bytes(
                        staging / "control_audit.native.jsonl",
                        bytes(control_audit_payload),
                    )
                _publish_directory(staging, target, overwrite=overwrite)
            finally:
                _remove_path(staging)
            managed.finalized = True
            _clear_controller_view_cache(managed)
            return SessionPublication(
                output_dir=target,
                replay_path=target / "replay.zip",
                analysis_path=target / "frame_analysis.native.jsonl",
                frame_count=int(manifest["frame_count"]),
                step_count=int(manifest["step_count"]),
                terminal=outcome.get("terminal", False),
                truncated=outcome.get("truncated", False),
                control_audit_path=(
                    None
                    if control_audit_payload is None
                    else target / "control_audit.native.jsonl"
                ),
            )

    def close_session(self, session_id: str) -> None:
        managed = self._managed(session_id)
        with managed.lock:
            _clear_controller_view_cache(managed)
            managed.environment.session.close()
            managed.live.close()
        with self._lock:
            self._sessions.pop(session_id, None)

    def _managed(self, session_id: str) -> _ManagedSession:
        with self._lock:
            try:
                return self._sessions[str(session_id)]
            except KeyError as exc:
                raise CommandGatewayError(
                    "session_not_found", f"unknown session {session_id!r}"
                ) from exc

    @staticmethod
    def _require_usable(managed: _ManagedSession) -> None:
        if managed.failed is not None:
            error = CommandGatewayError(
                "session_failed",
                f"session requires replacement: {managed.failed}",
            )
            if managed.first_failure is not None:
                raise error from managed.first_failure
            raise error
        if managed.environment.session.closed:
            raise CommandGatewayError(
                "session_closed", f"session {managed.external_id} is closed"
            )

    @staticmethod
    def _controller_view(
        managed: _ManagedSession,
        side: str,
        *,
        decision_window_index: int | None = None,
        frozen_valid_actions: Sequence[ValidAction] | None = None,
        frozen_valid_actions_revision: int | None = None,
        frame_index: int | None = None,
        sim_time_ms: int | None = None,
    ) -> ControllerView:
        _require_side(managed, side)
        session = managed.environment.session
        managed.controller_view_requests += 1
        state_revision = session.state_revision()
        if managed.controller_view_cache_revision != state_revision:
            managed.controller_view_cache.clear()
            managed.controller_view_cache_revision = state_revision
        current_frame = managed.step_index
        current_sim_time = _managed_sim_time_ms(managed)
        window = current_frame if decision_window_index is None else (
            decision_window_index
        )
        bound_frame = current_frame if frame_index is None else frame_index
        bound_time = current_sim_time if sim_time_ms is None else sim_time_ms
        for value, name in (
            (window, "decision_window_index"),
            (bound_frame, "frame_index"),
            (bound_time, "sim_time_ms"),
        ):
            if type(value) is not int or value < 0:
                raise DecisionProjectionContractError(
                    f"{name} must be a non-negative exact int"
                )
        if bound_frame != current_frame:
            raise DecisionProjectionContractError(
                "ControllerView frame is not the current frozen frame"
            )
        if bound_time != current_sim_time:
            raise DecisionProjectionContractError(
                "ControllerView sim time is not the current frozen time"
            )
        if (
            frozen_valid_actions_revision is not None
            and (
                type(frozen_valid_actions_revision) is not int
                or frozen_valid_actions_revision < 0
            )
        ):
            raise DecisionProjectionContractError(
                "frozen_valid_actions_revision must be a non-negative exact int"
            )
        cacheable = (
            frozen_valid_actions is None
            or frozen_valid_actions_revision is not None
        ) and not managed.finalized
        terminal = session.is_terminal()
        result = session.result()
        cache_key = (
            managed.environment.source_contract,
            state_revision,
            side,
            window,
            bound_frame,
            bound_time,
            frozen_valid_actions_revision,
            terminal,
            result.terminal,
            result.truncated,
            result.winner,
            result.outcome,
        )
        if cacheable:
            cached = managed.controller_view_cache.get(side)
            if cached is not None and cached[0] == cache_key:
                managed.controller_view_cache_hits += 1
                return deepcopy(cached[1])
            managed.controller_view_cache_misses += 1
        else:
            managed.controller_view_cache_bypasses += 1

        observation_value = session.observe(side)
        before_inventory_revision = session.state_revision()
        own_weapon_inventory = session.own_weapon_inventory(side)
        if session.state_revision() != before_inventory_revision:
            raise RuntimeError("weapon_inventory_query_modified_engine_state")
        actions = tuple(
            session.valid_actions(side)
            if frozen_valid_actions is None
            else frozen_valid_actions
        )
        if any(type(value) is not ValidAction for value in actions):
            raise TypeError("frozen_valid_actions must contain ValidAction values")
        source_frame = (
            managed.environment.last_bundle.red
            if side == "red"
            else managed.environment.last_bundle.blue
        )
        valid_actions = tuple(
            json_clone(item.to_dict())
            for item in actions
        )
        snapshot = DecisionSnapshotIdentity(
            side=DecisionSide(side),
            decision_window_index=window,
            frame_index=bound_frame,
            sim_time_ms=bound_time,
            stage=observation_value.stage,
            terminated=result.terminal,
            truncated=result.truncated,
        )
        core = build_controller_decision_core(
            session=session,
            observation=observation_value,
            snapshot_identity=snapshot,
            frozen_valid_actions=actions,
        )
        value = ControllerView(
            session_id=managed.external_id,
            perspective=side.upper(),
            step_index=window,
            observation=json_clone(observation_value.to_dict()),
            source_frame=json_clone(source_frame),
            valid_actions=valid_actions,
            terminal=terminal,
            winner=None if not terminal else result.winner,
            outcome=None if not terminal else result.outcome,
            frame_index=bound_frame,
            sim_time_ms=bound_time,
            terminated=result.terminal,
            truncated=result.truncated,
            decision_semantic=core.decision_semantic,
            action_catalog=core.action_catalog,
            own_weapon_inventory=own_weapon_inventory,
        )
        managed.controller_view_builds += 1
        if cacheable:
            # The template never crosses the manager boundary.  Keep a deep
            # copy so mutation of this miss result cannot poison later hits.
            managed.controller_view_cache[side] = (cache_key, deepcopy(value))
        return value


def _managed_sim_time_ms(managed: _ManagedSession) -> int:
    value = managed.environment.session.sim_time_ms()
    if type(value) is not int or value < 0:
        raise RuntimeError("invalid_authoritative_sim_time")
    return value


def _clear_controller_view_cache(managed: _ManagedSession) -> None:
    managed.controller_view_cache.clear()
    managed.controller_view_cache_revision = None
    managed.planning_world_hash_cache = None


def _mark_failed(
    managed: _ManagedSession, reason: str, error: BaseException,
) -> None:
    """Retain the first fatal cause without consulting historical data."""

    if managed.failed is None:
        managed.failed = reason
        managed.first_failure = error


def _managed_lifecycle(managed: _ManagedSession) -> SessionLifecycleState:
    if managed.environment.session.closed:
        return SessionLifecycleState.CLOSED
    if managed.finalized:
        return SessionLifecycleState.PUBLISHED
    if managed.environment.session.is_terminal():
        return SessionLifecycleState.TERMINATED
    return SessionLifecycleState.RUNNING


def _require_side(managed: _ManagedSession, side: str) -> None:
    if side not in managed.environment.session.scenario.side_order:
        raise CommandGatewayError(
            "invalid_perspective", f"unsupported controller side {side!r}"
        )


def _recorder_metadata(
    environment: SourceCompatibleEnvironment,
    green_frame: Mapping[str, Any],
    seed: int,
) -> Mapping[str, Any]:
    native = green_frame["extra"]["native"]
    scenario = environment.session.scenario
    value = {
        "scenario_id": scenario.scenario_id,
        "scenario_hash": scenario.scenario_hash,
        "terrain_id": green_frame["terrain_id"],
        "episode_id": environment.session.session_id,
        "engine_version": ENGINE_VERSION,
        "ruleset_id": native["ruleset_id"],
        "ruleset_version": native["ruleset_version"],
        "ruleset_hash": native["ruleset_hash"],
        "adjudication_table_set_id": native["adjudication_table_set_id"],
        "adjudication_table_set_version": native[
            "adjudication_table_set_version"
        ],
        "adjudication_table_set_hash": native[
            "adjudication_table_set_hash"
        ],
        "movement_policy_id": native["movement_policy_id"],
        "movement_policy_version": native["movement_policy_version"],
        "movement_policy_hash": native["movement_policy_hash"],
        "perspective_policy_id": native["perspective_policy_id"],
        "perspective_policy_version": native["perspective_policy_version"],
        "perspective_policy_hash": native["perspective_policy_hash"],
        "episode_seed": seed,
        "max_ticks": environment.session.max_ticks,
    }
    policy = getattr(environment.session.backend, "mounted_position_policy", None)
    if policy is not None:
        value["mounted_position_policy"] = policy
    if "source_frame_compatibility_version" in native:
        value["source_frame_compatibility_version"] = native["source_frame_compatibility_version"]
    if "map_runtime_data" in native:
        value["map_runtime_data"] = json_clone(native["map_runtime_data"])
    for name in (
        "boarding_type_assignment_authority_version",
        "boarding_type_assignment_authority_hash",
        "boarding_type_assignment_policy",
        "operator_lifecycle_authority_version",
        "operator_lifecycle_authority_hash",
        "loitering_munition_attack_authority_version",
        "loitering_munition_attack_authority_hash",
    ):
        if name in native:
            value[name] = native[name]
    return value


def _write_bytes(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_directory(staging: Path, target: Path, *, overwrite: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not os.path.lexists(target):
        os.replace(staging, target)
        return
    if not overwrite:
        raise FileExistsError(f"session output already exists: {target}")
    backup = Path(tempfile.mkdtemp(
        prefix=f".{target.name}.", suffix=".backup", dir=target.parent,
    ))
    backup.rmdir()
    os.replace(target, backup)
    try:
        os.replace(staging, target)
    except BaseException:
        os.replace(backup, target)
        raise
    _remove_path(backup)


def _remove_path(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
