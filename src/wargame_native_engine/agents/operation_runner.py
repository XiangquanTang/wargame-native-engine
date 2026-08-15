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
    SeatOperation,
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
from wargame_native_engine.operations import ProductOperationService
from wargame_native_engine.operations.facade import MatchHandle


MATCH_SUMMARY_NAME = "match_summary.native.json"
MATCH_SUMMARY_SCHEMA = "native-local-seat-match-summary/1.0"


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
    summary_path: Path
    terminal: bool
    truncated: bool
    decision_window_count: int
    fact_frame_count: int
    step_count: int
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
        self.controllers = {
            "red": match.bind_controller(
                "red", controller_id=match.red_controller_id
            ),
            "blue": match.bind_controller(
                "blue", controller_id=match.blue_controller_id
            ),
        }
        self.product_services = {
            side: ProductOperationService(controller)
            for side, controller in self.controllers.items()
        }
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
                "terminal": native_terminal,
                "truncated": native_truncated,
                "winner": winner,
                "outcome": final_view.outcome,
                "final_state_hash": final_state_hash,
                "replay_sha256": file_sha256(publication.replay_path),
                "analysis_sha256": file_sha256(publication.analysis_path),
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
                summary_path,
                native_terminal,
                native_truncated,
                processed,
                publication.frame_count,
                publication.step_count,
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
            submitted: list[str] = []
            for side in ("red", "blue"):
                agent = self.agents[side]
                called.append(agent.agent_id)
                context = self._decision_context(side, expected_window)
                try:
                    operation = agent.act(context=deepcopy(context))
                    if not isinstance(operation, SeatOperation):
                        raise TypeError(
                            "LocalSeatAgent.act must return SeatOperation"
                        )
                except BaseException as exc:
                    raise SeatAgentExecutionError(
                        agent_id=agent.agent_id,
                        side=side,
                        decision_index=expected_window,
                        phase="act",
                        cause=exc,
                        called_agent_ids=called,
                        submitted_sides=submitted,
                    ) from exc
                try:
                    preview = self.product_services[side].preview_operation(
                        operation.actor_ids,
                        operation.operation_type,
                        request_id=(
                            f"local-seat:{expected_window}:{side}:preview"
                        ),
                        destination_hex=operation.destination_hex,
                        target_id=operation.target_id,
                        movement_mode=operation.movement_mode,
                        weapon_choices=dict(operation.weapon_choices),
                        whole_side_wait=operation.whole_side_wait,
                    )
                    execution = self.product_services[side].execute_preview(
                        preview.preview_token,
                        request_id=(
                            f"local-seat:{expected_window}:{side}:execute"
                        ),
                        confirm_partial=operation.confirm_partial,
                    )
                except CommandGatewayError as exc:
                    raise SeatOperationError(
                        agent_id=agent.agent_id,
                        side=side,
                        decision_index=expected_window,
                        cause=exc,
                        submitted_sides=submitted,
                    ) from exc
                except BaseException as exc:
                    if self._analysis_desynchronized():
                        raise LocalSeatMatchError(
                            "analysis_desynchronized",
                            {
                                "perspective": side.upper(),
                                "decision_index": expected_window,
                                "cause_type": type(exc).__name__,
                                "cause_message": str(exc),
                                "environment_already_advanced": side == "blue",
                            },
                        ) from exc
                    raise
                submitted.append(side)
                if side == "red" and execution.advanced:
                    raise RuntimeError("RED submission advanced without BLUE")
                if side == "blue" and not execution.advanced:
                    raise RuntimeError("complete Agent window did not advance")
            processed += 1
        return processed

    def _decision_context(
        self,
        side: str,
        expected_window: int,
    ) -> AgentDecisionContext:
        view = self.controllers[side].observe()
        if view.step_index != expected_window:
            raise RuntimeError("controller observation/window mismatch")
        actor_ids = _friendly_actor_ids(view.observation)
        capabilities = self.product_services[side].capabilities(actor_ids)
        return AgentDecisionContext(
            side,
            expected_window,
            view.frame_index,
            view.sim_time_ms,
            json_clone(view.observation),
            json_clone(capabilities),
        )

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


def _friendly_actor_ids(observation: Mapping[str, Any]) -> tuple[int, ...]:
    values = observation.get("friendly_operators", ())
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise RuntimeError("side observation friendly_operators is invalid")
    result: list[int] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise RuntimeError("side observation friendly operator is invalid")
        unit_id = value.get("operator_id")
        if type(unit_id) is not int or unit_id < 0:
            raise RuntimeError("side observation operator_id is invalid")
        result.append(unit_id)
    return tuple(sorted(result))


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
