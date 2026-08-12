"""Small numeric-contract helpers shared by pure analysis calculations."""

from __future__ import annotations

import math
from typing import Any


def require_finite_number(value: Any, *, stage: str) -> int | float:
    """Return a real ``int``/``float`` after rejecting bool/non-finite values."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"{stage} must be an int or float (not bool), "
            f"got {type(value).__name__}"
        )
    try:
        finite = math.isfinite(value)
    except OverflowError as exc:
        raise ValueError(f"{stage} must be finite, got {value!r}") from exc
    if not finite:
        raise ValueError(f"{stage} must be finite, got {value!r}")
    return value


def to_finite_float(value: Any, *, stage: str) -> float:
    """Apply the existing float conversion contract, then require finiteness."""

    if isinstance(value, bool):
        raise TypeError(f"{stage} must be numeric (not bool)")
    try:
        numeric = float(value)
    except OverflowError as exc:
        raise ValueError(f"{stage} must be finite, got {value!r}") from exc
    except (TypeError, ValueError) as exc:
        raise type(exc)(f"{stage} must be numeric, got {value!r}") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{stage} must be finite, got {value!r}")
    return numeric
