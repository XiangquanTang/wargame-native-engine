from __future__ import annotations

import math
import statistics
from typing import Any, Iterable, Mapping


def native_advantage_curve(frames: Iterable[Mapping[str, Any]]) -> tuple[
        tuple[float, float], ...]:
    values: list[tuple[float, float]] = []
    for frame in frames:
        health = {"red": 0.0, "blue": 0.0}
        for operator in frame.get("operators", []):
            side = str(operator["side"])
            if side in health:
                health[side] += max(0.0, float(operator.get("health", 0.0)))
        total = health["red"] + health["blue"]
        advantage = 0.0 if total == 0 else (
            health["red"] - health["blue"]) / total
        values.append((float(frame["historical_time"]), advantage))
    return tuple(values)


def compare_advantage_curves(historical: Iterable[tuple[float, float]],
                             native: Iterable[tuple[float, float]]) -> Mapping[str, Any]:
    historical_values = tuple(historical)
    native_by_time = {float(time): float(value) for time, value in native}
    pairs = [(time, value, native_by_time[time])
             for time, value in historical_values if time in native_by_time]
    if not pairs:
        return {"supported": False, "status": "not_comparable",
                "reason": "no_overlapping_advantage_times",
                "curve_direction_consistency": None,
                "leading_side_consistency": None, "peak_time_error": None,
                "final_advantage_error": None, "mean_absolute_error": None,
                "normalized_curve_error": None}
    errors = [abs(left - right) for _, left, right in pairs]
    historical_directions = [_sign(pairs[index][1] - pairs[index - 1][1])
                             for index in range(1, len(pairs))]
    native_directions = [_sign(pairs[index][2] - pairs[index - 1][2])
                         for index in range(1, len(pairs))]
    direction = (None if not historical_directions else
                 sum(left == right for left, right in zip(
                     historical_directions, native_directions))
                 / len(historical_directions))
    leader = sum(_sign(left) == _sign(right) for _, left, right in pairs) / len(pairs)
    historical_peak = max(pairs, key=lambda item: (abs(item[1]), -item[0]))[0]
    native_peak = max(pairs, key=lambda item: (abs(item[2]), -item[0]))[0]
    scale = max((abs(value) for _, value, _ in pairs), default=0.0)
    normalized = statistics.fmean(errors) / max(scale, 1e-12)
    return {"supported": True, "status": "comparable",
            "curve_direction_consistency": direction,
            "leading_side_consistency": leader,
            "peak_time_error": native_peak - historical_peak,
            "final_advantage_error": pairs[-1][2] - pairs[-1][1],
            "mean_absolute_error": statistics.fmean(errors),
            "normalized_curve_error": normalized,
            "point_count": len(pairs),
            "formula": "sum_health_red_minus_blue_normalized"}


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0
