"""GREEN global relative-advantage calculation.

Matches the demo ``CombatBreakdown`` raw / normalized semantics with
``alpha = 1.0``, ``beta = 1.0`` on the full GREEN side effective powers.
This is an **analysis metric** — it does not participate in hit
adjudication, attrition, termination or win/loss decisions.
"""

from __future__ import annotations

from wargame_native_engine.analysis._numeric import require_finite_number
from wargame_native_engine.analysis.models import AdvantageAnalysis, SideAnalysis
from wargame_native_engine.analysis.semantic_identity import ADVANTAGE_FORMULA_ID


def calculate_green_advantage(
    red: SideAnalysis,
    blue: SideAnalysis,
) -> AdvantageAnalysis:
    """Compute GREEN symmetric advantage from pre-aggregated sides.

    Raises ``ValueError`` when *red.side* is not ``"red"`` or
    *blue.side* is not ``"blue"``.
    """

    if red.side != "red":
        raise ValueError(
            f"red.side must be 'red', got {red.side!r}"
        )
    if blue.side != "blue":
        raise ValueError(
            f"blue.side must be 'blue', got {blue.side!r}"
        )

    red_power = red.effective_power
    blue_power = blue.effective_power
    if red_power is not None:
        require_finite_number(
            red_power,
            stage="advantage RED effective_power",
        )
    if blue_power is not None:
        require_finite_number(
            blue_power,
            stage="advantage BLUE effective_power",
        )

    # ── either side unavailable ────────────────────────────────────
    if red_power is None or blue_power is None:
        return AdvantageAnalysis(
            raw_advantage=None,
            normalized_advantage=None,
            label="UNKNOWN",
            confidence=None,
            warnings=("advantage_unavailable",),
        )

    # ── compute advantage ──────────────────────────────────────────
    raw_advantage = red_power - blue_power
    require_finite_number(raw_advantage, stage="advantage raw_advantage")
    scale = red_power + blue_power
    require_finite_number(scale, stage="advantage total power denominator")
    if scale <= 0:
        normalized_advantage = 0.0
    else:
        normalized_advantage = raw_advantage / scale
    require_finite_number(
        normalized_advantage,
        stage="advantage normalized_advantage",
    )

    # ── label ─────────────────────────────────────────────────────
    if raw_advantage > 0:
        label = "RED"
    elif raw_advantage < 0:
        label = "BLUE"
    else:
        label = "EVEN"

    # ── warnings ──────────────────────────────────────────────────
    warnings: list[str] = []
    for sa in (red, blue):
        if sa.coverage is not None and sa.coverage < 1.0:
            warnings.append("partial_side_coverage")
            break

    return AdvantageAnalysis(
        raw_advantage=raw_advantage,
        normalized_advantage=normalized_advantage,
        label=label,
        confidence=None,
        warnings=tuple(warnings),
    )
