"""Shared validation for wall-clock deadlines passed to Python threads."""

from __future__ import annotations

import math
from numbers import Real
from threading import TIMEOUT_MAX


def validated_timeout(
    value: float | None,
    *,
    name: str = "timeout_seconds",
    allow_none: bool = False,
) -> float | None:
    """Reject unbounded/invalid numeric deadlines before any state changes.

    ``None`` is the sole explicit unlimited-wait value when allowed. Positive
    infinity is not equivalent: ``Thread.join`` cannot accept it. The platform
    limit also prevents otherwise finite values from overflowing thread waits.
    """
    if value is None and allow_none:
        return None
    message = f"{name} must be finite, positive and <= {TIMEOUT_MAX}"
    if allow_none:
        message += ", or None"
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(message)  # noqa: TRY004 - existing timeout API uses ValueError.
    try:
        seconds = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(message) from error
    if not math.isfinite(seconds) or not 0 < seconds <= TIMEOUT_MAX:
        raise ValueError(message)
    return seconds
