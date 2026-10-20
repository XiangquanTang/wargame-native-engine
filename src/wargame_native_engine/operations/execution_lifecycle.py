"""Bounded, side-safe operation execution correlation.

This module is a read-only consumer of committed Engine facts.  It neither
owns WorldState nor advances simulation time.  Public execution handles are
adapter identities and are deliberately distinct from catalog action IDs,
boarding transition IDs, and indirect-fire mission IDs.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from wargame_native_engine.agents.seat import SeatDecision
from wargame_native_engine.io.serialization import canonical_hash, json_clone


EXECUTION_RECORD_SCHEMA_VERSION = "native-operation-execution/1.0"
EXECUTION_PAGE_SCHEMA_VERSION = "native-operation-execution-page/2.0"
EXECUTION_AVAILABILITY_SCHEMA_VERSION = (
    "native-operation-execution-availability/1.0"
)
TRACKED_CONTINUOUS_OPERATIONS = frozenset({
    "MOVE", "BOARD", "UNLOAD", "INDIRECT_FIRE",
})


class ExecutionStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


TERMINAL_EXECUTION_STATUSES = frozenset({
    ExecutionStatus.COMPLETED,
    ExecutionStatus.INTERRUPTED,
    ExecutionStatus.FAILED,
})


class ExecutionQueryError(RuntimeError):
    """Stable query failure which does not reveal handle existence."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}:{self.message}")


@dataclass(frozen=True, slots=True)
class OperationExecutionRecord:
    episode_id: str
    side: str
    execution_handle: str
    batch_handle: str
    actor_id: int | None
    operation_type: str
    command_status: str
    execution_status: ExecutionStatus
    decision_window_index: int
    command_sequence: int | None
    request_id: str | None
    action_id: str | None
    submitted_frame_index: int
    submitted_sim_time_ms: int
    observed_frame_index: int
    observed_sim_time_ms: int
    started_frame_index: int | None = None
    started_sim_time_ms: int | None = None
    completed_frame_index: int | None = None
    completed_sim_time_ms: int | None = None
    reason_code: str | None = None
    evidence_category: str = "submission"
    progress: Mapping[str, Any] = field(default_factory=dict)
    revision: int = 0
    schema_version: str = EXECUTION_RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", str(self.side).lower())
        object.__setattr__(self, "operation_type", str(self.operation_type).upper())
        if not isinstance(self.execution_status, ExecutionStatus):
            object.__setattr__(
                self, "execution_status", ExecutionStatus(self.execution_status)
            )
        object.__setattr__(self, "progress", json_clone(self.progress))

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "side": self.side,
            "execution_handle": self.execution_handle,
            "batch_handle": self.batch_handle,
            "actor_id": self.actor_id,
            "operation_type": self.operation_type,
            "submission": {
                "decision_window_index": self.decision_window_index,
                "command_sequence": self.command_sequence,
                "request_id": self.request_id,
                "action_id": self.action_id,
                "frame_index": self.submitted_frame_index,
                "sim_time_ms": self.submitted_sim_time_ms,
            },
            "command_status": self.command_status,
            "execution_status": self.execution_status.value,
            "observed_frame_index": self.observed_frame_index,
            "observed_sim_time_ms": self.observed_sim_time_ms,
            "started_frame_index": self.started_frame_index,
            "started_sim_time_ms": self.started_sim_time_ms,
            "completed_frame_index": self.completed_frame_index,
            "completed_sim_time_ms": self.completed_sim_time_ms,
            "reason_code": self.reason_code,
            "evidence_category": self.evidence_category,
            "progress": json_clone(self.progress),
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class OperationExecutionPage:
    records: tuple[OperationExecutionRecord, ...]
    cursor: str
    next_cursor: str
    has_more: bool
    schema_version: str = EXECUTION_PAGE_SCHEMA_VERSION

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "cursor": self.cursor,
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "records": [value.to_record() for value in self.records],
        }


@dataclass(frozen=True, slots=True)
class OperationExecutionAvailability:
    side: str
    available: bool
    reason_code: str | None
    active_records: int
    max_active_records: int
    terminal_retention: int
    change_retention: int
    schema_version: str = EXECUTION_AVAILABILITY_SCHEMA_VERSION

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "side": self.side,
            "available": self.available,
            "reason_code": self.reason_code,
            "active_records": self.active_records,
            "max_active_records": self.max_active_records,
            "terminal_retention": self.terminal_retention,
            "change_retention": self.change_retention,
        }


@dataclass(slots=True)
class _Entry:
    record: OperationExecutionRecord
    operation_index: int
    target_id: int | None
    destination_hex: int | None
    target_hex: int | None
    weapon_id: int | None
    internal_identity: str | None = None
    started_frame_index: int | None = None
    started_sim_time_ms: int | None = None


class OperationExecutionTracker:
    """Per-episode bounded correlation store.

    Active records are never evicted.  Only terminal records and the change
    journal are bounded.  A capacity problem disables new tracking explicitly;
    it never changes Engine execution.
    """

    def __init__(
        self,
        episode_id: str,
        *,
        terminal_retention: int = 128,
        change_retention: int = 512,
        max_active_records: int = 256,
    ) -> None:
        if not isinstance(episode_id, str) or not episode_id:
            raise ValueError("episode_id must be non-empty")
        for value, name in (
            (terminal_retention, "terminal_retention"),
            (change_retention, "change_retention"),
            (max_active_records, "max_active_records"),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive exact int")
        self.episode_id = episode_id
        self.terminal_retention = terminal_retention
        self.change_retention = change_retention
        self.max_active_records = max_active_records
        self._entries: dict[str, _Entry] = {}
        self._terminal: dict[str, deque[str]] = {
            "red": deque(), "blue": deque(),
        }
        self._changes: dict[
            str, deque[tuple[int, OperationExecutionRecord]]
        ] = {"red": deque(), "blue": deque()}
        self._revision = {"red": 0, "blue": 0}
        self._submission_sequence = {"red": 0, "blue": 0}
        self._latest_handles: dict[str, tuple[str, ...]] = {
            "red": (), "blue": (),
        }
        self._available = {"red": True, "blue": True}
        self._unavailable_reason: dict[str, str | None] = {
            "red": None, "blue": None,
        }
        self._issued_cursors: dict[str, dict[str, int]] = {
            "red": {}, "blue": {},
        }
        self._issued_cursor_order: dict[str, deque[str]] = {
            "red": deque(), "blue": deque(),
        }
        for side in ("red", "blue"):
            self._remember_cursor(side, 0)

    @property
    def available(self) -> bool:
        """Backward-compatible aggregate; public callers should bind a side."""

        return all(self._available.values())

    @property
    def revision(self) -> int:
        """Backward-compatible aggregate diagnostic, not a public cursor."""

        return sum(self._revision.values())

    def revision_for(self, side: str) -> int:
        return self._revision[_side(side)]

    def tracking_status(self, side: str) -> OperationExecutionAvailability:
        normalized = _side(side)
        return OperationExecutionAvailability(
            side=normalized,
            available=self._available[normalized],
            reason_code=self._unavailable_reason[normalized],
            active_records=self._active_count(normalized),
            max_active_records=self.max_active_records,
            terminal_retention=self.terminal_retention,
            change_retention=self.change_retention,
        )

    def submit(
        self,
        side: str,
        decision: SeatDecision,
        *,
        decision_window_index: int,
        frame_index: int,
        sim_time_ms: int,
        command_status: str,
        command_sequence: int | None = None,
        request_id: str | None = None,
    ) -> tuple[str, ...]:
        normalized = _side(side)
        if not isinstance(decision, SeatDecision):
            raise TypeError("decision must be SeatDecision")
        children = sum(
            max(1, len(operation.actor_ids))
            for operation in decision.operations
        )
        active_count = self._active_count(normalized)
        if (
            not self._available[normalized]
            or active_count + children > self.max_active_records
        ):
            self._available[normalized] = False
            self._unavailable_reason[normalized] = "ACTIVE_CAPACITY_EXHAUSTED"
            self._latest_handles[normalized] = ()
            return ()
        sequence = self._submission_sequence[normalized]
        self._submission_sequence[normalized] += 1
        batch_handle = "execution-batch-" + canonical_hash({
            "episode_id": self.episode_id,
            "side": normalized,
            "submission_sequence": sequence,
            "decision_window_index": decision_window_index,
        })[:24]
        handles: list[str] = []
        for operation_index, operation in enumerate(decision.operations):
            actors: tuple[int | None, ...] = (
                tuple(operation.actor_ids) if operation.actor_ids else (None,)
            )
            for actor_index, actor_id in enumerate(actors):
                handle = "execution-" + canonical_hash({
                    "batch_handle": batch_handle,
                    "operation_index": operation_index,
                    "actor_index": actor_index,
                    "actor_id": actor_id,
                    "operation_type": operation.operation_type,
                })[:24]
                record = OperationExecutionRecord(
                    episode_id=self.episode_id,
                    side=normalized,
                    execution_handle=handle,
                    batch_handle=batch_handle,
                    actor_id=actor_id,
                    operation_type=operation.operation_type,
                    command_status=str(command_status),
                    execution_status=ExecutionStatus.NOT_STARTED,
                    decision_window_index=decision_window_index,
                    command_sequence=command_sequence,
                    request_id=request_id,
                    action_id=None,
                    submitted_frame_index=frame_index,
                    submitted_sim_time_ms=sim_time_ms,
                    observed_frame_index=frame_index,
                    observed_sim_time_ms=sim_time_ms,
                    progress=_operation_progress(operation),
                )
                self._entries[handle] = _Entry(
                    record=record,
                    operation_index=operation_index,
                    target_id=operation.target_id,
                    destination_hex=operation.destination_hex,
                    target_hex=operation.target_hex,
                    weapon_id=operation.weapon_id,
                )
                handles.append(handle)
                self._publish(handle, record)
        self._latest_handles[normalized] = tuple(handles)
        return tuple(handles)

    def bind_plan(
        self,
        handles: Sequence[str],
        correlations: Sequence[Mapping[str, object]],
    ) -> None:
        by_key = {
            (int(value["operation_index"]), value.get("actor_id")): value
            for value in correlations
        }
        for handle in handles:
            entry = self._entries.get(handle)
            if entry is None:
                continue
            value = by_key.get((entry.operation_index, entry.record.actor_id))
            if value is None:
                continue
            if value.get("accepted_for_preview") is not True:
                self.set_command_status(
                    (handle,),
                    "REJECTED_AT_PREFLIGHT",
                    reason_code=str(
                        value.get("reject_code") or "PREVIEW_REJECTED"
                    ),
                )
                continue
            action_id = value.get("action_id")
            if isinstance(action_id, str) and action_id:
                self._update(
                    handle,
                    action_id=action_id,
                    evidence_category="preflight_bound",
                )

    def set_command_status(
        self,
        handles: Sequence[str],
        status: str,
        *,
        reason_code: str | None = None,
        frame_index: int | None = None,
        sim_time_ms: int | None = None,
    ) -> None:
        for handle in handles:
            entry = self._entries.get(handle)
            if entry is None:
                continue
            if (
                str(status) == "APPLIED"
                and entry.record.command_status.startswith("REJECTED")
            ):
                continue
            changes: dict[str, object] = {
                "command_status": str(status),
            }
            if frame_index is not None:
                changes["observed_frame_index"] = frame_index
            if sim_time_ms is not None:
                changes["observed_sim_time_ms"] = sim_time_ms
            if reason_code is not None:
                changes["reason_code"] = reason_code
            if str(status).startswith("REJECTED") or str(status) in {
                "SUPERSEDED",
                "CANCELLED_ON_TERMINAL",
                "CANCELLED_ON_TRUNCATION",
                "CANCELLED_ON_RUNNER_LIMIT",
                "CANCELLED_ON_USER_STOP",
                "CANCELLED_ON_FAILURE",
                "ABORTED_ATOMIC_PREFLIGHT",
            }:
                changes["execution_status"] = ExecutionStatus.NOT_STARTED
                changes["evidence_category"] = "command_terminal_before_execution"
            elif str(status) == "APPLICATION_EVIDENCE_UNKNOWN":
                changes["execution_status"] = ExecutionStatus.UNKNOWN
                changes["evidence_category"] = "action_result_missing"
            self._update(handle, **changes)

    def consume(self, side: str, facts: Mapping[str, Any]) -> None:
        normalized = _side(side)
        if str(facts.get("side", "")).lower() != normalized:
            raise ValueError("execution facts side mismatch")
        frame = int(facts["frame_index"])
        sim_time = int(facts["sim_time_ms"])
        operators = {
            int(value["actor_id"]): value
            for value in facts.get("operators", ())
            if isinstance(value, Mapping)
        }
        transitions = {
            str(value["transition_id"]): value
            for value in facts.get("boarding_transitions", ())
            if isinstance(value, Mapping)
        }
        missions = {
            str(value["mission_id"]): value
            for value in facts.get("indirect_fire_missions", ())
            if isinstance(value, Mapping)
        }
        events = tuple(
            value for value in facts.get("events", ())
            if isinstance(value, Mapping)
        )
        created_by_action = {
            str(value.get("action_id")): value
            for value in events
            if value.get("event_type") == "indirect_fire_mission_created"
            and value.get("action_id") is not None
            and value.get("mission_id") is not None
        }
        transition_by_action = {
            str(value.get("action_id")): value
            for value in events
            if value.get("event_type") in {"board_started", "unload_started"}
            and value.get("action_id") is not None
            and value.get("transition_id") is not None
        }
        stopped_actors = {
            int(value["actor_id"])
            for value in events
            if value.get("event_type") == "stop_move_requested"
            and type(value.get("actor_id")) is int
        }
        movement_started_by_action = {
            str(value.get("action_id")): value
            for value in events
            if value.get("event_type") == "movement_started"
            and value.get("action_id") is not None
        }
        transition_terminal = {
            str(value.get("transition_id")): value
            for value in events
            if value.get("event_type") in {"board_completed", "unload_completed"}
            and value.get("transition_id") is not None
        }
        mission_terminal = {
            str(value.get("mission_id")): value
            for value in events
            if value.get("event_type") in {
                "indirect_fire_mission_cancelled", "indirect_fire_resolved",
            }
            and value.get("mission_id") is not None
        }
        for handle, entry in tuple(self._entries.items()):
            record = entry.record
            if (
                record.side != normalized
                or _is_terminal_record(record)
                or record.command_status != "APPLIED"
            ):
                continue
            common = {
                "observed_frame_index": frame,
                "observed_sim_time_ms": sim_time,
            }
            if record.operation_type == "MOVE":
                started = movement_started_by_action.get(record.action_id or "")
                if started is not None:
                    self._remember_start(entry, frame, started)
                self._consume_move(handle, entry, operators, stopped_actors, common)
            elif record.operation_type in {"BOARD", "UNLOAD"}:
                if entry.internal_identity is None and record.action_id is not None:
                    started = transition_by_action.get(record.action_id)
                    if started is not None:
                        entry.internal_identity = str(started["transition_id"])
                        self._remember_start(entry, frame, started)
                self._consume_boarding(
                    handle, entry, transitions, operators, common,
                    transition_terminal,
                )
            elif record.operation_type == "INDIRECT_FIRE":
                if entry.internal_identity is None and record.action_id is not None:
                    started = created_by_action.get(record.action_id)
                    if started is not None:
                        entry.internal_identity = str(started["mission_id"])
                        self._remember_start(entry, frame, started)
                self._consume_mission(
                    handle, entry, missions, common, mission_terminal,
                )
            else:
                self._update(
                    handle,
                    **common,
                    execution_status=ExecutionStatus.UNKNOWN,
                    reason_code="EXECUTION_QUERY_NOT_SUPPORTED",
                    evidence_category="unsupported_operation",
                )

    def _consume_move(self, handle, entry, operators, stopped_actors, common) -> None:
        record = entry.record
        operator = operators.get(record.actor_id)
        if operator is None:
            self._update(
                handle, **common, execution_status=ExecutionStatus.UNKNOWN,
                reason_code="OWN_ACTOR_FACT_UNAVAILABLE",
                evidence_category="operator_fact",
            )
            return
        if operator.get("alive") is False:
            self._finish(
                handle, ExecutionStatus.INTERRUPTED, "ACTOR_DESTROYED",
                "operator_fact", common,
            )
            return
        if record.actor_id in stopped_actors:
            self._finish(
                handle, ExecutionStatus.INTERRUPTED, "STOP_MOVE_REQUESTED",
                "movement_event", common,
            )
            return
        authoritative_action = operator.get("movement_action_id")
        if authoritative_action not in {None, record.action_id}:
            self._finish(
                handle, ExecutionStatus.INTERRUPTED, "REPLACED_BY_NEW_MOVE",
                "movement_identity", common,
            )
            return
        progress = {
            "destination_hex": entry.destination_hex,
            "current_hex": operator.get("current_hex"),
            "remaining_path_length": len(operator.get("move_path", ())),
            "state": operator.get("state"),
            "segment_progress_millionths": operator.get(
                "segment_progress_millionths", 0
            ),
        }
        complete = (
            authoritative_action == record.action_id
            and entry.destination_hex is not None
            and operator.get("current_hex") == entry.destination_hex
            and not operator.get("move_path")
            and operator.get("stopped") is True
            and int(operator.get("stop_transition_remaining_ms", 0)) == 0
        )
        if complete:
            self._finish(
                handle, ExecutionStatus.COMPLETED, "DESTINATION_REACHED_AND_STOPPED",
                "movement_identity_and_state", common, progress=progress,
            )
            return
        started = authoritative_action == record.action_id
        self._update(
            handle,
            **common,
            execution_status=(
                ExecutionStatus.IN_PROGRESS if started else ExecutionStatus.UNKNOWN
            ),
            started_frame_index=(
                record.started_frame_index if record.started_frame_index is not None
                else entry.started_frame_index
                if entry.started_frame_index is not None
                else common["observed_frame_index"] if started else None
            ),
            started_sim_time_ms=(
                record.started_sim_time_ms if record.started_sim_time_ms is not None
                else entry.started_sim_time_ms
            ),
            reason_code=("MOVEMENT_ACTIVE" if started else "START_EVIDENCE_UNKNOWN"),
            evidence_category="movement_identity_and_state",
            progress=progress,
        )

    def _consume_boarding(
        self, handle, entry, transitions, operators, common, terminal_events,
    ) -> None:
        transition = transitions.get(entry.internal_identity or "")
        if transition is None:
            self._update(
                handle, **common, execution_status=ExecutionStatus.UNKNOWN,
                reason_code="TRANSITION_IDENTITY_NOT_OBSERVED",
                evidence_category="boarding_transition",
            )
            return
        record_type = entry.record.operation_type
        progress = {
            "transition_type": record_type,
            "remaining_ms": transition.get("remaining_ms"),
        }
        if transition.get("status") == "ACTIVE":
            self._start(handle, common, "TRANSITION_ACTIVE", "boarding_transition", progress)
            return
        passenger = operators.get(int(transition["passenger_id"]))
        carrier = operators.get(int(transition["carrier_id"]))
        final_ok = False
        if passenger is not None and carrier is not None:
            if record_type == "BOARD":
                final_ok = passenger.get("mounted_on") == transition["carrier_id"]
            else:
                final_ok = (
                    passenger.get("mounted_on") is None
                    and passenger.get("deployed") is True
                    and passenger.get("current_hex") == carrier.get("current_hex")
                )
        if transition.get("status") == "COMPLETED" and final_ok:
            terminal_event = terminal_events.get(entry.internal_identity or "")
            self._finish(
                handle, ExecutionStatus.COMPLETED, "COMPLETED",
                "boarding_terminal_fact_and_state", common, progress=progress,
                completed_sim_time_ms=_event_time(terminal_event),
            )
        else:
            self._update(
                handle, **common, execution_status=ExecutionStatus.UNKNOWN,
                reason_code="BOARDING_FINAL_STATE_UNPROVEN",
                evidence_category="boarding_terminal_fact_and_state",
                progress=progress,
            )

    def _consume_mission(
        self, handle, entry, missions, common, terminal_events,
    ) -> None:
        mission = missions.get(entry.internal_identity or "")
        if mission is None:
            self._update(
                handle, **common, execution_status=ExecutionStatus.UNKNOWN,
                reason_code="MISSION_IDENTITY_NOT_OBSERVED",
                evidence_category="mission_identity",
            )
            return
        phase = str(mission.get("phase"))
        progress = {
            "phase": phase,
            "remaining_flight_seconds": mission.get("remaining_flight_seconds"),
            "active_remaining_seconds": mission.get("active_remaining_seconds"),
            "planned_hex": entry.target_hex,
        }
        if phase in {"PLANNED", "FLYING", "ACTIVE"}:
            self._start(
                handle, common, f"MISSION_{phase}", "mission_phase", progress
            )
        elif phase == "RESOLVED":
            terminal_event = terminal_events.get(entry.internal_identity or "")
            self._finish(
                handle, ExecutionStatus.COMPLETED,
                str(mission.get("terminal_outcome") or "RESOLVED"),
                "mission_terminal_phase", common, progress=progress,
                completed_sim_time_ms=_event_time(terminal_event),
            )
        elif phase == "CANCELLED":
            terminal_event = terminal_events.get(entry.internal_identity or "")
            self._finish(
                handle, ExecutionStatus.INTERRUPTED,
                str(mission.get("cancellation_reason") or "CANCELLED"),
                "mission_terminal_phase", common, progress=progress,
                completed_sim_time_ms=_event_time(terminal_event),
            )
        else:
            self._update(
                handle, **common, execution_status=ExecutionStatus.UNKNOWN,
                reason_code="UNKNOWN_MISSION_PHASE",
                evidence_category="mission_phase", progress=progress,
            )

    def _start(self, handle, common, reason, category, progress) -> None:
        entry = self._entries[handle]
        record = entry.record
        self._update(
            handle,
            **common,
            execution_status=ExecutionStatus.IN_PROGRESS,
            started_frame_index=(
                record.started_frame_index
                if record.started_frame_index is not None
                else entry.started_frame_index
                if entry.started_frame_index is not None
                else common["observed_frame_index"]
            ),
            started_sim_time_ms=(
                record.started_sim_time_ms
                if record.started_sim_time_ms is not None
                else entry.started_sim_time_ms
            ),
            reason_code=reason,
            evidence_category=category,
            progress=progress,
        )

    def _finish(
        self, handle, status, reason, category, common, *, progress=None,
        completed_sim_time_ms=None,
    ) -> None:
        entry = self._entries[handle]
        record = entry.record
        self._update(
            handle,
            **common,
            execution_status=status,
            started_frame_index=(
                record.started_frame_index
                if record.started_frame_index is not None
                else entry.started_frame_index
                if entry.started_frame_index is not None
                else common["observed_frame_index"]
            ),
            started_sim_time_ms=(
                record.started_sim_time_ms
                if record.started_sim_time_ms is not None
                else entry.started_sim_time_ms
            ),
            completed_frame_index=common["observed_frame_index"],
            # Existing facts establish the containing frame but do not always
            # expose an exact within-window completion timestamp.  Keep that
            # value unknown instead of substituting the polling time.
            completed_sim_time_ms=completed_sim_time_ms,
            reason_code=reason,
            evidence_category=category,
            progress={} if progress is None else progress,
        )

    def end_active(self, *, frame_index: int, sim_time_ms: int, reason_code: str) -> None:
        for handle, entry in tuple(self._entries.items()):
            if not _is_terminal_record(entry.record):
                self._finish(
                    handle, ExecutionStatus.UNKNOWN, reason_code,
                    "tracking_boundary",
                    {
                        "observed_frame_index": frame_index,
                        "observed_sim_time_ms": sim_time_ms,
                    },
                    progress=entry.record.progress,
                )

    def latest_handles(self, side: str) -> tuple[str, ...]:
        return tuple(self._latest_handles[_side(side)])

    def retains(self, handle: str) -> bool:
        """Whether a command index must still retain this opaque handle."""

        return str(handle) in self._entries

    def get(self, side: str, handle: str) -> OperationExecutionRecord:
        normalized = _side(side)
        entry = self._entries.get(str(handle))
        if entry is None or entry.record.side != normalized:
            raise ExecutionQueryError(
                "execution_record_unavailable",
                "execution handle is expired, unknown, or inaccessible",
            )
        return deepcopy(entry.record)

    def current(self, side: str, *, limit: int = 100) -> tuple[OperationExecutionRecord, ...]:
        normalized = _side(side)
        _limit(limit)
        values = [
            entry.record
            for entry in self._entries.values()
            if entry.record.side == normalized
            and not _is_terminal_record(entry.record)
        ]
        values.sort(key=lambda value: value.execution_handle)
        return deepcopy(tuple(values[:limit]))

    def changes(
        self, side: str, *, cursor: str | None = None, limit: int = 100,
    ) -> OperationExecutionPage:
        normalized = _side(side)
        _limit(limit)
        revision, cursor_token = self._resolve_cursor(normalized, cursor)
        changes = self._changes[normalized]
        if changes and revision < changes[0][0] - 1:
            raise ExecutionQueryError(
                "execution_cursor_expired",
                "cursor precedes the retained change window",
            )
        candidates = [
            (value_revision, record)
            for value_revision, record in changes
            if value_revision > revision
        ]
        selected = candidates[:limit]
        next_revision = revision if not selected else selected[-1][0]
        next_cursor = self._remember_cursor(normalized, next_revision)
        return OperationExecutionPage(
            records=deepcopy(tuple(record for _, record in selected)),
            cursor=cursor_token,
            next_cursor=next_cursor,
            has_more=len(candidates) > len(selected),
        )

    def _update(self, handle: str, **changes: object) -> None:
        entry = self._entries[handle]
        before_terminal = _is_terminal_record(entry.record)
        candidate = replace(entry.record, **changes)
        if candidate == entry.record:
            return
        entry.record = candidate
        after_terminal = _is_terminal_record(candidate)
        self._publish(handle, candidate)
        if not before_terminal and after_terminal:
            terminal = self._terminal[candidate.side]
            terminal.append(handle)
            while len(terminal) > self.terminal_retention:
                expired = terminal.popleft()
                self._entries.pop(expired, None)

    def _publish(self, handle: str, record: OperationExecutionRecord) -> None:
        side = record.side
        self._revision[side] += 1
        current = replace(record, revision=self._revision[side])
        if handle in self._entries:
            self._entries[handle].record = current
        changes = self._changes[side]
        changes.append((self._revision[side], deepcopy(current)))
        while len(changes) > self.change_retention:
            changes.popleft()

    def _active_count(self, side: str) -> int:
        return sum(
            entry.record.side == side and not _is_terminal_record(entry.record)
            for entry in self._entries.values()
        )

    def _resolve_cursor(
        self, side: str, cursor: str | None,
    ) -> tuple[int, str]:
        if cursor is None or (type(cursor) is int and cursor == 0):
            token = self._cursor_token(side, 0)
            return 0, token
        if type(cursor) is int:
            raise ExecutionQueryError(
                "execution_cursor_version_unsupported",
                "numeric lifecycle cursors are not valid for page schema 2.0",
            )
        if not isinstance(cursor, str) or not cursor:
            raise ValueError("cursor must be an opaque cursor string or None")
        revision = self._issued_cursors[side].get(cursor)
        if revision is None:
            raise ExecutionQueryError(
                "execution_cursor_invalid",
                "cursor is expired, unknown, or bound to another query scope",
            )
        return revision, cursor

    def _remember_cursor(self, side: str, revision: int) -> str:
        token = self._cursor_token(side, revision)
        issued = self._issued_cursors[side]
        order = self._issued_cursor_order[side]
        if token not in issued:
            issued[token] = revision
            order.append(token)
        while len(order) > self.change_retention * 2 + 2:
            issued.pop(order.popleft(), None)
        return token

    def _cursor_token(self, side: str, revision: int) -> str:
        return "execution-cursor-" + canonical_hash({
            "episode_id": self.episode_id,
            "side": side,
            "revision": revision,
            "schema_version": EXECUTION_PAGE_SCHEMA_VERSION,
        })[:24]

    @staticmethod
    def _remember_start(entry: _Entry, frame: int, event: Mapping[str, Any]) -> None:
        if entry.started_frame_index is None:
            entry.started_frame_index = frame
            entry.started_sim_time_ms = _event_time(event)


def _side(value: str) -> str:
    normalized = str(value).lower()
    if normalized not in {"red", "blue"}:
        raise ValueError("side must be RED or BLUE")
    return normalized


def _limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 256:
        raise ValueError("limit must be an exact int in [1, 256]")


def _operation_progress(operation) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in ("destination_hex", "target_hex", "target_id", "weapon_id"):
        value = getattr(operation, name)
        if value is not None:
            result[name] = value
    return result


def _event_time(event: Mapping[str, Any] | None) -> int | None:
    if event is None or type(event.get("event_time_ms")) is not int:
        return None
    return int(event["event_time_ms"])


def _is_terminal_record(record: OperationExecutionRecord) -> bool:
    if record.execution_status in TERMINAL_EXECUTION_STATUSES:
        return True
    if record.command_status.startswith("REJECTED") or record.command_status in {
        "SUPERSEDED",
        "CANCELLED_ON_TERMINAL",
        "CANCELLED_ON_TRUNCATION",
        "CANCELLED_ON_RUNNER_LIMIT",
        "CANCELLED_ON_USER_STOP",
        "CANCELLED_ON_FAILURE",
        "ABORTED_ATOMIC_PREFLIGHT",
    }:
        return True
    return (
        record.execution_status is ExecutionStatus.UNKNOWN
        and record.evidence_category in {
            "action_result_missing", "unsupported_operation", "tracking_boundary",
        }
    )


__all__ = [
    "EXECUTION_AVAILABILITY_SCHEMA_VERSION",
    "EXECUTION_PAGE_SCHEMA_VERSION",
    "EXECUTION_RECORD_SCHEMA_VERSION",
    "ExecutionQueryError",
    "ExecutionStatus",
    "OperationExecutionAvailability",
    "OperationExecutionPage",
    "OperationExecutionRecord",
    "OperationExecutionTracker",
    "TRACKED_CONTINUOUS_OPERATIONS",
]
