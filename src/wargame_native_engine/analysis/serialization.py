"""Pure JSON-safe record conversion for ``FrameAnalysis``.

Converts every dataclass field explicitly — no pickle, no raw frame
access, no sidecar I/O.  The output dict passes ``json.dumps`` with
``allow_nan=False`` and ``sort_keys=True``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from copy import deepcopy
from typing import NoReturn

from wargame_native_engine.analysis.eight_factor_contract import (
    FACTOR_ORDER,
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


class AnalysisSidecarFormatError(ValueError):
    """Raised when a record does not satisfy the analysis sidecar schema."""

    def __init__(
        self,
        message: str,
        *,
        path: str = "$",
        line_number: int | None = None,
    ) -> None:
        self.path = path
        self.line_number = line_number
        location = path
        if line_number is not None:
            location = f"line {line_number}, {path}"
        super().__init__(f"analysis sidecar format error at {location}: {message}")


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


# ── strict record deserialization ───────────────────────────────────


_TOP_LEVEL_FIELDS = frozenset({
    "analysis_schema",
    "provenance",
    "unit_analyses",
    "side_analyses",
    "advantage",
    "events",
    "warnings",
})
_PROVENANCE_FIELDS = frozenset({
    "perspective",
    "frame_index",
    "frame_hash",
    "scenario_hash",
    "model_id",
    "model_version",
    "model_hash",
})
_UNIT_ANALYSIS_FIELDS = frozenset({
    "operator_id",
    "side",
    "factor_evidence",
    "factor_values",
    "effective_power",
    "valid",
    "observed_factor_count",
    "warnings",
})
_FACTOR_EVIDENCE_FIELDS = frozenset({
    "name",
    "raw_value",
    "source_paths",
    "status",
    "missing_fields",
})
_FACTOR_VALUE_FIELDS = frozenset({
    "factor_name",
    "value",
    "status",
    "source_paths",
    "formula_id",
    "warnings",
    "missing_fields",
})
_SIDE_ANALYSIS_FIELDS = frozenset({
    "side",
    "unit_ids",
    "unit_count",
    "observed_unit_count",
    "effective_power",
    "coverage",
    "confidence",
    "warnings",
})
_ADVANTAGE_FIELDS = frozenset({
    "raw_advantage",
    "normalized_advantage",
    "label",
    "confidence",
    "warnings",
})
_EVENT_FIELDS = frozenset({
    "event_type",
    "previous_frame_index",
    "frame_index",
    "from_label",
    "to_label",
    "delta_advantage",
})
_UNIT_SIDES = frozenset({"red", "blue", "UNKNOWN"})
_ANALYZED_SIDES = frozenset({"red", "blue"})
_ADVANTAGE_LABELS = frozenset({"RED", "BLUE", "EVEN", "UNKNOWN"})
_EVENT_LABELS = frozenset({"RED", "BLUE", "EVEN"})


def frame_analysis_from_record(
    record: Mapping[str, object],
) -> tuple[FrameAnalysis, tuple[AnalysisEvent, ...]]:
    """Strictly reconstruct a frame analysis and its persisted events.

    Every schema object must contain exactly the fields emitted by
    :func:`frame_analysis_to_record`.  Values are validated without coercion;
    mutable JSON evidence is recursively detached from *record*.
    """

    return _frame_analysis_from_record(record, line_number=None)


def _frame_analysis_from_record(
    record: object,
    *,
    line_number: int | None,
) -> tuple[FrameAnalysis, tuple[AnalysisEvent, ...]]:
    obj = _object(record, "$", line_number)
    _fields(obj, _TOP_LEVEL_FIELDS, "$", line_number)

    schema = _string(obj["analysis_schema"], "$.analysis_schema", line_number)
    if schema != ANALYSIS_SCHEMA:
        _fail(
            f"expected {ANALYSIS_SCHEMA!r}, got {schema!r}",
            "$.analysis_schema",
            line_number,
        )

    provenance = _provenance_from_record(
        obj["provenance"], "$.provenance", line_number
    )
    units = _array(obj["unit_analyses"], "$.unit_analyses", line_number)
    unit_analyses = tuple(
        _unit_analysis_from_record(
            value, f"$.unit_analyses[{index}]", line_number
        )
        for index, value in enumerate(units)
    )
    operator_ids = tuple(unit.operator_id for unit in unit_analyses)
    _require_unique(operator_ids, "operator_id", "$.unit_analyses", line_number)

    sides = _array(obj["side_analyses"], "$.side_analyses", line_number)
    side_analyses = tuple(
        _side_analysis_from_record(
            value, f"$.side_analyses[{index}]", line_number
        )
        for index, value in enumerate(sides)
    )
    side_names = tuple(side.side for side in side_analyses)
    _require_unique(side_names, "side", "$.side_analyses", line_number)

    advantage = _advantage_from_record(
        obj["advantage"], "$.advantage", line_number
    )
    warnings = _string_tuple(obj["warnings"], "$.warnings", line_number)

    event_values = _array(obj["events"], "$.events", line_number)
    events = tuple(
        _event_from_record(value, f"$.events[{index}]", line_number)
        for index, value in enumerate(event_values)
    )
    for index, event in enumerate(events):
        if event.frame_index != provenance.frame_index:
            _fail(
                "event frame_index does not match provenance frame_index "
                f"{provenance.frame_index}",
                f"$.events[{index}].frame_index",
                line_number,
            )

    return (
        FrameAnalysis(
            provenance=provenance,
            unit_analyses=unit_analyses,
            side_analyses=side_analyses,
            advantage=advantage,
            warnings=warnings,
        ),
        events,
    )


def _provenance_from_record(
    value: object,
    path: str,
    line_number: int | None,
) -> AnalysisProvenance:
    obj = _object(value, path, line_number)
    _fields(obj, _PROVENANCE_FIELDS, path, line_number)
    perspective = _string(obj["perspective"], f"{path}.perspective", line_number)
    if perspective != "GREEN":
        _fail("expected 'GREEN'", f"{path}.perspective", line_number)

    frame_index = _non_negative_int(
        obj["frame_index"], f"{path}.frame_index", line_number
    )
    try:
        return AnalysisProvenance(
            perspective=perspective,
            frame_index=frame_index,
            frame_hash=_nullable_string(
                obj["frame_hash"], f"{path}.frame_hash", line_number
            ),
            scenario_hash=_nullable_string(
                obj["scenario_hash"], f"{path}.scenario_hash", line_number
            ),
            model_id=_string(obj["model_id"], f"{path}.model_id", line_number),
            model_version=_string(
                obj["model_version"], f"{path}.model_version", line_number
            ),
            model_hash=_nullable_string(
                obj["model_hash"], f"{path}.model_hash", line_number
            ),
        )
    except (TypeError, ValueError) as exc:
        _fail(str(exc), path, line_number, cause=exc)


def _unit_analysis_from_record(
    value: object,
    path: str,
    line_number: int | None,
) -> UnitFactorAnalysis:
    obj = _object(value, path, line_number)
    _fields(obj, _UNIT_ANALYSIS_FIELDS, path, line_number)

    side = _string(obj["side"], f"{path}.side", line_number)
    if side not in _UNIT_SIDES:
        _fail(
            f"expected one of {sorted(_UNIT_SIDES)!r}, got {side!r}",
            f"{path}.side",
            line_number,
        )

    evidence_values = _array(
        obj["factor_evidence"], f"{path}.factor_evidence", line_number
    )
    evidence = tuple(
        _factor_evidence_from_record(
            item, f"{path}.factor_evidence[{index}]", line_number
        )
        for index, item in enumerate(evidence_values)
    )
    factor_values_json = _array(
        obj["factor_values"], f"{path}.factor_values", line_number
    )
    factor_values = tuple(
        _factor_value_from_record(
            item, f"{path}.factor_values[{index}]", line_number
        )
        for index, item in enumerate(factor_values_json)
    )

    evidence_names = tuple(item.name for item in evidence)
    value_names = tuple(item.factor_name for item in factor_values)
    _require_factor_order(
        evidence_names, f"{path}.factor_evidence", line_number
    )
    _require_factor_order(value_names, f"{path}.factor_values", line_number)

    return UnitFactorAnalysis(
        operator_id=_exact_int(
            obj["operator_id"], f"{path}.operator_id", line_number
        ),
        side=side,
        factor_evidence=evidence,
        factor_values=factor_values,
        effective_power=_nullable_number(
            obj["effective_power"], f"{path}.effective_power", line_number
        ),
        valid=_boolean(obj["valid"], f"{path}.valid", line_number),
        observed_factor_count=_non_negative_int(
            obj["observed_factor_count"],
            f"{path}.observed_factor_count",
            line_number,
        ),
        warnings=_string_tuple(obj["warnings"], f"{path}.warnings", line_number),
    )


def _factor_evidence_from_record(
    value: object,
    path: str,
    line_number: int | None,
) -> FactorEvidence:
    obj = _object(value, path, line_number)
    _fields(obj, _FACTOR_EVIDENCE_FIELDS, path, line_number)
    name = _string(obj["name"], f"{path}.name", line_number)
    if name not in FACTOR_ORDER:
        _fail(f"unknown factor name {name!r}", f"{path}.name", line_number)
    return FactorEvidence(
        name=name,
        raw_value=_json_value(obj["raw_value"], f"{path}.raw_value", line_number),
        source_paths=_string_tuple(
            obj["source_paths"], f"{path}.source_paths", line_number
        ),
        status=_factor_status(obj["status"], f"{path}.status", line_number),
        missing_fields=_string_tuple(
            obj["missing_fields"], f"{path}.missing_fields", line_number
        ),
    )


def _factor_value_from_record(
    value: object,
    path: str,
    line_number: int | None,
) -> FactorValue:
    obj = _object(value, path, line_number)
    _fields(obj, _FACTOR_VALUE_FIELDS, path, line_number)
    factor_name = _string(
        obj["factor_name"], f"{path}.factor_name", line_number
    )
    if factor_name not in FACTOR_ORDER:
        _fail(
            f"unknown factor name {factor_name!r}",
            f"{path}.factor_name",
            line_number,
        )
    return FactorValue(
        factor_name=factor_name,
        value=_nullable_number(obj["value"], f"{path}.value", line_number),
        status=_factor_status(obj["status"], f"{path}.status", line_number),
        source_paths=_string_tuple(
            obj["source_paths"], f"{path}.source_paths", line_number
        ),
        formula_id=_string(obj["formula_id"], f"{path}.formula_id", line_number),
        warnings=_string_tuple(obj["warnings"], f"{path}.warnings", line_number),
        missing_fields=_string_tuple(
            obj["missing_fields"], f"{path}.missing_fields", line_number
        ),
    )


def _side_analysis_from_record(
    value: object,
    path: str,
    line_number: int | None,
) -> SideAnalysis:
    obj = _object(value, path, line_number)
    _fields(obj, _SIDE_ANALYSIS_FIELDS, path, line_number)
    side = _string(obj["side"], f"{path}.side", line_number)
    if side not in _ANALYZED_SIDES:
        _fail(
            f"expected one of {sorted(_ANALYZED_SIDES)!r}, got {side!r}",
            f"{path}.side",
            line_number,
        )
    unit_ids_json = _array(obj["unit_ids"], f"{path}.unit_ids", line_number)
    unit_ids = tuple(
        _exact_int(item, f"{path}.unit_ids[{index}]", line_number)
        for index, item in enumerate(unit_ids_json)
    )
    _require_unique(unit_ids, "unit id", f"{path}.unit_ids", line_number)
    return SideAnalysis(
        side=side,
        unit_ids=unit_ids,
        unit_count=_non_negative_int(
            obj["unit_count"], f"{path}.unit_count", line_number
        ),
        observed_unit_count=_non_negative_int(
            obj["observed_unit_count"],
            f"{path}.observed_unit_count",
            line_number,
        ),
        effective_power=_nullable_number(
            obj["effective_power"], f"{path}.effective_power", line_number
        ),
        coverage=_nullable_number(obj["coverage"], f"{path}.coverage", line_number),
        confidence=_nullable_number(
            obj["confidence"], f"{path}.confidence", line_number
        ),
        warnings=_string_tuple(obj["warnings"], f"{path}.warnings", line_number),
    )


def _advantage_from_record(
    value: object,
    path: str,
    line_number: int | None,
) -> AdvantageAnalysis:
    obj = _object(value, path, line_number)
    _fields(obj, _ADVANTAGE_FIELDS, path, line_number)
    label = _string(obj["label"], f"{path}.label", line_number)
    if label not in _ADVANTAGE_LABELS:
        _fail(
            f"expected one of {sorted(_ADVANTAGE_LABELS)!r}, got {label!r}",
            f"{path}.label",
            line_number,
        )
    return AdvantageAnalysis(
        raw_advantage=_nullable_number(
            obj["raw_advantage"], f"{path}.raw_advantage", line_number
        ),
        normalized_advantage=_nullable_number(
            obj["normalized_advantage"],
            f"{path}.normalized_advantage",
            line_number,
        ),
        label=label,
        confidence=_nullable_number(
            obj["confidence"], f"{path}.confidence", line_number
        ),
        warnings=_string_tuple(obj["warnings"], f"{path}.warnings", line_number),
    )


def _event_from_record(
    value: object,
    path: str,
    line_number: int | None,
) -> AnalysisEvent:
    obj = _object(value, path, line_number)
    _fields(obj, _EVENT_FIELDS, path, line_number)
    from_label = _string(obj["from_label"], f"{path}.from_label", line_number)
    to_label = _string(obj["to_label"], f"{path}.to_label", line_number)
    for field_name, label in (("from_label", from_label), ("to_label", to_label)):
        if label not in _EVENT_LABELS:
            _fail(
                f"expected one of {sorted(_EVENT_LABELS)!r}, got {label!r}",
                f"{path}.{field_name}",
                line_number,
            )
    try:
        return AnalysisEvent(
            event_type=_string(
                obj["event_type"], f"{path}.event_type", line_number
            ),
            previous_frame_index=_non_negative_int(
                obj["previous_frame_index"],
                f"{path}.previous_frame_index",
                line_number,
            ),
            frame_index=_non_negative_int(
                obj["frame_index"], f"{path}.frame_index", line_number
            ),
            from_label=from_label,
            to_label=to_label,
            delta_advantage=_nullable_number(
                obj["delta_advantage"],
                f"{path}.delta_advantage",
                line_number,
            ),
        )
    except (TypeError, ValueError) as exc:
        _fail(str(exc), path, line_number, cause=exc)


def _object(
    value: object,
    path: str,
    line_number: int | None,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail(
            f"expected object, got {type(value).__name__}", path, line_number
        )
    for key in value:
        if type(key) is not str:
            _fail(
                f"object key must be a string, got {type(key).__name__}",
                path,
                line_number,
            )
    return value


def _fields(
    obj: Mapping[str, object],
    expected: frozenset[str],
    path: str,
    line_number: int | None,
) -> None:
    actual = set(obj)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing fields {missing!r}")
        if extra:
            details.append(f"unexpected fields {extra!r}")
        _fail("; ".join(details), path, line_number)


def _array(value: object, path: str, line_number: int | None) -> list[object]:
    if type(value) is not list:
        _fail(f"expected array, got {type(value).__name__}", path, line_number)
    return value


def _string(value: object, path: str, line_number: int | None) -> str:
    if type(value) is not str:
        _fail(f"expected string, got {type(value).__name__}", path, line_number)
    return value


def _nullable_string(
    value: object,
    path: str,
    line_number: int | None,
) -> str | None:
    if value is None:
        return None
    return _string(value, path, line_number)


def _boolean(value: object, path: str, line_number: int | None) -> bool:
    if type(value) is not bool:
        _fail(f"expected bool, got {type(value).__name__}", path, line_number)
    return value


def _exact_int(value: object, path: str, line_number: int | None) -> int:
    if type(value) is not int:
        _fail(
            f"expected int (not bool), got {type(value).__name__}",
            path,
            line_number,
        )
    return value


def _non_negative_int(value: object, path: str, line_number: int | None) -> int:
    result = _exact_int(value, path, line_number)
    if result < 0:
        _fail(f"expected value >= 0, got {result}", path, line_number)
    return result


def _number(value: object, path: str, line_number: int | None) -> int | float:
    if type(value) not in (int, float):
        _fail(
            f"expected number (not bool), got {type(value).__name__}",
            path,
            line_number,
        )
    if type(value) is float and not math.isfinite(value):
        _fail(f"expected finite number, got {value!r}", path, line_number)
    return value


def _nullable_number(
    value: object,
    path: str,
    line_number: int | None,
) -> int | float | None:
    if value is None:
        return None
    return _number(value, path, line_number)


def _string_tuple(
    value: object,
    path: str,
    line_number: int | None,
) -> tuple[str, ...]:
    items = _array(value, path, line_number)
    return tuple(
        _string(item, f"{path}[{index}]", line_number)
        for index, item in enumerate(items)
    )


def _factor_status(
    value: object,
    path: str,
    line_number: int | None,
) -> FactorStatus:
    status_value = _string(value, path, line_number)
    try:
        return FactorStatus(status_value)
    except ValueError as exc:
        _fail(f"unknown FactorStatus {status_value!r}", path, line_number, cause=exc)


def _json_value(value: object, path: str, line_number: int | None) -> object:
    """Validate and detach a value in the JSON data model."""

    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _fail(f"expected finite number, got {value!r}", path, line_number)
        return value
    if type(value) is list:
        return [
            _json_value(item, f"{path}[{index}]", line_number)
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        detached: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                _fail(
                    f"object key must be a string, got {type(key).__name__}",
                    path,
                    line_number,
                )
            detached[key] = _json_value(item, f"{path}.{key}", line_number)
        return detached
    _fail(
        f"value is not JSON-compatible: {type(value).__name__}",
        path,
        line_number,
    )


def _require_unique(
    values: tuple[object, ...],
    description: str,
    path: str,
    line_number: int | None,
) -> None:
    seen: set[object] = set()
    for value in values:
        if value in seen:
            _fail(f"duplicate {description} {value!r}", path, line_number)
        seen.add(value)


def _require_factor_order(
    names: tuple[str, ...],
    path: str,
    line_number: int | None,
) -> None:
    _require_unique(names, "factor name", path, line_number)
    positions = tuple(FACTOR_ORDER.index(name) for name in names)
    if positions != tuple(sorted(positions)):
        _fail("factor names are not in canonical order", path, line_number)


def _fail(
    message: str,
    path: str,
    line_number: int | None,
    *,
    cause: BaseException | None = None,
) -> NoReturn:
    error = AnalysisSidecarFormatError(
        message,
        path=path,
        line_number=line_number,
    )
    if cause is None:
        raise error
    raise error from cause
