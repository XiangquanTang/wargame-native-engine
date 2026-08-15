"""Local RED/BLUE seat runner over the product Operations Facade."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from wargame_native_engine.agents.seat import (
    AgentDecisionContext,
    LocalSeatAgent,
    SeatDecision,
)
from wargame_native_engine.analysis import EightFactorAnalyzer, analyze_replay_to_jsonl
from wargame_native_engine.compat.source_frame.serialization import (
    canonical_source_json,
)
from wargame_native_engine.gateway import CommandGatewayError
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.serialization import file_sha256, json_clone
from wargame_native_engine.operations.facade import MatchHandle
from wargame_native_engine.training.models import DecisionTransition
from wargame_native_engine.training.multi_seat import (
    NativeDecisionSession,
    NativeMultiSeatError,
    mark_transition_pair_truncated,
)
from wargame_native_engine.training.transitions import (
    DECISION_TRANSITIONS_NAME,
    write_decision_transitions_jsonl,
)


MATCH_SUMMARY_NAME = "match_summary.native.json"
MATCH_SUMMARY_SCHEMA = "native-local-seat-match-summary/1.1"


class LocalSeatMatchError(RuntimeError):
    """A seat match stopped before synchronized evidence was published."""

    def __init__(self, status: str, detail: Mapping[str, Any]) -> None:
        self.status = str(status)
        self.detail = json_clone(detail)
        super().__init__(f"local_seat_match_failed:{self.status}")


class SeatAgentExecutionError(LocalSeatMatchError):
    """One local seat Agent failed during reset or decision generation."""

    def __init__(
        self,
        *,
        agent_id: str,
        side: str,
        decision_index: int,
        phase: str,
        cause: BaseException,
        called_agent_ids: Sequence[str],
        submitted_sides: Sequence[str],
    ) -> None:
        self.agent_id = str(agent_id)
        self.side = str(side).lower()
        self.decision_index = int(decision_index)
        self.phase = str(phase)
        self.cause_type = type(cause).__name__
        self.cause_message = str(cause)
        self.called_agent_ids = tuple(called_agent_ids)
        self.submitted_sides = tuple(submitted_sides)
        super().__init__("agent_error", {
            "agent_id": self.agent_id,
            "perspective": self.side.upper(),
            "decision_index": self.decision_index,
            "phase": self.phase,
            "cause_type": self.cause_type,
            "cause_message": self.cause_message,
            "called_agent_ids": list(self.called_agent_ids),
            "submitted_sides": [item.upper() for item in self.submitted_sides],
        })
        self.args = (
            "seat_agent_execution_error:"
            f"{self.agent_id}:{self.side.upper()}:{self.decision_index}:"
            f"{self.phase}:{self.cause_type}:{self.cause_message}",
        )


class SeatOperationError(LocalSeatMatchError):
    """The shared product operation layer rejected an Agent decision."""

    def __init__(
        self,
        *,
        agent_id: str,
        side: str,
        decision_index: int,
        cause: CommandGatewayError,
        submitted_sides: Sequence[str],
    ) -> None:
        self.agent_id = str(agent_id)
        self.side = str(side).lower()
        self.decision_index = int(decision_index)
        self.operation_error_code = cause.code
        self.submitted_sides = tuple(submitted_sides)
        super().__init__("invalid_agent_operation", {
            "agent_id": self.agent_id,
            "perspective": self.side.upper(),
            "decision_index": self.decision_index,
            "operation_error_code": cause.code,
            "operation_error_message": cause.message,
            "submitted_sides": [item.upper() for item in self.submitted_sides],
        })


@dataclass(frozen=True, slots=True)
class LocalSeatMatchResult:
    output_dir: Path
    replay_path: Path
    analysis_path: Path
    transition_path: Path
    summary_path: Path
    terminal: bool
    truncated: bool
    decision_window_count: int
    fact_frame_count: int
    step_count: int
    transition_count: int
    winner: str | None
    outcome: str | None
    final_state_hash: str
    strict_replay_match: bool
    online_offline_analysis_match: bool

    @property
    def frame_count(self) -> int:
        """Compatibility alias; facts are not decision windows."""

        return self.fact_frame_count


class LocalSeatMatchRunner:
    """Drive local seat Agents through product preview/execute and Gateway."""

    def __init__(
        self,
        match: MatchHandle,
        *,
        red_agent: LocalSeatAgent,
        blue_agent: LocalSeatAgent,
    ) -> None:
        if not isinstance(match, MatchHandle):
            raise TypeError("match must be a MatchHandle")
        self.match = match
        self.agents = {"red": red_agent, "blue": blue_agent}
        for side, agent in self.agents.items():
            agent_id = getattr(agent, "agent_id", None)
            if not isinstance(agent_id, str) or not agent_id.strip():
                raise ValueError(f"{side} agent_id must be a non-empty string")
        seed = match.gateway.manager.agent_reset_context(
            match.session_id, "red", red_agent.agent_id
        ).seed
        self.decision_session = NativeDecisionSession(
            match,
            episode_id=f"{match.session_id}-local-seat",
            seed=seed,
        )
        self.controllers = self.decision_session.controllers
        self.product_services = self.decision_session.product_services
        self._transitions: list[DecisionTransition] = []
        self._ran = False

    def run(
        self,
        output_dir: str | Path,
        *,
        max_decision_windows: int | None = None,
        overwrite: bool = False,
    ) -> LocalSeatMatchResult:
        if self._ran:
            raise RuntimeError("local_seat_match_runner_is_single_use")
        self._ran = True
        if max_decision_windows is not None and (
            type(max_decision_windows) is not int
            or max_decision_windows <= 0
        ):
            raise ValueError("max_decision_windows must be positive or None")
        if type(overwrite) is not bool:
            raise TypeError("overwrite must be bool")
        target = Path(output_dir)
        if os.path.lexists(target) and not overwrite:
            raise FileExistsError(f"match output already exists: {target}")

        try:
            self._reset_agents()
            processed = self._run_windows(max_decision_windows)
            final_view = self.controllers["red"].observe()
            truncated = not final_view.terminal
            transitions = tuple(self._transitions)
            if truncated and transitions:
                transitions = (
                    *transitions[:-2],
                    *mark_transition_pair_truncated(transitions[-2:]),
                )
            publication = self.match.publish(
                target,
                truncated=truncated,
                overwrite=overwrite,
            )
            archive = SourceReplayArchiveImporter().import_path(
                publication.replay_path
            )
            verification = replay_source_archive(
                self.match.bundle.scenario,
                publication.replay_path,
                strict=True,
                map_runtime_data=self.match.bundle.map_runtime_data,
            )
            if not verification.matches:
                raise RuntimeError("strict replay final state hash mismatch")
            with tempfile.TemporaryDirectory(
                prefix="wargame-local-seat-analysis."
            ) as temporary:
                offline = Path(temporary) / "frame_analysis.native.jsonl"
                analyze_replay_to_jsonl(
                    publication.replay_path,
                    offline,
                    analyzer=EightFactorAnalyzer(),
                )
                online_offline = (
                    publication.analysis_path.read_bytes()
                    == offline.read_bytes()
                )
            if not online_offline:
                raise RuntimeError("online and offline analysis JSONL differ")
            transition_path = write_decision_transitions_jsonl(
                publication.output_dir / DECISION_TRANSITIONS_NAME,
                transitions,
            )
            final_state_hash = _nonempty_manifest_text(
                archive.manifest, "final_state_hash"
            )
            native_terminal = bool(archive.manifest.get("termination", False))
            native_truncated = bool(archive.manifest.get("truncated", False))
            winner = archive.manifest.get("winner")
            if winner is not None and not isinstance(winner, str):
                raise RuntimeError("published replay winner is invalid")
            summary = {
                "summary_schema": MATCH_SUMMARY_SCHEMA,
                "status": "completed" if native_terminal else "truncated",
                "seed": archive.manifest.get("episode_seed"),
                "scenario_id": archive.manifest.get("scenario_id"),
                "scenario_hash": archive.manifest.get("scenario_hash"),
                "red_agent_id": self.agents["red"].agent_id,
                "blue_agent_id": self.agents["blue"].agent_id,
                "decision_window_count": processed,
                "fact_frame_count": publication.frame_count,
                "step_count": publication.step_count,
                "transition_count": len(transitions),
                "terminal": native_terminal,
                "truncated": native_truncated,
                "winner": winner,
                "outcome": final_view.outcome,
                "final_state_hash": final_state_hash,
                "replay_sha256": file_sha256(publication.replay_path),
                "analysis_sha256": file_sha256(publication.analysis_path),
                "transitions_sha256": file_sha256(transition_path),
                "strict_replay_match": True,
                "online_offline_analysis_match": True,
                "error": None,
            }
            summary_path = publication.output_dir / MATCH_SUMMARY_NAME
            _atomic_write(
                summary_path,
                (canonical_source_json(summary) + "\n").encode("utf-8"),
            )
            return LocalSeatMatchResult(
                publication.output_dir,
                publication.replay_path,
                publication.analysis_path,
                transition_path,
                summary_path,
                native_terminal,
                native_truncated,
                processed,
                publication.frame_count,
                publication.step_count,
                len(transitions),
                winner,
                final_view.outcome,
                final_state_hash,
                True,
                True,
            )
        finally:
            self._close_agents()

    def _reset_agents(self) -> None:
        called: list[str] = []
        manager = self.match.gateway.manager
        for side in ("red", "blue"):
            agent = self.agents[side]
            called.append(agent.agent_id)
            context = manager.agent_reset_context(
                self.match.session_id, side, agent.agent_id
            )
            try:
                agent.reset(deepcopy(context))
            except BaseException as exc:
                raise SeatAgentExecutionError(
                    agent_id=agent.agent_id,
                    side=side,
                    decision_index=0,
                    phase="reset",
                    cause=exc,
                    called_agent_ids=called,
                    submitted_sides=(),
                ) from exc

    def _run_windows(self, maximum: int | None) -> int:
        processed = 0
        while not self.match.status.terminal:
            if maximum is not None and processed >= maximum:
                break
            expected_window = self.controllers[
                "red"
            ].window().decision_window_index
            called: list[str] = []
            decisions: dict[str, SeatDecision] = {}
            for side in ("red", "blue"):
                agent = self.agents[side]
                called.append(agent.agent_id)
                context = self._decision_context(side, expected_window)
                try:
                    decision = agent.act(context=deepcopy(context))
                    if not isinstance(decision, SeatDecision):
                        raise TypeError(
                            "LocalSeatAgent.act must return SeatDecision"
                        )
                    decisions[side] = decision
                except BaseException as exc:
                    raise SeatAgentExecutionError(
                        agent_id=agent.agent_id,
                        side=side,
                        decision_index=expected_window,
                        phase="act",
                        cause=exc,
                        called_agent_ids=called,
                        submitted_sides=(),
                    ) from exc

            try:
                step_result = self.decision_session.step(
                    decisions["red"], decisions["blue"]
                )
            except NativeMultiSeatError as exc:
                cause = exc.__cause__
                if (
                    exc.code == "decision_preview_failed"
                    and isinstance(cause, CommandGatewayError)
                ):
                    side = exc.side or "red"
                    raise SeatOperationError(
                        agent_id=self.agents[side].agent_id,
                        side=side,
                        decision_index=expected_window,
                        cause=cause,
                        submitted_sides=(),
                    ) from exc
                if self._analysis_desynchronized():
                    raise LocalSeatMatchError(
                        "analysis_desynchronized",
                        {
                            "perspective": (exc.side or "blue").upper(),
                            "decision_index": expected_window,
                            "cause_type": type(cause or exc).__name__,
                            "cause_message": str(cause or exc),
                            "environment_already_advanced": True,
                        },
                    ) from exc
                raise
            self._transitions.extend(deepcopy(step_result.transitions))
            processed += 1
        return processed

    def _decision_context(
        self,
        side: str,
        expected_window: int,
    ) -> AgentDecisionContext:
        context = self.decision_session.decision_context(side)
        if context.decision_index != expected_window:
            raise RuntimeError("controller observation/window mismatch")
        return context

    def _analysis_desynchronized(self) -> bool:
        try:
            self.match.gateway.manager.descriptor(self.match.session_id)
        except CommandGatewayError as exc:
            return (
                exc.code == "session_failed"
                and "analysis_desynchronized" in exc.message
            )
        return False

    def _close_agents(self) -> None:
        for side in ("red", "blue"):
            try:
                self.agents[side].close()
            except Exception:
                pass


OperationAgentMatchRunner = LocalSeatMatchRunner
OperationAgentMatchResult = LocalSeatMatchResult


def run_local_agent_match(
    match: MatchHandle,
    *,
    red_agent: LocalSeatAgent,
    blue_agent: LocalSeatAgent,
    output_dir: str | Path,
    max_decision_windows: int | None = None,
    overwrite: bool = False,
) -> LocalSeatMatchResult:
    """Run one product-operation match without HTTP or browser dependencies."""

    return LocalSeatMatchRunner(
        match,
        red_agent=red_agent,
        blue_agent=blue_agent,
    ).run(
        output_dir,
        max_decision_windows=max_decision_windows,
        overwrite=overwrite,
    )


def _nonempty_manifest_text(manifest: Mapping[str, Any], field: str) -> str:
    value = manifest.get(field)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"published replay lacks {field}")
    return value


def _atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
