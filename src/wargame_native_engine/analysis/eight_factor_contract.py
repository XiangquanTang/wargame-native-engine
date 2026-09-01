from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from wargame_native_engine.analysis.semantic_identity import (
    FACTOR_ORDER,
    FACTOR_SOURCE_FIELDS,
)


class FactorStatus(str, Enum):
    OBSERVED = "OBSERVED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FactorEvidence:
    name: str
    raw_value: Any
    source_paths: tuple[str, ...]
    status: FactorStatus
    missing_fields: tuple[str, ...]


def _prefixed(key: str) -> str:
    return f"unit.{key}"


def _copy_value(value: Any) -> Any:
    """Deep-copy every value so evidence is fully detached from input."""
    return deepcopy(value)


def extract_unit_factor_evidence(
    unit: Mapping[str, Any],
) -> tuple[FactorEvidence, ...]:
    if not isinstance(unit, Mapping):
        raise TypeError(
            f"Expected Mapping, got {type(unit).__name__}"
        )

    results: list[FactorEvidence] = []
    for factor_name in FACTOR_ORDER:
        field_keys = FACTOR_SOURCE_FIELDS[factor_name]
        missing: list[str] = []
        present_values: dict[str, Any] = {}
        for key in field_keys:
            if key in unit:
                present_values[key] = _copy_value(unit[key])
            else:
                missing.append(key)

        if missing:
            sort_missing = tuple(sorted(missing))
            merged = {**present_values, **{k: None for k in sort_missing}}
            raw = tuple(merged[k] for k in field_keys)
            if len(raw) == 1:
                raw_value = raw[0]
            else:
                raw_value = raw
            evidence = FactorEvidence(
                name=factor_name,
                raw_value=raw_value,
                source_paths=tuple(_prefixed(k) for k in field_keys),
                status=FactorStatus.UNKNOWN,
                missing_fields=sort_missing,
            )
        else:
            raw = tuple(present_values[k] for k in field_keys)
            if len(raw) == 1:
                raw_value = raw[0]
            else:
                raw_value = raw
            evidence = FactorEvidence(
                name=factor_name,
                raw_value=raw_value,
                source_paths=tuple(_prefixed(k) for k in field_keys),
                status=FactorStatus.OBSERVED,
                missing_fields=(),
            )

        results.append(evidence)

    return tuple(results)
