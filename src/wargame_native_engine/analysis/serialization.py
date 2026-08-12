"""Pure JSON-safe record conversion for ``FrameAnalysis``.

Converts every dataclass field explicitly — no pickle, no raw frame
access, no sidecar I/O.  The output dict passes ``json.dumps`` with
``allow_nan=False`` and ``sort_keys=True``.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy

from wargame_native_engine.analysis.eight_factor_contract import (
    FactorEvidence,
    FactorStatus,
)
from wargame_native_engine.analysis.models import (
    AdvantageAnalysis,
    AnalysisEvent,
    AnalysisProvenance,
    FactorValue,
    FrameAnalysis,
    SideAnalysis,
    UnitFactorAnalysis,
)

ANALYSIS_SCHEMA = "native-eight-factor-frame-analysis/1.0"


def frame_analysis_to_record(
    frame: FrameAnalysis,
    *,
    events: tuple[AnalysisEvent, ...] = (),
) -> dict[str, object]:
    """Convert *frame* (and optional *events*) into a JSON-safe dict.

    Raises ``ValueError`` when any event's ``frame_index`` does not
    match the frame, when a float value is non-finite (NaN / Infinity),
    or when a value is not representable in JSON.
    """

    # ── validate events belong to this frame ────────────────────────
    frame_idx = frame.provenance.frame_index
    for event in events:
        if event.frame_index != frame_idx:
            raise ValueError(
                f"event.frame_index {event.frame_index} does not "
                f"match frame.provenance.frame_index {frame_idx}"
            )

    # ── build record ────────────────────────────────────────────────
    record: dict[str, object] = {
        "analysis_schema": ANALYSIS_SCHEMA,
        "provenance": _provenance_to_dict(frame.provenance),
        "unit_analyses": [
            _unit_analysis_to_dict(ua) for ua in frame.unit_analyses
        ],
        "side_analyses": [
            _side_analysis_to_dict(sa) for sa in frame.side_analyses
        ],
        "advantage": _advantage_to_dict(frame.advantage),
        "events": [
            _event_to_dict(e) for e in _sort_events(events)
        ],
        "warnings": list(frame.warnings),
    }

    # ── validate float finiteness ───────────────────────────────────
    _require_finite_floats(record)

    # ── validate JSON round-trip ────────────────────────────────────
    try:
        json.dumps(record, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise type(exc)(
            f"Record is not JSON-safe: {exc}"
        ) from exc

    return record


# ── per-type converters ─────────────────────────────────────────────


def _provenance_to_dict(p: AnalysisProvenance) -> dict[str, object]:
    return {
        "perspective": p.perspective,
        "frame_index": p.frame_index,
        "frame_hash": p.frame_hash,
        "scenario_hash": p.scenario_hash,
        "model_id": p.model_id,
        "model_version": p.model_version,
        "model_hash": p.model_hash,
    }


def _unit_analysis_to_dict(ua: UnitFactorAnalysis) -> dict[str, object]:
    return {
        "operator_id": ua.operator_id,
        "side": ua.side,
        "factor_evidence": [
            _factor_evidence_to_dict(ev) for ev in ua.factor_evidence
        ],
        "factor_values": [
            _factor_value_to_dict(fv) for fv in ua.factor_values
        ],
        "effective_power": ua.effective_power,
        "valid": ua.valid,
        "observed_factor_count": ua.observed_factor_count,
        "warnings": list(ua.warnings),
    }


def _factor_evidence_to_dict(ev: FactorEvidence) -> dict[str, object]:
    return {
        "name": ev.name,
        "raw_value": _to_json_value(deepcopy(ev.raw_value)),
        "source_paths": list(ev.source_paths),
        "status": ev.status.value,
        "missing_fields": list(ev.missing_fields),
    }


def _factor_value_to_dict(fv: FactorValue) -> dict[str, object]:
    return {
        "factor_name": fv.factor_name,
        "value": fv.value,
        "status": fv.status.value,
        "source_paths": list(fv.source_paths),
        "formula_id": fv.formula_id,
        "warnings": list(fv.warnings),
        "missing_fields": list(fv.missing_fields),
    }


def _side_analysis_to_dict(sa: SideAnalysis) -> dict[str, object]:
    return {
        "side": sa.side,
        "unit_ids": list(sa.unit_ids),
        "unit_count": sa.unit_count,
        "observed_unit_count": sa.observed_unit_count,
        "effective_power": sa.effective_power,
        "coverage": sa.coverage,
        "confidence": sa.confidence,
        "warnings": list(sa.warnings),
    }


def _advantage_to_dict(adv: AdvantageAnalysis) -> dict[str, object]:
    return {
        "raw_advantage": adv.raw_advantage,
        "normalized_advantage": adv.normalized_advantage,
        "label": adv.label,
        "confidence": adv.confidence,
        "warnings": list(adv.warnings),
    }


def _event_to_dict(event: AnalysisEvent) -> dict[str, object]:
    return {
        "event_type": event.event_type,
        "previous_frame_index": event.previous_frame_index,
        "frame_index": event.frame_index,
        "from_label": event.from_label,
        "to_label": event.to_label,
        "delta_advantage": event.delta_advantage,
    }


# ── helpers ─────────────────────────────────────────────────────────


def _sort_events(
    events: tuple[AnalysisEvent, ...],
) -> tuple[AnalysisEvent, ...]:
    """Sort events deterministically by fixed field order."""
    return tuple(sorted(
        events,
        key=lambda e: (
            e.previous_frame_index,
            e.frame_index,
            e.event_type,
            e.from_label,
            e.to_label,
        ),
    ))


def _to_json_value(value: object, path: str = "raw_value") -> object:
    """Deep-convert *value* to a JSON-safe equivalent.

    Tuples become lists (recursively).  Dict keys must be strings —
    non-string keys raise ``TypeError`` with the offending *path*.
    Other non-JSON-safe scalars are caught later by ``json.dumps``
    validation.
    """
    if isinstance(value, tuple):
        return [
            _to_json_value(v, f"{path}[{i}]")
            for i, v in enumerate(value)
        ]
    if isinstance(value, list):
        return [
            _to_json_value(v, f"{path}[{i}]")
            for i, v in enumerate(value)
        ]
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for k, v in value.items():  # type: ignore[union-attr]
            if not isinstance(k, str):
                raise TypeError(
                    f"Non-string dict key at {path}: {k!r}"
                )
            result[k] = _to_json_value(v, f"{path}.{k}")
        return result
    return value


def _require_finite_floats(obj: object, path: str = "$") -> None:
    """Recursively check that every float in *obj* is finite."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(
                f"Non-finite float at {path}: {obj!r}"
            )
    elif isinstance(obj, dict):
        for key, val in obj.items():  # type: ignore[assignment]
            _require_finite_floats(val, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            _require_finite_floats(item, f"{path}[{idx}]")
