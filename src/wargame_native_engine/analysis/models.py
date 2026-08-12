"""Immutable analysis output types for the Native eight-factor model.

Unit-level values, effective power, side aggregation and GREEN symmetric
advantage are computed via demo-compatible formulas.  ``confidence``
fields remain ``None`` — no confidence formula has been defined yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from wargame_native_engine.analysis.eight_factor_contract import (
    FactorEvidence,
    FactorStatus,
)


def _validate_frame_index(value: object, *, field_name: str = "frame_index") -> None:
    """Enforce the shared non-negative, exact-int frame index contract."""

    if type(value) is not int:
        raise TypeError(
            f"{field_name} must be an int (not bool), "
            f"got {type(value).__name__}"
        )
    if value < 0:
        raise ValueError(f"{field_name} must be >= 0, got {value}")


@dataclass(frozen=True, slots=True)
class FactorValue:
    """A single factor's typed value after formula application.

    ``value`` is ``None`` when the factor is UNKNOWN or the formula
    cannot produce a result; otherwise it holds the computed float.
    """

    factor_name: str
    value: float | None
    status: FactorStatus
    source_paths: tuple[str, ...]
    formula_id: str
    warnings: tuple[str, ...]
    missing_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalysisProvenance:
    """Metadata identifying the analysis run and its input snapshot."""

    perspective: str
    frame_index: int
    frame_hash: str | None
    scenario_hash: str | None
    model_id: str
    model_version: str
    model_hash: str | None

    def __post_init__(self) -> None:
        _validate_frame_index(self.frame_index)


@dataclass(frozen=True, slots=True)
class UnitFactorAnalysis:
    """Per-unit eight-factor evidence → values → effective power.

    ``effective_power`` is the product of all eight factor values, or
    ``None`` when any factor is unavailable.  ``valid`` is ``True``
    exactly when ``effective_power is not None`` (including ``0.0``).
    """

    operator_id: int
    side: str
    factor_evidence: tuple[FactorEvidence, ...]
    factor_values: tuple[FactorValue, ...]
    effective_power: float | None
    valid: bool
    observed_factor_count: int
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SideAnalysis:
    """Per-side aggregate: effective power, coverage and observed-units.

    ``effective_power`` is the sum of valid unit powers (``None`` when
    no valid units exist).  ``coverage`` is the valid-to-total unit
    ratio, or ``None`` for an empty side.  ``confidence`` is not yet
    implemented.
    """

    side: str
    unit_ids: tuple[int, ...]
    unit_count: int
    observed_unit_count: int
    effective_power: float | None
    coverage: float | None
    confidence: float | None
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdvantageAnalysis:
    """GREEN symmetric advantage: raw = red - blue, normalized in [-1, 1].

    Positive values favour RED; negative favour BLUE.  ``label`` is
    ``"RED"``, ``"BLUE"``, ``"EVEN"`` or ``"UNKNOWN"``.  ``confidence``
    is not yet implemented.
    """

    raw_advantage: float | None
    normalized_advantage: float | None
    label: str
    confidence: float | None
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FrameAnalysis:
    """Complete single-frame GREEN analysis contract."""

    provenance: AnalysisProvenance
    unit_analyses: tuple[UnitFactorAnalysis, ...]
    side_analyses: tuple[SideAnalysis, ...]
    advantage: AdvantageAnalysis
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalysisTimeline:
    """Ordered GREEN frame analyses and their advantage series.

    ``delta_advantages[t]`` is strictly the normalized advantage delta
    ``normalized_advantage[t] - normalized_advantage[t-1]``.  It is not a
    raw-advantage delta or a rate, and frame indices are not treated as time.
    Its first value is always ``None``; later values are also ``None`` when
    either adjacent normalized value is unavailable.
    """

    frame_analyses: tuple[FrameAnalysis, ...]
    frame_indices: tuple[int, ...]
    raw_advantages: tuple[float | None, ...]
    normalized_advantages: tuple[float | None, ...]
    delta_advantages: tuple[float | None, ...]


@dataclass(frozen=True, slots=True)
class AnalysisEvent:
    """A discrete change in GREEN advantage label between adjacent frames.

    Generated only when both adjacent frames carry a known label
    (RED / BLUE / EVEN), the labels differ, and the normalized
    delta for the transition is available.
    """

    event_type: str
    previous_frame_index: int
    frame_index: int
    from_label: str
    to_label: str
    delta_advantage: float | None

    def __post_init__(self) -> None:
        _validate_frame_index(
            self.previous_frame_index,
            field_name="previous_frame_index",
        )
        _validate_frame_index(self.frame_index)
