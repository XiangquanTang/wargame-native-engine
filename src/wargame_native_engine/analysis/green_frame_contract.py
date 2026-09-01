"""Pure-function GREEN-frame analysis contract.

Builds a ``FrameAnalysis`` from a detached GREEN frame dict using
demo-compatible eight-factor formulas, side aggregation and symmetric
GREEN advantage.
"""

from __future__ import annotations

from typing import Any, Mapping

from wargame_native_engine.analysis.advantage import calculate_green_advantage
from wargame_native_engine.analysis.demo_compat_factors import (
    ammo_factor,
    base_value_factor,
    health_factor,
    information_factor,
    mobility_factor,
    readiness_factor,
    status_factor,
    weapon_factor,
)
from wargame_native_engine.analysis.effective_power import (
    calculate_unit_effective_power,
)
from wargame_native_engine.analysis.eight_factor_contract import (
    FactorEvidence,
    FactorStatus,
    extract_unit_factor_evidence,
)
from wargame_native_engine.analysis.models import (
    AnalysisProvenance,
    FactorValue,
    FrameAnalysis,
    SideAnalysis,
    UnitFactorAnalysis,
)
from wargame_native_engine.analysis.side_aggregation import (
    aggregate_side_analysis,
)
from wargame_native_engine.analysis.semantic_identity import (
    ANALYZER_MODEL_HASH,
    ANALYZER_MODEL_ID,
    ANALYZER_MODEL_VERSION,
    FACTOR_FORMULA_IDS,
)

# factor_name → callable(unit) → float | None
_FACTOR_DISPATCH = {
    "base_value": base_value_factor,
    "health": health_factor,
    "weapon": weapon_factor,
    "ammo": ammo_factor,
    "readiness": readiness_factor,
    "mobility": mobility_factor,
    "information": information_factor,
    "status": status_factor,
}


def build_green_frame_analysis(
    frame: Mapping[str, Any],
    *,
    frame_index: int,
    frame_hash: str | None = None,
    scenario_hash: str | None = None,
    model_id: str = ANALYZER_MODEL_ID,
    model_version: str = ANALYZER_MODEL_VERSION,
    model_hash: str | None = ANALYZER_MODEL_HASH,
) -> FrameAnalysis:
    """Build a GREEN-only ``FrameAnalysis`` from a single frame.

    Eight demo-compatible factors are computed per unit, multiplied
    into ``effective_power``, aggregated per side, and combined into
    a symmetric GREEN advantage score.
    """

    if not isinstance(frame, Mapping):
        raise TypeError(
            f"frame must be a Mapping, got {type(frame).__name__}"
        )

    provenance = AnalysisProvenance(
        perspective="GREEN",
        frame_index=frame_index,
        frame_hash=frame_hash,
        scenario_hash=scenario_hash,
        model_id=model_id,
        model_version=model_version,
        model_hash=model_hash,
    )

    # ── collect & sort units ──────────────────────────────────────────
    units: list[Mapping[str, Any]] = []
    seen_ids: set[int] = set()

    for collection_name in ("operators", "passengers"):
        collection = frame.get(collection_name)
        if collection is None:
            continue
        if not isinstance(collection, (list, tuple)):
            raise TypeError(
                f"frame[{collection_name!r}] must be a sequence, "
                f"got {type(collection).__name__}"
            )
        for item in collection:
            if not isinstance(item, Mapping):
                raise TypeError(
                    f"every item in frame[{collection_name!r}] must be a "
                    f"Mapping, got {type(item).__name__}"
                )
            obj_id = _obj_id(item)
            if obj_id in seen_ids:
                raise ValueError(
                    f"duplicate obj_id {obj_id} across "
                    f"operators and passengers"
                )
            seen_ids.add(obj_id)
            units.append(item)

    # Stable sort by obj_id.
    units.sort(key=_obj_id)

    # ── unit analyses ─────────────────────────────────────────────────
    unit_analyses: list[UnitFactorAnalysis] = []
    all_warnings: list[str] = []

    for unit in units:
        evidence = extract_unit_factor_evidence(unit)
        factor_values = _build_factor_values(evidence, unit)
        obs_count = sum(
            1 for ev in evidence if ev.status is FactorStatus.OBSERVED
        )

        eff_power = calculate_unit_effective_power(factor_values)
        valid = eff_power is not None
        unit_warnings: tuple[str, ...] = ()
        if not valid:
            unit_warnings = ("effective_power_unavailable",)

        unit_analyses.append(
            UnitFactorAnalysis(
                operator_id=_obj_id(unit),
                side=_side(unit),
                factor_evidence=evidence,
                factor_values=factor_values,
                effective_power=eff_power,
                valid=valid,
                observed_factor_count=obs_count,
                warnings=unit_warnings,
            )
        )

    # ── side analyses (red, blue — fixed order) ───────────────────────
    unit_analyses_tuple = tuple(unit_analyses)
    side_analyses: tuple[SideAnalysis, ...] = (
        aggregate_side_analysis(unit_analyses_tuple, side="red"),
        aggregate_side_analysis(unit_analyses_tuple, side="blue"),
    )

    # ── advantage ─────────────────────────────────────────────────────
    advantage = calculate_green_advantage(
        side_analyses[0], side_analyses[1],
    )

    return FrameAnalysis(
        provenance=provenance,
        unit_analyses=tuple(unit_analyses),
        side_analyses=side_analyses,
        advantage=advantage,
        warnings=tuple(all_warnings),
    )


# ── helpers ───────────────────────────────────────────────────────────


def _obj_id(unit: Mapping[str, Any]) -> int:
    return int(unit["obj_id"])


def _side(unit: Mapping[str, Any]) -> str:
    color = unit.get("color")
    if color == 0:
        return "red"
    if color == 1:
        return "blue"
    return "UNKNOWN"


def _any_raw_none(ev: FactorEvidence) -> bool:
    """True when any source field's raw value was ``None``."""
    rv = ev.raw_value
    if rv is None:
        return True
    if isinstance(rv, tuple):
        return any(v is None for v in rv)
    return False


def _build_factor_values(
    evidence: tuple[FactorEvidence, ...],
    unit: Mapping[str, Any],
) -> tuple[FactorValue, ...]:
    """Produce typed ``FactorValue`` records from evidence + unit.

    All eight factors use demo-compatible formulas.  When evidence
    status is UNKNOWN the value is ``None`` regardless of the formula.
    """

    result: list[FactorValue] = []
    for ev in evidence:
        formula_id = FACTOR_FORMULA_IDS[ev.name]
        if ev.status is FactorStatus.UNKNOWN:
            result.append(
                FactorValue(
                    factor_name=ev.name,
                    value=None,
                    status=ev.status,
                    source_paths=ev.source_paths,
                    formula_id=formula_id,
                    warnings=(),
                    missing_fields=ev.missing_fields,
                )
            )
            continue

        # OBSERVED — apply demo-compatible formula.
        compute = _FACTOR_DISPATCH[ev.name]
        factor_val = compute(unit)

        fv_warnings: tuple[str, ...] = ()
        if factor_val is not None and _any_raw_none(ev):
            fv_warnings = ("demo_default_applied",)

        result.append(
            FactorValue(
                factor_name=ev.name,
                value=factor_val,
                status=ev.status,
                source_paths=ev.source_paths,
                formula_id=formula_id,
                warnings=fv_warnings,
                missing_fields=ev.missing_fields,
            )
        )
    return tuple(result)
