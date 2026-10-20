"""Observer-only action lifecycle aggregation from explicit Native evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence


ACTION_LIFECYCLE_STATS_SCHEMA_VERSION = "native-action-lifecycle-statistics/1.0"
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "PENDING", "UNKNOWN"}


@dataclass(frozen=True, slots=True)
class ActionExecutionIdentity:
    episode_id: str
    session_id: str
    side: str
    actor_id: int | None
    action_id: str
    request_id: str
    start_window: int
    mission_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("episode_id", "session_id", "action_id", "request_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty")
        if self.side not in {"red", "blue"}:
            raise ValueError("side must be red or blue")
        if type(self.start_window) is not int or self.start_window < 0:
            raise ValueError("start_window must be non-negative")


@dataclass(slots=True)
class _Execution:
    identity: ActionExecutionIdentity
    action_type: str
    weapon_id: int | None
    selected: bool = False
    submitted: bool = False
    accepted: bool = False
    rejected: bool = False
    in_progress: bool = False
    terminal_status: str | None = None
    evidence: set[str] = field(default_factory=set)


class ActionLifecycleStatistics:
    """Aggregate explicit catalog, submission and lifecycle facts.

    Callers supply identities and evidence keys from existing engine facts.  The
    collector performs no adjudication and never guesses correlation by time.
    """

    def __init__(self) -> None:
        self._catalog: dict[tuple[str, str, int, str, str], int] = {}
        self._executions: dict[ActionExecutionIdentity, _Execution] = {}

    def observe_catalog(
        self, *, session_id: str, side: str, window: int,
        action_types: Sequence[str], catalog_identity: str = "UNVERSIONED",
    ) -> None:
        counts: dict[str, int] = {}
        for action_type in action_types:
            counts[str(action_type)] = counts.get(str(action_type), 0) + 1
        for action_type, count in counts.items():
            key = (session_id, side, window, catalog_identity, action_type)
            previous = self._catalog.get(key)
            if previous is not None and previous != count:
                raise ValueError("catalog evidence changed for a frozen window")
            self._catalog[key] = count

    def selected(
        self, identity: ActionExecutionIdentity, *, action_type: str,
        weapon_id: int | None = None,
    ) -> None:
        value = self._execution(identity, action_type, weapon_id)
        value.selected = True

    def submitted(self, identity: ActionExecutionIdentity) -> None:
        self._require(identity).submitted = True

    def submission_result(
        self, identity: ActionExecutionIdentity, *, accepted: bool,
        evidence_key: str,
    ) -> None:
        value = self._require(identity)
        if evidence_key in value.evidence:
            return
        value.evidence.add(evidence_key)
        if accepted:
            value.accepted = True
            value.in_progress = True
        else:
            value.rejected = True
            value.in_progress = False
            value.terminal_status = "FAILED"

    def lifecycle(
        self, identity: ActionExecutionIdentity, *, status: str,
        evidence_key: str,
    ) -> None:
        value = self._require(identity)
        normalized = str(status).upper()
        if normalized not in _TERMINAL | {"IN_PROGRESS"}:
            raise ValueError("unsupported explicit lifecycle status")
        if evidence_key in value.evidence:
            return
        value.evidence.add(evidence_key)
        if normalized == "IN_PROGRESS":
            if value.terminal_status is None:
                value.in_progress = True
            return
        if value.terminal_status is not None and value.terminal_status != normalized:
            raise ValueError("conflicting terminal lifecycle evidence")
        value.terminal_status = normalized
        value.in_progress = False

    def truncate(self, *, session_id: str, episode_id: str) -> None:
        for value in self._executions.values():
            identity = value.identity
            if identity.session_id != session_id or identity.episode_id != episode_id:
                continue
            if value.terminal_status is None:
                value.terminal_status = (
                    "PENDING" if value.accepted else "UNKNOWN"
                )
                value.in_progress = False

    def to_dict(self) -> Mapping[str, object]:
        by_type: dict[str, dict[str, int]] = {}
        for (
            _session, _side, _window, _catalog_identity, action_type,
        ), count in self._catalog.items():
            bucket = by_type.setdefault(action_type, self._empty_counts())
            bucket["catalog_available"] += count
        records = []
        for identity, value in sorted(
            self._executions.items(),
            key=lambda item: (
                item[0].session_id, item[0].side, item[0].start_window,
                item[0].actor_id if item[0].actor_id is not None else -1,
                item[0].request_id, item[0].action_id,
            ),
        ):
            bucket = by_type.setdefault(value.action_type, self._empty_counts())
            for name in ("selected", "submitted", "accepted", "rejected", "in_progress"):
                bucket[name] += int(getattr(value, name))
            if value.terminal_status is not None:
                bucket[value.terminal_status.lower()] += 1
            records.append({
                "episode_id": identity.episode_id,
                "session_id": identity.session_id,
                "side": identity.side,
                "actor_id": identity.actor_id,
                "action_id": identity.action_id,
                "request_id": identity.request_id,
                "start_window": identity.start_window,
                "mission_id": identity.mission_id,
                "action_type": value.action_type,
                "weapon_id": value.weapon_id,
                "selected": value.selected,
                "submitted": value.submitted,
                "accepted": value.accepted,
                "rejected": value.rejected,
                "in_progress": value.in_progress,
                "terminal_status": value.terminal_status,
                "evidence_count": len(value.evidence),
            })
        return {
            "schema_version": ACTION_LIFECYCLE_STATS_SCHEMA_VERSION,
            "catalog_count_semantics": "sum of available entries over frozen windows",
            "execution_count_semantics": "explicit execution identities, not action-id global deduplication",
            "by_action_type": {key: by_type[key] for key in sorted(by_type)},
            "executions": records,
        }

    def _execution(
        self, identity: ActionExecutionIdentity, action_type: str,
        weapon_id: int | None,
    ) -> _Execution:
        current = self._executions.get(identity)
        if current is None:
            current = _Execution(identity, str(action_type).upper(), weapon_id)
            self._executions[identity] = current
        elif (current.action_type, current.weapon_id) != (
            str(action_type).upper(), weapon_id,
        ):
            raise ValueError("execution identity reused with different semantics")
        return current

    def _require(self, identity: ActionExecutionIdentity) -> _Execution:
        try:
            return self._executions[identity]
        except KeyError as exc:
            raise ValueError("execution must be selected before later stages") from exc

    @staticmethod
    def _empty_counts() -> dict[str, int]:
        return {
            "catalog_available": 0,
            "selected": 0,
            "submitted": 0,
            "accepted": 0,
            "rejected": 0,
            "in_progress": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "pending": 0,
            "unknown": 0,
        }
