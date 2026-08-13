"""Single-match orchestration for detached local Agents and Native replay."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping, Sequence

from wargame_native_engine.agents.protocol import (
    Agent,
    AgentAction,
    AgentResetContext,
    AgentStepContext,
)
from wargame_native_engine.analysis import (
    EightFactorAnalyzer,
    LiveAnalysisDesynchronizedError,
    LiveAnalyzedSession,
    analyze_replay_to_jsonl,
)
from wargame_native_engine.analysis.replay_processor import (
    FRAME_ANALYSIS_JSONL_NAME,
)
from wargame_native_engine.core.action import EngineAction, ValidAction
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.serialization import file_sha256, json_clone
from wargame_native_engine.compat.source_frame.serialization import (
    canonical_source_json,
)
from wargame_native_engine.replay import SourceFrameRecorder
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)
from wargame_native_engine.version import ENGINE_VERSION


MATCH_SUMMARY_SCHEMA = "native-local-agent-match-summary/1.0"
MATCH_SUMMARY_NAME = "match_summary.native.json"
REPLAY_NAME = "replay.zip"
_DETERMINISTIC_STARTED_AT = "1970-01-01T00:00:00+00:00"


class LocalAgentMatchError(RuntimeError):
    """A match stopped before a publishable synchronized result existed."""

    def __init__(self, status: str, error: Mapping[str, Any]) -> None:
        self.status = status
        self.error = json_clone(error)
        super().__init__(f"local_agent_match_failed:{status}")


class AgentExecutionError(LocalAgentMatchError):
    """One local Agent failed during reset, action, or output validation."""

    def __init__(
        self,
        *,
        agent_id: str,
        perspective: str,
        step_index: int,
        phase: str,
        cause: BaseException,
        called_agent_ids: Sequence[str],
    ) -> None:
        self.agent_id = agent_id
        self.perspective = perspective
        self.step_index = step_index
        self.phase = phase
        self.cause_type = type(cause).__name__
        self.cause_message = str(cause)
        self.called_agent_ids = tuple(called_agent_ids)
        super().__init__("agent_error", {
            "agent_id": agent_id,
            "perspective": perspective,
            "step_index": step_index,
            "phase": phase,
            "cause_type": self.cause_type,
            "cause_message": self.cause_message,
            "called_agent_ids": list(self.called_agent_ids),
        })
        self.args = (
            "agent_execution_error:"
            f"{agent_id}:{perspective}:{step_index}:{phase}:"
            f"{self.cause_type}:{self.cause_message}",
        )


@dataclass(frozen=True, slots=True)
class LocalAgentMatchResult:
    output_dir: Path
    replay_path: Path
    analysis_path: Path
    summary_path: Path
    status: str
    seed: int
    step_count: int
    frame_count: int
    terminal: bool
    truncated: bool
    winner: str | None
    outcome: str
    strict_replay_match: bool | None
    online_offline_analysis_match: bool
    summary: Mapping[str, Any]


class LocalAgentMatchRunner:
    """Run exactly one local RED/BLUE match over the Source environment."""

    def __init__(
        self,
        *,
        environment: SourceCompatibleEnvironment,
        red_agent: Agent,
        blue_agent: Agent,
        analyzer: EightFactorAnalyzer | None = None,
        delta_time_seconds: int | float = 20,
    ) -> None:
        if not isinstance(environment, SourceCompatibleEnvironment):
            raise TypeError("environment must be a SourceCompatibleEnvironment")
        self.environment = environment
        self.agents: dict[str, Agent] = {
            "red": red_agent,
            "blue": blue_agent,
        }
        if tuple(environment.session.scenario.side_order) != ("red", "blue"):
            raise ValueError("local Agent runner requires RED/BLUE scenario sides")
        for side, agent in self.agents.items():
            if not isinstance(agent.agent_id, str) or not agent.agent_id.strip():
                raise ValueError(f"{side} agent_id must be a non-empty string")
        self.analyzer = analyzer if analyzer is not None else EightFactorAnalyzer()
        self.delta_time_seconds = delta_time_seconds

    def run(
        self,
        output_dir: str | Path,
        *,
        seed: int = 42,
        max_steps: int | None = None,
        overwrite: bool = False,
    ) -> LocalAgentMatchResult:
        if type(seed) is not int:
            raise TypeError("seed must be an int (not bool)")
        if max_steps is not None:
            if type(max_steps) is not int:
                raise TypeError("max_steps must be an int (not bool) or None")
            if max_steps < 0:
                raise ValueError("max_steps must be >= 0")

        target = Path(output_dir)
        if _path_exists(target) and not overwrite:
            raise FileExistsError(f"match output already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        ))
        try:
            try:
                summary = self._run_in_staging(
                    staging,
                    seed=seed,
                    max_steps=max_steps,
                )
                _publish_directory(staging, target, overwrite=overwrite)
            finally:
                self._close_agents()
        finally:
            _remove_path(staging)

        return LocalAgentMatchResult(
            output_dir=target,
            replay_path=target / REPLAY_NAME,
            analysis_path=target / FRAME_ANALYSIS_JSONL_NAME,
            summary_path=target / MATCH_SUMMARY_NAME,
            status=str(summary["status"]),
            seed=seed,
            step_count=int(summary["step_count"]),
            frame_count=int(summary["frame_count"]),
            terminal=bool(summary["terminal"]),
            truncated=bool(summary["truncated"]),
            winner=(None if summary["winner"] is None else str(summary["winner"])),
            outcome=str(summary["outcome"]),
            strict_replay_match=summary["strict_replay_match"],
            online_offline_analysis_match=bool(
                summary["online_offline_analysis_match"]
            ),
            summary=json_clone(summary),
        )

    def _run_in_staging(
        self,
        staging: Path,
        *,
        seed: int,
        max_steps: int | None,
    ) -> Mapping[str, Any]:
        live = LiveAnalyzedSession(self.environment, analyzer=self.analyzer)
        try:
            update = live.setup(seed)
        except BaseException as exc:
            self._raise_if_desynchronized(live, exc, phase="setup")
            raise

        recorder = SourceFrameRecorder(staging / ".recorder")
        recorder.start(
            self._recorder_metadata(update.green_frame, seed),
            started_at=_DETERMINISTIC_STARTED_AT,
        )
        recorder.record_initial(
            update.green_frame,
            initial_state_hash=self.environment.initial_state_hash,
        )
        self._reset_agents(seed)

        step_count = 0
        runner_truncated = False
        done = self.environment.session.is_terminal()
        while not done:
            if max_steps is not None and step_count >= max_steps:
                runner_truncated = True
                break
            joint_actions = self._joint_actions(step_index=step_count)
            try:
                update = live.step(
                    joint_actions,
                    delta_time_seconds=self.delta_time_seconds,
                )
            except BaseException as exc:
                self._raise_if_desynchronized(live, exc, phase="step")
                raise
            step_record = self.environment.last_step_record
            if step_record is None:
                raise RuntimeError("environment did not expose committed step record")
            recorder.record_step(update.green_frame, step_record)
            step_count += 1
            done = bool(update.done)

        outcome = self.environment.session.result().to_dict()
        manifest = recorder.finalize(
            outcome,
            final_state_hash=str(outcome["world_hash"]),
        )
        analysis_payload = live.analysis_jsonl().encode("utf-8")
        analysis_path = staging / FRAME_ANALYSIS_JSONL_NAME
        _write_bytes(analysis_path, analysis_payload)
        replay_path = recorder.package_zip(staging / REPLAY_NAME)

        offline_path = staging / ".offline-analysis.jsonl"
        analyze_replay_to_jsonl(
            replay_path,
            offline_path,
            analyzer=self.analyzer,
        )
        offline_payload = offline_path.read_bytes()
        online_offline_match = analysis_payload == offline_payload
        if not online_offline_match:
            raise RuntimeError("online and offline analysis JSONL differ")
        offline_path.unlink()

        strict_match: bool | None = None
        if not runner_truncated:
            verification = replay_source_archive(
                self.environment.session.scenario,
                replay_path,
                ruleset=self.environment.session.backend.ruleset,
                strict=True,
            )
            strict_match = verification.matches
            if not strict_match:
                raise RuntimeError("strict replay final state hash mismatch")

        status = "truncated" if runner_truncated else "completed"
        summary: dict[str, Any] = {
            "summary_schema": MATCH_SUMMARY_SCHEMA,
            "status": status,
            "seed": seed,
            "red_agent_id": self.agents["red"].agent_id,
            "blue_agent_id": self.agents["blue"].agent_id,
            "step_count": step_count,
            "frame_count": int(manifest["frame_count"]),
            "terminal": bool(outcome["terminal"]),
            "truncated": bool(outcome["truncated"]) or runner_truncated,
            "winner": outcome["winner"],
            "outcome": outcome["outcome"],
            "replay_sha256": file_sha256(replay_path),
            "analysis_sha256": file_sha256(analysis_path),
            "final_state_hash": outcome["world_hash"],
            "strict_replay_match": strict_match,
            "online_offline_analysis_match": online_offline_match,
            "error": None,
        }
        _write_bytes(
            staging / MATCH_SUMMARY_NAME,
            (canonical_source_json(summary) + "\n").encode("utf-8"),
        )
        _remove_path(recorder.root)
        return summary

    def _reset_agents(self, seed: int) -> None:
        called: list[str] = []
        session = self.environment.session
        for side in session.scenario.side_order:
            agent = self.agents[side]
            called.append(agent.agent_id)
            context = AgentResetContext(
                session.session_id,
                session.scenario.scenario_id,
                side,
                seed,
                session.backend.ruleset.ruleset_version,
                agent.agent_id,
            )
            try:
                agent.reset(deepcopy(context))
            except BaseException as exc:
                raise AgentExecutionError(
                    agent_id=agent.agent_id,
                    perspective=side.upper(),
                    step_index=0,
                    phase="reset",
                    cause=exc,
                    called_agent_ids=called,
                ) from exc

    def _joint_actions(self, *, step_index: int) -> tuple[EngineAction, ...]:
        called: list[str] = []
        selected: list[EngineAction] = []
        session = self.environment.session
        for side in session.scenario.side_order:
            agent = self.agents[side]
            observation = deepcopy(session.observe(side))
            valid_actions = deepcopy(session.valid_actions(side))
            context = AgentStepContext(
                session.session_id,
                side,
                observation.tick,
                observation.stage,
                session.seed,
                agent.agent_id,
            )
            called.append(agent.agent_id)
            try:
                value = agent.act(
                    observation=observation,
                    valid_actions=valid_actions,
                    context=deepcopy(context),
                )
                action = self._validate_agent_action(side, value, valid_actions)
            except BaseException as exc:
                if isinstance(exc, AgentExecutionError):
                    raise
                raise AgentExecutionError(
                    agent_id=agent.agent_id,
                    perspective=side.upper(),
                    step_index=step_index,
                    phase="act",
                    cause=exc,
                    called_agent_ids=called,
                ) from exc
            if action is not None:
                selected.append(action)
        return tuple(selected)

    @staticmethod
    def _validate_agent_action(
        side: str,
        selected: AgentAction | None,
        valid_actions: Sequence[ValidAction],
    ) -> EngineAction | None:
        if selected is None:
            return None
        if not isinstance(selected, AgentAction):
            raise TypeError("Agent.act must return AgentAction or None")
        action = deepcopy(selected.action)
        if action.side != side:
            raise ValueError(f"agent_action_side_mismatch:{side}")
        if selected.valid_action_id != action.action_id:
            raise ValueError("agent valid-action identity mismatch")
        legal = {item.action_id for item in valid_actions}
        if selected.valid_action_id not in legal:
            raise ValueError(f"agent_action_not_valid:{side}")
        return action

    def _recorder_metadata(
        self,
        green_frame: Mapping[str, Any],
        seed: int,
    ) -> Mapping[str, Any]:
        native = green_frame["extra"]["native"]
        scenario = self.environment.session.scenario
        return {
            "scenario_id": scenario.scenario_id,
            "scenario_hash": scenario.scenario_hash,
            "terrain_id": green_frame["terrain_id"],
            "episode_id": self.environment.session.session_id,
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
            "max_ticks": self.environment.session.max_ticks,
            "red_agent_id": self.agents["red"].agent_id,
            "blue_agent_id": self.agents["blue"].agent_id,
        }

    def _close_agents(self) -> None:
        for side in self.environment.session.scenario.side_order:
            try:
                self.agents[side].close()
            except Exception:
                # Closing is best-effort and must not mask match evidence or
                # the original Agent/environment failure.
                pass

    @staticmethod
    def _raise_if_desynchronized(
        live: LiveAnalyzedSession,
        original: BaseException,
        *,
        phase: str,
    ) -> None:
        try:
            live.current_timeline
        except LiveAnalysisDesynchronizedError:
            raise LocalAgentMatchError("analysis_desynchronized", {
                "phase": phase,
                "cause_type": type(original).__name__,
                "cause_message": str(original),
            }) from original


def _write_bytes(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_directory(staging: Path, target: Path, *, overwrite: bool) -> None:
    if not _path_exists(target):
        os.replace(staging, target)
        return
    if not overwrite:
        raise FileExistsError(f"match output already exists: {target}")
    backup = Path(tempfile.mkdtemp(
        prefix=f".{target.name}.", suffix=".backup", dir=target.parent
    ))
    backup.rmdir()
    os.replace(target, backup)
    try:
        os.replace(staging, target)
    except BaseException:
        os.replace(backup, target)
        raise
    _remove_path(backup)


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _remove_path(path: Path) -> None:
    if not _path_exists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
