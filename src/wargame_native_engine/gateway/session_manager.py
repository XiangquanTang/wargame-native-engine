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
    LiveAnalysisDesynchronizedError,
    LiveAnalyzedSession,
)
from wargame_native_engine.agents.protocol import (
    AgentResetContext,
    AgentStepContext,
)
from wargame_native_engine.core.action import EngineAction, ValidAction
from wargame_native_engine.core.observation import SideObservation
from wargame_native_engine.gateway.models import (
    CommandGatewayError,
    ControllerView,
    SessionDescriptor,
    SessionPublication,
)
from wargame_native_engine.io.serialization import canonical_hash, json_clone
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
    finalized: bool = False
    lock: RLock = field(default_factory=RLock)


class SessionManager:
    """Own isolated environments without exposing them to controllers."""

    def __init__(
        self,
        environment_factory: EnvironmentFactory,
        *,
        storage_root: str | Path,
        analyzer_factory: AnalyzerFactory | None = None,
    ) -> None:
        if not callable(environment_factory):
            raise TypeError("environment_factory must be callable")
        self._environment_factory = environment_factory
        self._analyzer_factory = analyzer_factory or EightFactorAnalyzer
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

    def controller_view(self, session_id: str, side: str) -> ControllerView:
        managed = self._managed(session_id)
        with managed.lock:
            self._require_usable(managed)
            return self._controller_view(managed, side)

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
            snapshot = managed.environment.session.snapshot()
            state_hash = canonical_hash(snapshot["world"])
            route = managed.environment.session.plan_move(
                side, unit_id, destination_hex, movement_mode
            )
            if canonical_hash(
                managed.environment.session.snapshot()["world"]
            ) != state_hash:
                raise RuntimeError("move_planning_modified_world_state")
            return deepcopy(route), state_hash

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
            try:
                update = managed.live.step(
                    tuple(actions), delta_time_ms=delta_time_ms
                )
            except BaseException:
                try:
                    managed.live.current_timeline
                except LiveAnalysisDesynchronizedError:
                    managed.failed = "analysis_desynchronized"
                raise
            step_record = managed.environment.last_step_record
            if step_record is None:
                managed.failed = "missing_committed_step_record"
                raise RuntimeError("environment did not expose committed step record")
            step_record = json_clone(step_record)
            if decision_window_index is not None:
                step_record["decision_window_index"] = decision_window_index
            if frame_interval_offset is not None:
                step_record["frame_interval_offset"] = frame_interval_offset
            if managed.recorder is not None:
                try:
                    managed.recorder.record_step(
                        update.green_frame,
                        step_record,
                    )
                except BaseException:
                    managed.failed = "recorder_desynchronized"
                    raise
            managed.step_index = update.frame_index
            native_result = managed.environment.session.result()
            return SessionDescriptor(
                session_id=managed.external_id,
                step_index=managed.step_index,
                terminal=bool(update.done),
                finalized=False,
                frame_index=managed.step_index,
                sim_time_ms=_managed_sim_time_ms(managed),
                lifecycle_state=_managed_lifecycle(managed).value,
                terminated=native_result.terminal,
                truncated=native_result.truncated,
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
            analysis_payload = managed.live.analysis_jsonl().encode("utf-8")
            manifest = managed.recorder.finalize(
                outcome,
                final_state_hash=str(outcome["world_hash"]),
            )
            staging = Path(tempfile.mkdtemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
            ))
            try:
                replay_path = managed.recorder.package_zip(staging / "replay.zip")
                _write_bytes(
                    staging / "frame_analysis.native.jsonl",
                    analysis_payload,
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
            return SessionPublication(
                output_dir=target,
                replay_path=target / "replay.zip",
                analysis_path=target / "frame_analysis.native.jsonl",
                frame_count=int(manifest["frame_count"]),
                step_count=int(manifest["step_count"]),
                terminal=bool(outcome.get("terminal", False)),
                truncated=bool(outcome.get("truncated", False)),
                control_audit_path=(
                    None
                    if control_audit_payload is None
                    else target / "control_audit.native.jsonl"
                ),
            )

    def close_session(self, session_id: str) -> None:
        managed = self._managed(session_id)
        with managed.lock:
            managed.environment.session.close()
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
            raise CommandGatewayError(
                "session_failed",
                f"session requires replacement: {managed.failed}",
            )
        if managed.environment.session.closed:
            raise CommandGatewayError(
                "session_closed", f"session {managed.external_id} is closed"
            )

    @staticmethod
    def _controller_view(
        managed: _ManagedSession,
        side: str,
    ) -> ControllerView:
        _require_side(managed, side)
        session = managed.environment.session
        observation = session.observe(side).to_dict()
        source_frame = (
            managed.environment.last_bundle.red
            if side == "red"
            else managed.environment.last_bundle.blue
        )
        valid_actions = tuple(
            json_clone(item.to_dict())
            for item in session.valid_actions(side)
        )
        terminal = session.is_terminal()
        result = session.result()
        return ControllerView(
            session_id=managed.external_id,
            perspective=side.upper(),
            step_index=managed.step_index,
            observation=json_clone(observation),
            source_frame=json_clone(source_frame),
            valid_actions=valid_actions,
            terminal=terminal,
            winner=None if not terminal else result.winner,
            outcome=None if not terminal else result.outcome,
            frame_index=managed.step_index,
            sim_time_ms=_managed_sim_time_ms(managed),
            terminated=result.terminal,
            truncated=result.truncated,
        )


def _managed_sim_time_ms(managed: _ManagedSession) -> int:
    world = managed.environment.session.snapshot()["world"]
    value = world["elapsed_time_ms"]
    if type(value) is not int or value < 0:
        raise RuntimeError("invalid_authoritative_sim_time")
    return value


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
    if "map_runtime_data" in native:
        value["map_runtime_data"] = json_clone(native["map_runtime_data"])
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
