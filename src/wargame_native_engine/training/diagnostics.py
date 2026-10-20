"""Bounded, opt-in diagnostics for the Native training path.

These collectors are observer-only.  They never enter WorldState, replay,
reward, catalog, RNG, or checkpoint identities.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import time
from typing import Iterator, Mapping


TRAINING_TIMING_SCHEMA_VERSION = "native-training-aggregate-timing/1.0"


@dataclass(slots=True)
class _TimingValue:
    count: int = 0
    total_ns: int = 0
    max_ns: int = 0


class TrainingTimingCollector:
    """Low-overhead aggregate count/total/max monotonic timings.

    The disabled path performs no clock read.  Enabled measurements are
    inclusive unless the exported boundary description says otherwise; callers
    must not sum nested stages.
    """

    __slots__ = ("enabled", "_values")

    def __init__(self, *, enabled: bool = False) -> None:
        if type(enabled) is not bool:
            raise TypeError("enabled must be an exact bool")
        self.enabled = enabled
        self._values: dict[str, _TimingValue] = {}

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        if not isinstance(stage, str) or not stage or stage != stage.strip():
            raise ValueError("timing stage must be non-empty trimmed text")
        started = time.perf_counter_ns()
        try:
            yield
        finally:
            elapsed = time.perf_counter_ns() - started
            value = self._values.setdefault(stage, _TimingValue())
            value.count += 1
            value.total_ns += elapsed
            value.max_ns = max(value.max_ns, elapsed)

    def snapshot(self) -> Mapping[str, object]:
        stages = {
            name: {
                "count": value.count,
                "total_seconds": value.total_ns / 1_000_000_000.0,
                "max_seconds": value.max_ns / 1_000_000_000.0,
            }
            for name, value in sorted(self._values.items())
        }
        return {
            "schema_version": TRAINING_TIMING_SCHEMA_VERSION,
            "enabled": self.enabled,
            "clock": "perf_counter_ns",
            "aggregation": "count_total_max",
            "bounded": True,
            "stages": stages,
            "boundary_notes": {
                "runner.reset_episode": "inclusive of env reset and initial candidate preparation",
                "runner.env_step": "inclusive NativeTrainingEnv.step_bound_action",
                "env.step_total": "inclusive; contains projection, submit/advance, reward and bind stages",
                "env.submit_and_advance": "both side submissions; second submission performs Engine advance and recording",
                "env.provider_prepare": "candidate planning/authorization only; nested in reset or step bind",
                "runner.episode_finish": "reward verification plus replay publication when artifacts are enabled",
                "residual": "derive only from an enclosing wall interval minus direct non-overlapping children",
            },
        }

    def clear(self) -> None:
        """Discard aggregate samples at an explicit diagnostic boundary."""

        self._values.clear()

