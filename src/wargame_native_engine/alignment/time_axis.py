from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class TimeAlignmentConfig:
    strategy: str
    scale: float
    offset: float
    tolerance: float = 1e-9

    def __post_init__(self) -> None:
        if self.strategy not in {"exact", "nearest", "fixed_ratio", "resampled"}:
            raise ValueError("unsupported time alignment strategy")
        if self.scale <= 0 or self.tolerance < 0:
            raise ValueError("time alignment scale must be positive")


@dataclass(frozen=True, slots=True)
class TimePair:
    historical_time: float
    target_native_tick: float
    native_tick: float | None
    error: float | None
    status: str

    def to_dict(self) -> dict[str, float | str | None]:
        return {"historical_time": self.historical_time,
                "target_native_tick": self.target_native_tick,
                "native_tick": self.native_tick, "error": self.error,
                "status": self.status}


def align_times(historical_times: Iterable[float], native_ticks: Iterable[float],
                config: TimeAlignmentConfig) -> tuple[TimePair, ...]:
    native = tuple(sorted(set(float(item) for item in native_ticks)))
    result: list[TimePair] = []
    for historical in sorted(set(float(item) for item in historical_times)):
        target = historical * config.scale + config.offset
        if not native:
            result.append(TimePair(historical, target, None, None, "UNMATCHED"))
            continue
        nearest = min(native, key=lambda item: (abs(item - target), item))
        error = nearest - target
        if config.strategy == "exact":
            status = "EXACT" if abs(error) <= config.tolerance else "UNMATCHED"
            result.append(TimePair(historical, target,
                                   nearest if status == "EXACT" else None,
                                   error if status == "EXACT" else None, status))
        elif config.strategy == "nearest":
            result.append(TimePair(historical, target, nearest, error, "NEAREST"))
        elif config.strategy == "fixed_ratio":
            status = "EXACT" if abs(error) <= config.tolerance else "FIXED_RATIO_NEAREST"
            result.append(TimePair(historical, target, nearest, error, status))
        else:
            status = "EXACT" if abs(error) <= config.tolerance else "RESAMPLED"
            result.append(TimePair(historical, target, nearest, error, status))
    return tuple(result)
