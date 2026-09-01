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
from wargame_native_engine.analysis.semantic_identity import (
    AMMO_CAP,
    AMMO_LOG_DIVISOR,
    AMMO_LOG_INTERCEPT,
    AMMO_NON_MAPPING_VALUE,
    AMMO_ZERO_TOTAL_VALUE,
    BASE_VALUE_FLOOR,
    HEALTH_ALIVE_FLOOR,
    HEALTH_ALIVE_SPAN,
    HEALTH_DESTROYED_THRESHOLD,
    HEALTH_NONPOSITIVE_MAX_ALIVE_VALUE,
    HEALTH_RATIO_MAX,
    HEALTH_RATIO_MIN,
    INFORMATION_BASE,
    INFORMATION_CAP,
    INFORMATION_PER_CONTACT,
    MOBILITY_CANNOT_MOVE_MULTIPLIER,
    MOBILITY_DEFAULT,
    MOBILITY_ON_BOARD_MULTIPLIER,
    MOBILITY_STATE,
    MOBILITY_STOP_MULTIPLIER,
    READINESS_BASE,
    READINESS_COOLDOWN_CAP,
    READINESS_COOLDOWN_DIVISOR,
    READINESS_COOLDOWN_FLOOR,
    READINESS_FOLDED_MULTIPLIER,
    READINESS_UNFOLDED_MULTIPLIER,
    STATUS_BASE,
    STATUS_CLOSE_COMBAT_MULTIPLIER,
    STATUS_FATIGUE_LEVEL_ONE_MULTIPLIER,
    STATUS_FATIGUE_LEVEL_TWO_PLUS_MULTIPLIER,
    STATUS_LOSE_CONTROL_MULTIPLIER,
    STATUS_SUPPRESSED_MULTIPLIER,
    WEAPON_EMPTY_WEIGHT,
    WEAPON_UNKNOWN_WEIGHT,
    WEAPON_WEIGHT,
)


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
    if blood <= HEALTH_DESTROYED_THRESHOLD:
        return _finite_result("health_factor result", 0.0)
    if max_blood <= 0:
        result = (
            HEALTH_NONPOSITIVE_MAX_ALIVE_VALUE
            if blood > HEALTH_DESTROYED_THRESHOLD else 0.0
        )
        return _finite_result("health_factor result", result)
    ratio = blood / max_blood
    require_finite_number(ratio, stage="health_factor ratio")
    ratio = max(HEALTH_RATIO_MIN, min(HEALTH_RATIO_MAX, ratio))
    if ratio <= HEALTH_RATIO_MIN:
        return _finite_result("health_factor result", 0.0)
    return _finite_result(
        "health_factor result", HEALTH_ALIVE_FLOOR + HEALTH_ALIVE_SPAN * ratio
    )


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
    return _finite_result(
        "base_value_factor result", max(BASE_VALUE_FLOOR, numeric)
    )


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
        return _finite_result("weapon_factor result", WEAPON_EMPTY_WEIGHT)
    weights = [
        WEAPON_WEIGHT.get(
            _as_int(w, stage="weapon_factor carry_weapon_ids item") or -1,
            WEAPON_UNKNOWN_WEIGHT,
        )
        for w in weapons
    ]
    result = max(weights) if weights else WEAPON_EMPTY_WEIGHT
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
        return _finite_result("ammo_factor result", AMMO_NON_MAPPING_VALUE)
    total = 0.0
    for value in ammo.values():
        amount = _as_float(value, stage="ammo_factor ammunition value")
        total += max(0.0, amount)
        require_finite_number(total, stage="ammo_factor total")
    if total <= 0:
        return _finite_result("ammo_factor result", AMMO_ZERO_TOTAL_VALUE)
    result = min(
        AMMO_CAP,
        AMMO_LOG_INTERCEPT + math.log1p(total) / AMMO_LOG_DIVISOR,
    )
    return _finite_result("ammo_factor result", result)


def readiness_factor(unit: Mapping[str, Any]) -> float:
    """Compute the readiness factor *R* from cooldown and weapon state.

    Exact demo semantics: cooldown discount with a 0.55 floor and
    100/180 cap; unfold state 0 → 0.9, state 1 → 1.05.
    """

    factor = READINESS_BASE
    cool = _as_float(
        unit.get("weapon_cool_time"),
        0.0,
        stage="readiness_factor weapon_cool_time",
    )
    if cool > 0:
        factor *= max(
            READINESS_COOLDOWN_FLOOR,
            READINESS_BASE
            - min(cool, READINESS_COOLDOWN_CAP) / READINESS_COOLDOWN_DIVISOR,
        )
        require_finite_number(factor, stage="readiness_factor cooldown result")
    unfold_state = unit.get("weapon_unfold_state")
    _validate_numeric_code(
        unfold_state,
        stage="readiness_factor weapon_unfold_state",
    )
    if unfold_state == 0:
        factor *= READINESS_FOLDED_MULTIPLIER
    elif unfold_state == 1:
        factor *= READINESS_UNFOLDED_MULTIPLIER
    return _finite_result("readiness_factor result", factor)


def mobility_factor(unit: Mapping[str, Any]) -> float:
    """Compute the mobility factor *M* from movement state.

    Exact demo semantics: move_state lookup with 1.0 fallback;
    stop/can_to_move/on_board multiplicative modifiers.
    """

    state = unit.get("move_state")
    _validate_numeric_code(state, stage="mobility_factor move_state")
    state_factor = MOBILITY_STATE.get(state, MOBILITY_DEFAULT)
    if unit.get("stop") == 1:
        state_factor *= MOBILITY_STOP_MULTIPLIER
        require_finite_number(state_factor, stage="mobility_factor stop result")
    if unit.get("can_to_move") is False:
        state_factor *= MOBILITY_CANNOT_MOVE_MULTIPLIER
        require_finite_number(
            state_factor,
            stage="mobility_factor can_to_move result",
        )
    if unit.get("on_board"):
        state_factor *= MOBILITY_ON_BOARD_MULTIPLIER
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
    result = min(
        INFORMATION_CAP,
        INFORMATION_BASE + INFORMATION_PER_CONTACT * len(seen),
    )
    return _finite_result("information_factor result", result)


def status_factor(unit: Mapping[str, Any]) -> float:
    """Compute the status factor *S* from suppression, fatigue and control.

    Exact demo semantics: keep → 0.65; tire 1 → 0.9, tire≥2 → 0.75;
    lose_control → 0.2; close_combat → 1.05.
    """

    factor = STATUS_BASE
    if unit.get("keep"):
        factor *= STATUS_SUPPRESSED_MULTIPLIER
        require_finite_number(factor, stage="status_factor keep result")
    tire = _as_int(unit.get("tire"), stage="status_factor tire") or 0
    if tire == 1:
        factor *= STATUS_FATIGUE_LEVEL_ONE_MULTIPLIER
    elif tire >= 2:
        factor *= STATUS_FATIGUE_LEVEL_TWO_PLUS_MULTIPLIER
    require_finite_number(factor, stage="status_factor tire result")
    if unit.get("lose_control"):
        factor *= STATUS_LOSE_CONTROL_MULTIPLIER
        require_finite_number(
            factor,
            stage="status_factor lose_control result",
        )
    if unit.get("close_combat"):
        factor *= STATUS_CLOSE_COMBAT_MULTIPLIER
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
