"""Side-level aggregation for the Native eight-factor model.

Sums per-unit ``effective_power`` values for a side, tracking coverage
and skipping UNKNOWN units.  Matches the demo ``faction_power()``
semantics for GREEN complete-frame inputs.
"""

from __future__ import annotations

from wargame_native_engine.analysis._numeric import require_finite_number
from wargame_native_engine.analysis.eight_factor_contract import FactorStatus
from wargame_native_engine.analysis.models import SideAnalysis, UnitFactorAnalysis

_VALID_SIDES = frozenset({"red", "blue"})


def aggregate_side_analysis(
    unit_analyses: tuple[UnitFactorAnalysis, ...],
    *,
    side: str,
) -> SideAnalysis:
    """Aggregate effective power for *side* from pre-computed unit analyses.

    Raises ``ValueError`` when *side* is not ``"red"`` or ``"blue"``.
    """

    if side not in _VALID_SIDES:
        raise ValueError(
            f"side must be 'red' or 'blue', got {side!r}"
        )

    # Select units belonging to this side, sorted by operator_id.
    selected = sorted(
        (ua for ua in unit_analyses if ua.side == side),
        key=lambda ua: ua.operator_id,
    )

    unit_ids = tuple(ua.operator_id for ua in selected)
    unit_count = len(unit_ids)

    # This function is a public calculation boundary: hand-built unit models
    # must not bypass the finite effective-power contract.
    for ua in selected:
        if ua.effective_power is not None:
            require_finite_number(
                ua.effective_power,
                stage=(
                    f"side aggregation {side} unit {ua.operator_id} "
                    "effective_power"
                ),
            )

    observed_unit_count = sum(
        1 for ua in selected
        if all(ev.status is FactorStatus.OBSERVED
               for ev in ua.factor_evidence)
    )

    valid_units = [
        ua for ua in selected
        if ua.valid and ua.effective_power is not None
    ]
    invalid_units = [
        ua for ua in selected
        if not ua.valid or ua.effective_power is None
    ]

    # ── empty side ─────────────────────────────────────────────────
    if unit_count == 0:
        return SideAnalysis(
            side=side,
            unit_ids=(),
            unit_count=0,
            observed_unit_count=0,
            effective_power=0.0,
            coverage=None,
            confidence=None,
            warnings=(),
        )

    # ── no valid units at all ──────────────────────────────────────
    if not valid_units:
        return SideAnalysis(
            side=side,
            unit_ids=unit_ids,
            unit_count=unit_count,
            observed_unit_count=observed_unit_count,
            effective_power=None,
            coverage=0.0,
            confidence=None,
            warnings=("no_valid_units",),
        )

    # ── sum valid effective powers ─────────────────────────────────
    total = 0.0
    for ua in valid_units:
        total += ua.effective_power  # type: ignore[arg-type]
        require_finite_number(
            total,
            stage=f"side aggregation {side} effective_power sum",
        )

    valid_count = len(valid_units)
    coverage = valid_count / unit_count

    warnings: tuple[str, ...] = ()
    if invalid_units:
        warnings = ("unknown_units_skipped",)

    return SideAnalysis(
        side=side,
        unit_ids=unit_ids,
        unit_count=unit_count,
        observed_unit_count=observed_unit_count,
        effective_power=total,
        coverage=coverage,
        confidence=None,
        warnings=warnings,
    )
