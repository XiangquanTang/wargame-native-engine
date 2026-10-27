"""Versioned current Native facts, not inferred Source compatibility fields.

Only Source contract 1.2 exports this payload. Historical recordings without it
must not acquire present-day facts through defaults or template backfilling.
"""
from __future__ import annotations

from typing import Any

from wargame_native_engine.compat.source_frame.contract import (
    NATIVE_ANALYSIS_FACTS_SCHEMA_VERSION,
)
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.projection.operator_lifecycle import project_operator_conditions


def project_native_analysis_facts(operator: OperatorState) -> dict[str, Any]:
    """Return detached, constant-size facts from one current operator only.

    Movement lock is a narrow runtime fact, not a complete MOVE permission.
    Suppression follows its timer, never the stale imported ``keep`` field.
    Fatigue retains both domain applicability and the actual Native level.
    Loss of control is an analysis-policy interpretation of suppression, not a
    second runtime condition; export that underlying fact only once.
    """
    fatigue, suppression, integrity = project_operator_conditions(operator)
    return {
        "schema_version": NATIVE_ANALYSIS_FACTS_SCHEMA_VERSION,
        "movement_locked": operator.movement_locked,
        "suppression": {
            "active": suppression.active,
            "remaining_ms": suppression.remaining_ms,
        },
        "fatigue": {
            "applicability": fatigue.applicability.value,
            "level": fatigue.level,
            "native_level": operator.fatigue_level,
        },
        "boarding": {
            "mounted_on": operator.mounted_on,
            "passenger_id": operator.passenger_id,
            "on_board": operator.mounted_on is not None,
        },
        "close_combat": operator.state == "CLOSE_COMBAT",
        "integrity": {"status": integrity.status.value, "codes": list(integrity.codes)},
    }
