"""Demo-compatible eight-factor formulas.

These are **historical-replay-compatible Native analysis candidates v1**,
migrated from the benchmark-lab demo renderer.  They are NOT official
Source SDK rules and NOT NativeEngine adjudication rules.

Source: engine_renderer_qa_demo/engine/lanchester_scoring/combat_value.py
SHA-256: eea763c68ed1e07fcc4fb8bbdf39cf96cef3b20e3f31be697d9fe3950562cdcf
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from wargame_native_engine.analysis._numeric import (
    require_finite_number,
    to_finite_float,
)

# ── weapon weight table (exact copy from demo) ────────────────────────

WEAPON_WEIGHT: dict[int, float] = {
    1: 1.8,
    2: 1.9,
    3: 2.1,
    4: 1.5,
    5: 1.7,
    29: 0.8,
    35: 1.2,
    36: 2.5,
    37: 2.0,
    43: 0.9,
    54: 1.4,
    56: 1.6,
    69: 2.0,
    71: 1.6,
    72: 2.3,
    73: 2.7,
    74: 2.1,
    75: 1.7,
    76: 2.2,
    83: 2.1,
    84: 2.2,
    88: 1.8,
    89: 1.2,
}

# ── mobility state map (exact copy from demo) ─────────────────────────

_MOBILITY_STATE: dict[int, float] = {
    0: 1.0,
    1: 1.05,
    2: 1.1,
    3: 1.15,
    4: 0.9,
    5: 0.95,
}


# ── public factor functions ───────────────────────────────────────────


def health_factor(unit: Mapping[str, Any]) -> float | None:
    """Compute the health factor *H* from blood / max_blood.

    Returns ``None`` when either field is missing or ``None``-valued.
    Otherwise matches the demo semantics exactly.
    """

    if "blood" not in unit or "max_blood" not in unit:
        return None
    raw_blood = unit["blood"]
    raw_max = unit["max_blood"]
    if raw_blood is None or raw_max is None:
        return None
    blood = to_finite_float(raw_blood, stage="health_factor blood")
    max_blood = to_finite_float(raw_max, stage="health_factor max_blood")
    if blood <= 0:
        return _finite_result("health_factor result", 0.0)
    if max_blood <= 0:
        result = 1.0 if blood > 0 else 0.0
        return _finite_result("health_factor result", result)
    ratio = blood / max_blood
    require_finite_number(ratio, stage="health_factor ratio")
    ratio = max(0.0, min(1.0, ratio))
    if ratio <= 0:
        return _finite_result("health_factor result", 0.0)
    return _finite_result("health_factor result", 0.25 + 0.75 * ratio)


def base_value_factor(unit: Mapping[str, Any]) -> float | None:
    """Compute the base-value factor *V* from ``unit["value"]``.

    Returns ``None`` when the field is missing or ``None``-valued.
    Otherwise returns ``max(1.0, float(value))``.
    """

    if "value" not in unit:
        return None
    raw = unit["value"]
    if raw is None:
        return None
    numeric = to_finite_float(raw, stage="base_value_factor value")
    return _finite_result("base_value_factor result", max(1.0, numeric))


def weapon_factor(unit: Mapping[str, Any]) -> float:
    """Compute the weapon factor *W* from ``carry_weapon_ids``.

    Exact demo semantics: takes the maximum configured weight across
    carried weapons; defaults to 0.6 when no weapons are carried and
    1.0 for unknown weapon ids.
    """

    raw_weapons = unit.get("carry_weapon_ids")
    if isinstance(raw_weapons, bool):
        raise TypeError("weapon_factor carry_weapon_ids must not be bool")
    if isinstance(raw_weapons, (int, float)):
        require_finite_number(
            raw_weapons,
            stage="weapon_factor carry_weapon_ids",
        )
    weapons = raw_weapons or []
    if not weapons:
        return _finite_result("weapon_factor result", 0.6)
    weights = [
        WEAPON_WEIGHT.get(
            _as_int(w, stage="weapon_factor carry_weapon_ids item") or -1,
            1.0,
        )
        for w in weapons
    ]
    result = max(weights) if weights else 0.6
    return _finite_result("weapon_factor result", result)


def ammo_factor(unit: Mapping[str, Any]) -> float:
    """Compute the ammunition factor *A* from ``remain_bullet_nums``.

    Exact demo semantics: non-Mapping → 0.7; zero total → 0.3;
    otherwise logarithmic with an upper bound of 1.25.
    """

    raw_ammo = unit.get("remain_bullet_nums")
    if isinstance(raw_ammo, bool):
        raise TypeError("ammo_factor remain_bullet_nums must not be bool")
    ammo = raw_ammo or {}
    if not isinstance(ammo, dict):
        if isinstance(ammo, (int, float)):
            require_finite_number(
                ammo,
                stage="ammo_factor remain_bullet_nums",
            )
        return _finite_result("ammo_factor result", 0.7)
    total = 0.0
    for value in ammo.values():
        amount = _as_float(value, stage="ammo_factor ammunition value")
        total += max(0.0, amount)
        require_finite_number(total, stage="ammo_factor total")
    if total <= 0:
        return _finite_result("ammo_factor result", 0.3)
    result = min(1.25, 0.55 + math.log1p(total) / 5.0)
    return _finite_result("ammo_factor result", result)


def readiness_factor(unit: Mapping[str, Any]) -> float:
    """Compute the readiness factor *R* from cooldown and weapon state.

    Exact demo semantics: cooldown discount with a 0.55 floor and
    100/180 cap; unfold state 0 → 0.9, state 1 → 1.05.
    """

    factor = 1.0
    cool = _as_float(
        unit.get("weapon_cool_time"),
        0.0,
        stage="readiness_factor weapon_cool_time",
    )
    if cool > 0:
        factor *= max(0.55, 1.0 - min(cool, 100.0) / 180.0)
        require_finite_number(factor, stage="readiness_factor cooldown result")
    unfold_state = unit.get("weapon_unfold_state")
    _validate_numeric_code(
        unfold_state,
        stage="readiness_factor weapon_unfold_state",
    )
    if unfold_state == 0:
        factor *= 0.9
    elif unfold_state == 1:
        factor *= 1.05
    return _finite_result("readiness_factor result", factor)


def mobility_factor(unit: Mapping[str, Any]) -> float:
    """Compute the mobility factor *M* from movement state.

    Exact demo semantics: move_state lookup with 1.0 fallback;
    stop/can_to_move/on_board multiplicative modifiers.
    """

    state = unit.get("move_state")
    _validate_numeric_code(state, stage="mobility_factor move_state")
    state_factor = _MOBILITY_STATE.get(state, 1.0)
    if unit.get("stop") == 1:
        state_factor *= 0.95
        require_finite_number(state_factor, stage="mobility_factor stop result")
    if unit.get("can_to_move") is False:
        state_factor *= 0.85
        require_finite_number(
            state_factor,
            stage="mobility_factor can_to_move result",
        )
    if unit.get("on_board"):
        state_factor *= 0.9
    return _finite_result("mobility_factor result", state_factor)


def information_factor(unit: Mapping[str, Any]) -> float:
    """Compute the information factor *I* from visible enemy count.

    Exact demo semantics: 1.0 + 0.05 × len(seen); upper bound 1.25.
    """

    raw_seen = unit.get("see_enemy_bop_ids")
    if isinstance(raw_seen, bool):
        raise TypeError("information_factor see_enemy_bop_ids must not be bool")
    if isinstance(raw_seen, (int, float)):
        require_finite_number(
            raw_seen,
            stage="information_factor see_enemy_bop_ids",
        )
    seen = raw_seen or []
    result = min(1.25, 1.0 + 0.05 * len(seen))
    return _finite_result("information_factor result", result)


def status_factor(unit: Mapping[str, Any]) -> float:
    """Compute the status factor *S* from suppression, fatigue and control.

    Exact demo semantics: keep → 0.65; tire 1 → 0.9, tire≥2 → 0.75;
    lose_control → 0.2; close_combat → 1.05.
    """

    factor = 1.0
    if unit.get("keep"):
        factor *= 0.65
        require_finite_number(factor, stage="status_factor keep result")
    tire = _as_int(unit.get("tire"), stage="status_factor tire") or 0
    if tire == 1:
        factor *= 0.9
    elif tire >= 2:
        factor *= 0.75
    require_finite_number(factor, stage="status_factor tire result")
    if unit.get("lose_control"):
        factor *= 0.2
        require_finite_number(
            factor,
            stage="status_factor lose_control result",
        )
    if unit.get("close_combat"):
        factor *= 1.05
    return _finite_result("status_factor result", factor)


# ── internal helpers (replicate demo _as_float / _as_int) ─────────────


def _as_float(
    value: Any,
    default: float = 0.0,
    *,
    stage: str,
) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{stage} must be numeric (not bool)")
    try:
        numeric = float(value)
    except OverflowError as exc:
        raise ValueError(f"{stage} must be finite, got {value!r}") from exc
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        raise ValueError(f"{stage} must be finite, got {value!r}")
    return numeric


def _as_int(value: Any, *, stage: str) -> int | None:
    if isinstance(value, bool):
        raise TypeError(f"{stage} must be numeric (not bool)")
    try:
        return int(value)
    except OverflowError as exc:
        raise ValueError(f"{stage} must be finite, got {value!r}") from exc
    except (TypeError, ValueError):
        # Preserve the demo fallback for ordinary incompatible values, but
        # never let textual NaN/Infinity masquerade as an unknown code.
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(numeric):
            raise ValueError(f"{stage} must be finite, got {value!r}")
        return None


def _validate_numeric_code(value: Any, *, stage: str) -> None:
    """Validate numeric state codes without changing demo equality semantics."""

    if value is None:
        return
    if isinstance(value, bool):
        raise TypeError(f"{stage} must be numeric (not bool)")
    if isinstance(value, (int, float)):
        require_finite_number(value, stage=stage)


def _finite_result(stage: str, value: float) -> float:
    require_finite_number(value, stage=stage)
    return value
