"""Non-authoritative service/control audit records."""

from __future__ import annotations

from typing import Any, Mapping

from wargame_native_engine.io.serialization import canonical_json, json_clone


CONTROL_AUDIT_NAME = "control_audit.native.jsonl"


class ControlAuditLog:
    """In-memory, append-only control evidence outside fact replay hashes."""

    def __init__(self, session_id: str) -> None:
        self.session_id = str(session_id)
        self._records: list[dict[str, Any]] = []

    def append(self, event: str, **fields: object) -> None:
        self._records.append({
            "schema_version": "native-control-audit/1.0",
            "audit_sequence": len(self._records),
            "session_id": self.session_id,
            "event": str(event),
            **json_clone(fields),
        })

    @property
    def records(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(json_clone(self._records))

    def jsonl_bytes(self) -> bytes:
        return (
            "".join(canonical_json(record) + "\n" for record in self._records)
        ).encode("utf-8")
