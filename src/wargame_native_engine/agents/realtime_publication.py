"""Atomic publication for one realtime Native command episode."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping, Sequence

from wargame_native_engine.agents.realtime import (
    CommandStatus,
    RealtimeTickRecord,
    RealtimeTickResult,
    SeatCommandEvent,
)
from wargame_native_engine.compat.source_frame.serialization import (
    canonical_source_json,
)
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveReader,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.serialization import file_sha256
from wargame_native_engine.operations.facade import MatchHandle
from wargame_native_engine.training.publication import (
    _fsync_tree,
    _manifest_bool,
    _manifest_text,
    _publish_staging,
    _verify_analysis_alignment,
    _verify_offline_analysis,
    _write_summary,
)


SEAT_COMMANDS_NAME = "seat_commands.native.jsonl"
REALTIME_TICKS_NAME = "realtime_ticks.native.jsonl"
REALTIME_SUMMARY_NAME = "match_summary.native.json"
REALTIME_SUMMARY_SCHEMA = "native-realtime-match-summary/1.1"


class RealtimeEpisodePublicationError(RuntimeError):
    def __init__(self, phase: str, cause: BaseException) -> None:
        self.phase = str(phase)
        self.cause = cause
        super().__init__(
            f"realtime_episode_publication_failed:{phase}:"
            f"{type(cause).__name__}:{cause}"
        )


@dataclass(frozen=True, slots=True)
class RealtimeEpisodePublication:
    output_dir: Path
    replay_path: Path
    analysis_path: Path
    command_path: Path
    tick_path: Path
    summary_path: Path
    control_audit_path: Path | None
    run_namespace: str
    episode_id: str
    tick_count: int
    fact_frame_count: int
    accepted_command_count: int
    applied_command_count: int
    superseded_command_count: int
    rejected_command_count: int
    cancelled_terminal_command_count: int
    cancelled_truncation_command_count: int
    cancelled_runner_limit_command_count: int
    cancelled_user_stop_command_count: int
    cancelled_failure_command_count: int
    terminal: bool
    truncated: bool
    winner: str | None
    outcome: str | None
    final_state_hash: str


def seat_commands_to_jsonl(values: Sequence[SeatCommandEvent]) -> str:
    records = tuple(values)
    if not all(isinstance(value, SeatCommandEvent) for value in records):
        raise TypeError("values must contain only SeatCommandEvent records")
    return "".join(
        canonical_source_json(value.to_record()) + "\n" for value in records
    )


RealtimeTickEvidence = RealtimeTickRecord | RealtimeTickResult


def realtime_ticks_to_jsonl(values: Sequence[RealtimeTickEvidence]) -> str:
    records = tuple(values)
    if not all(isinstance(value, (RealtimeTickRecord, RealtimeTickResult))
               for value in records):
        raise TypeError("values must contain only realtime tick records")
    return "".join(
        canonical_source_json(value.to_record()) + "\n" for value in records
    )


def publish_realtime_episode(
    match: MatchHandle,
    output_dir: str | Path,
    *,
    run_namespace: str,
    episode_id: str,
    tick_interval_ms: int,
    command_events: Sequence[SeatCommandEvent],
    tick_results: Sequence[RealtimeTickEvidence],
    truncated: bool,
    overwrite: bool = False,
) -> RealtimeEpisodePublication:
    """Build and verify all realtime evidence before exposing the directory."""

    if not isinstance(match, MatchHandle):
        raise TypeError("match must be a MatchHandle")
    namespace = _text(run_namespace, "run_namespace")
    episode = _text(episode_id, "episode_id")
    if type(tick_interval_ms) is not int or tick_interval_ms <= 0:
        raise TypeError("tick_interval_ms must be a positive exact int")
    if type(truncated) is not bool or type(overwrite) is not bool:
        raise TypeError("truncated and overwrite must be exact bool values")
    events = tuple(command_events)
    ticks = tuple(tick_results)
    if not all(isinstance(value, SeatCommandEvent) for value in events):
        raise TypeError("command_events contains an invalid record")
    if not all(isinstance(value, (RealtimeTickRecord, RealtimeTickResult))
               for value in ticks):
        raise TypeError("tick_results contains an invalid record")
    target = Path(output_dir)
    if os.path.lexists(target) and not overwrite:
        raise FileExistsError(f"realtime episode output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(
        prefix=f".{target.name}.realtime.", suffix=".tmp", dir=target.parent,
    ))
    staging_episode = staging_root / "episode"
    phase = "facts"
    try:
        final_view = match.gateway.view(
            match.session_id, match.red_controller_id
        )
        facts = match.publish(
            staging_episode, truncated=truncated, overwrite=False,
        )
        phase = "command_jsonl"
        command_path = staging_episode / SEAT_COMMANDS_NAME
        _write_bytes(command_path, seat_commands_to_jsonl(events).encode("utf-8"))
        phase = "tick_jsonl"
        tick_path = staging_episode / REALTIME_TICKS_NAME
        _write_bytes(tick_path, realtime_ticks_to_jsonl(ticks).encode("utf-8"))
        phase = "replay_verification"
        archive = SourceReplayArchiveReader(facts.replay_path)
        replay = replay_source_archive(
            match.bundle.scenario,
            facts.replay_path,
            strict=True,
            map_runtime_data=match.bundle.map_runtime_data,
        )
        if not replay.matches:
            raise ValueError("strict replay final state hash mismatch")
        phase = "analysis"
        _verify_offline_analysis(facts.replay_path, facts.analysis_path)
        _verify_analysis_alignment(archive, facts.analysis_path)
        phase = "integrity"
        _verify_commands(events, episode_id=episode)
        _verify_ticks(
            ticks,
            events=events,
            tick_interval_ms=tick_interval_ms,
            fact_frame_count=archive.frame_count,
            step_count=int(archive.manifest["step_count"]),
        )
        _verify_exact_jsonl(command_path, seat_commands_to_jsonl(events))
        _verify_exact_jsonl(tick_path, realtime_ticks_to_jsonl(ticks))
        terminal = _manifest_bool(archive.manifest, "termination")
        published_truncated = _manifest_bool(archive.manifest, "truncated")
        if terminal != final_view.terminated:
            raise ValueError("realtime terminal status does not match Replay")
        if published_truncated != truncated:
            raise ValueError("realtime truncated status does not match request")
        winner = archive.manifest.get("winner")
        final_hash = _manifest_text(archive.manifest, "final_state_hash")
        counts = _command_counts(events)
        summary: dict[str, Any] = {
            "summary_schema": REALTIME_SUMMARY_SCHEMA,
            "status": "completed" if terminal else "truncated",
            "run_namespace": namespace,
            "episode_id": episode,
            "scenario_id": archive.manifest.get("scenario_id"),
            "scenario_hash": archive.manifest.get("scenario_hash"),
            "map_id": match.bundle.map_identity.map_id,
            "map_hash": match.bundle.map_identity.sha256,
            "seed": archive.manifest.get("episode_seed"),
            "tick_interval_ms": tick_interval_ms,
            "tick_count": len(ticks),
            "fact_frame_count": archive.frame_count,
            "step_count": int(archive.manifest["step_count"]),
            **counts,
            "terminal": terminal,
            "truncated": published_truncated,
            "winner": winner,
            "outcome": final_view.outcome,
            "final_state_hash": final_hash,
            "replay_sha256": file_sha256(facts.replay_path),
            "analysis_sha256": file_sha256(facts.analysis_path),
            "commands_sha256": file_sha256(command_path),
            "ticks_sha256": file_sha256(tick_path),
            "strict_replay_match": True,
            "online_offline_analysis_match": True,
        }
        phase = "summary"
        summary_path = staging_episode / REALTIME_SUMMARY_NAME
        _write_summary(summary_path, summary)
        _verify_summary(summary_path, summary)
        _fsync_tree(staging_episode)
        phase = "publish"
        _publish_staging(staging_episode, target, overwrite=overwrite)
        return RealtimeEpisodePublication(
            target,
            target / "replay.zip",
            target / "frame_analysis.native.jsonl",
            target / SEAT_COMMANDS_NAME,
            target / REALTIME_TICKS_NAME,
            target / REALTIME_SUMMARY_NAME,
            (
                target / "control_audit.native.jsonl"
                if (target / "control_audit.native.jsonl").is_file() else None
            ),
            namespace,
            episode,
            len(ticks),
            archive.frame_count,
            counts["accepted_command_count"],
            counts["applied_command_count"],
            counts["superseded_command_count"],
            counts["rejected_command_count"],
            counts["cancelled_terminal_command_count"],
            counts["cancelled_truncation_command_count"],
            counts["cancelled_runner_limit_command_count"],
            counts["cancelled_user_stop_command_count"],
            counts["cancelled_failure_command_count"],
            terminal,
            published_truncated,
            winner,
            final_view.outcome,
            final_hash,
        )
    except RealtimeEpisodePublicationError:
        raise
    except BaseException as exc:
        raise RealtimeEpisodePublicationError(phase, exc) from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _verify_commands(
    values: Sequence[SeatCommandEvent], *, episode_id: str,
) -> None:
    for expected, value in enumerate(values):
        if value.command_sequence != expected:
            raise ValueError("command sequence is not contiguous")
        if value.episode_id != episode_id:
            raise ValueError("command episode identity mismatch")
        if value.status is CommandStatus.PENDING:
            raise ValueError("pending command cannot be published")
        if value.status is CommandStatus.APPLIED:
            if value.applied_frame_index is None or value.target_tick is None:
                raise ValueError("applied command lacks frame/tick evidence")
        elif value.applied_frame_index is not None:
            raise ValueError("non-applied command has an applied frame")


def _verify_ticks(
    values: Sequence[RealtimeTickEvidence],
    *,
    events: Sequence[SeatCommandEvent],
    tick_interval_ms: int,
    fact_frame_count: int,
    step_count: int,
) -> None:
    event_by_sequence = {value.command_sequence: value for value in events}
    if fact_frame_count != len(values) + 1:
        raise ValueError("realtime tick/fact frame count mismatch")
    if step_count != len(values):
        raise ValueError("realtime tick/step count mismatch")
    previous_sim = 0
    for expected, value in enumerate(values):
        if value.tick_index != expected:
            raise ValueError("realtime tick index is not contiguous")
        if value.frame_index != expected + 1:
            raise ValueError("realtime tick/frame alignment mismatch")
        if value.sim_time_start_ms != previous_sim:
            raise ValueError("realtime tick sim-time start mismatch")
        if value.sim_time_end_ms - value.sim_time_start_ms != tick_interval_ms:
            raise ValueError("realtime tick interval mismatch")
        previous_sim = value.sim_time_end_ms
        for side, sequences in (
            ("red", value.red_command_sequences),
            ("blue", value.blue_command_sequences),
        ):
            for sequence in sequences:
                event = event_by_sequence.get(sequence)
                if (
                    event is None
                    or event.seat != side
                    or event.target_tick != expected
                    or event.status is not CommandStatus.APPLIED
                    or event.applied_frame_index != value.frame_index
                ):
                    raise ValueError("tick references an invalid applied command")
    applied = {
        value.command_sequence
        for value in events if value.status is CommandStatus.APPLIED
    }
    referenced = {
        sequence
        for value in values
        for sequence in (*value.red_command_sequences, *value.blue_command_sequences)
    }
    if applied != referenced:
        raise ValueError("applied commands and tick records are not bijective")


def _command_counts(values: Sequence[SeatCommandEvent]) -> dict[str, int]:
    return {
        "accepted_command_count": sum(
            value.status is not CommandStatus.REJECTED_AT_SUBMIT for value in values
        ),
        "applied_command_count": sum(
            value.status is CommandStatus.APPLIED for value in values
        ),
        "superseded_command_count": sum(
            value.status is CommandStatus.SUPERSEDED for value in values
        ),
        "rejected_command_count": sum(
            value.status in {
                CommandStatus.REJECTED_AT_SUBMIT,
                CommandStatus.REJECTED_AT_APPLY,
            }
            for value in values
        ),
        "cancelled_terminal_command_count": sum(
            value.status is CommandStatus.CANCELLED_ON_TERMINAL
            for value in values
        ),
        "cancelled_truncation_command_count": sum(
            value.status is CommandStatus.CANCELLED_ON_TRUNCATION
            for value in values
        ),
        "cancelled_runner_limit_command_count": sum(
            value.status is CommandStatus.CANCELLED_ON_RUNNER_LIMIT
            for value in values
        ),
        "cancelled_user_stop_command_count": sum(
            value.status is CommandStatus.CANCELLED_ON_USER_STOP
            for value in values
        ),
        "cancelled_failure_command_count": sum(
            value.status is CommandStatus.CANCELLED_ON_FAILURE
            for value in values
        ),
    }


def _write_bytes(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _verify_exact_jsonl(path: Path, expected: str) -> None:
    payload = path.read_text(encoding="utf-8")
    if payload != expected:
        raise ValueError(f"canonical JSONL mismatch: {path.name}")
    if expected and (not expected.endswith("\n") or expected.endswith("\n\n")):
        raise ValueError(f"invalid JSONL framing: {path.name}")
    for line in payload.splitlines():
        if not line or not isinstance(json.loads(line), Mapping):
            raise ValueError(f"invalid JSONL record: {path.name}")


def _verify_summary(path: Path, expected: Mapping[str, Any]) -> None:
    payload = path.read_text(encoding="utf-8")
    if payload != canonical_source_json(expected) + "\n":
        raise ValueError("realtime summary is not canonical")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field} must not contain surrounding whitespace")
    return value
