"""Exact simulation-time boundary helpers.

The authoritative engine clock is integer milliseconds. Seconds remain a
compatibility input only and are converted before entering the formal step
chain.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def require_positive_milliseconds(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("delta_time_ms must be a positive exact int")
    return value


def seconds_to_milliseconds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("delta_time_seconds must be a positive number")
    try:
        milliseconds = Decimal(str(value)) * Decimal(1000)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("delta_time_seconds must be numeric") from exc
    if milliseconds != milliseconds.to_integral_value() or milliseconds <= 0:
        raise ValueError(
            "delta_time_seconds must resolve to positive integer milliseconds"
        )
    return int(milliseconds)


def resolve_delta_time_ms(
    *,
    delta_time_seconds: object | None,
    delta_time_ms: object | None,
    default_ms: int = 1_000,
) -> int:
    """Resolve mutually exclusive compatibility/exact time inputs."""

    if delta_time_seconds is not None and delta_time_ms is not None:
        raise ValueError("provide delta_time_ms or delta_time_seconds, not both")
    if delta_time_ms is not None:
        return require_positive_milliseconds(delta_time_ms)
    if delta_time_seconds is not None:
        return seconds_to_milliseconds(delta_time_seconds)
    return require_positive_milliseconds(default_ms)
