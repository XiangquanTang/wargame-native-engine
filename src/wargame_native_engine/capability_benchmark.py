"""Data-only Native capability benchmark registry and traceability checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path

from wargame_native_engine.capability_registry import CapabilityRegistry
from wargame_native_engine.demo_registry import DemoRegistry


BENCHMARK_REGISTRY_SCHEMA = "native-capability-benchmark/v2"
LEGACY_BENCHMARK_REGISTRY_SCHEMA = "native-capability-benchmark/v1"


class CapabilityBenchmarkError(ValueError):
    """A benchmark contract or traceability edge is invalid."""


@dataclass(frozen=True, slots=True)
class BenchmarkValidation:
    engine_checks: tuple[str, ...]
    human_checks: tuple[str, ...]
    agent_checks: tuple[str, ...]
    replay_check: str
    tick_interval_ms: int
    capability_checks: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def checks_for(self, capability_id: str) -> tuple[str, ...]:
        return dict(self.capability_checks).get(capability_id, ())


@dataclass(frozen=True, slots=True)
class CapabilityBenchmarkDescriptor:
    benchmark_id: str
    capability_ids: tuple[str, ...]
    scenario: str
    required_features: tuple[str, ...]
    validation: BenchmarkValidation
    expected_events: tuple[str, ...]
    replay_required: bool
    human_required: bool
    agent_required: bool

    @property
    def capability(self) -> str:
        """Backward-compatible singular accessor for legacy benchmarks."""

        return self.capability_ids[0]

    @property
    def composite(self) -> bool:
        return len(self.capability_ids) > 1

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.benchmark_id,
            "capability_ids": list(self.capability_ids),
            "scenario": self.scenario,
            "required_features": list(self.required_features),
            "validation": {
                "engine_checks": list(self.validation.engine_checks),
                "human_checks": list(self.validation.human_checks),
                "agent_checks": list(self.validation.agent_checks),
                "replay_check": self.validation.replay_check,
                "tick_interval_ms": self.validation.tick_interval_ms,
                "capability_checks": {
                    capability_id: list(checks)
                    for capability_id, checks
                    in self.validation.capability_checks
                },
            },
            "expected_events": list(self.expected_events),
            "replay_required": self.replay_required,
            "human_required": self.human_required,
            "agent_required": self.agent_required,
        }


class CapabilityBenchmarkRegistry:
    """Validated deterministic benchmark-to-capability-to-demo catalog."""

    def __init__(
        self,
        repository_root: Path,
        descriptors: Sequence[CapabilityBenchmarkDescriptor],
    ) -> None:
        self.repository_root = repository_root.resolve()
        ordered = tuple(sorted(descriptors, key=lambda item: item.benchmark_id))
        if len({item.benchmark_id for item in ordered}) != len(ordered):
            raise CapabilityBenchmarkError("benchmark IDs must be unique")
        self._descriptors = ordered
        self._by_id = {item.benchmark_id: item for item in ordered}

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        repository_root: str | Path | None = None,
    ) -> "CapabilityBenchmarkRegistry":
        source = Path(path).resolve()
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CapabilityBenchmarkError(
                f"cannot load benchmark registry: {source}"
            ) from exc
        if not isinstance(value, Mapping):
            raise CapabilityBenchmarkError("benchmark registry must be an object")
        if value.get("schema") not in {
            BENCHMARK_REGISTRY_SCHEMA,
            LEGACY_BENCHMARK_REGISTRY_SCHEMA,
        }:
            raise CapabilityBenchmarkError("unsupported benchmark registry schema")
        raw = value.get("benchmarks")
        if not isinstance(raw, list):
            raise CapabilityBenchmarkError("benchmarks must be an array")
        descriptors = tuple(_descriptor(item) for item in raw)
        if tuple(item.benchmark_id for item in descriptors) != tuple(sorted(
            item.benchmark_id for item in descriptors
        )):
            raise CapabilityBenchmarkError(
                "benchmarks must be sorted by benchmark id"
            )
        root = (
            Path(repository_root).resolve()
            if repository_root is not None
            else source.parents[2]
        )
        return cls(root, descriptors)

    @property
    def descriptors(self) -> tuple[CapabilityBenchmarkDescriptor, ...]:
        return self._descriptors

    def resolve(self, benchmark_id: str) -> CapabilityBenchmarkDescriptor:
        try:
            return self._by_id[str(benchmark_id)]
        except KeyError as exc:
            raise CapabilityBenchmarkError(
                f"unknown benchmark: {benchmark_id}"
            ) from exc

    def validate_traceability(
        self,
        capabilities: CapabilityRegistry,
        demos: DemoRegistry,
    ) -> None:
        for benchmark in self.descriptors:
            demo = demos.resolve(benchmark.scenario)
            if benchmark.composite:
                if tuple(benchmark.capability_ids) != tuple(demo.capability_ids):
                    raise CapabilityBenchmarkError(
                        f"{benchmark.benchmark_id} capability set does not match demo"
                    )
            for capability_id in benchmark.capability_ids:
                capability = capabilities.resolve(capability_id)
                if not benchmark.composite and capability.demo_id != demo.demo_id:
                    raise CapabilityBenchmarkError(
                        f"{benchmark.benchmark_id} does not use capability demo"
                    )
                if capability.engine_status == "NOT_IMPLEMENTED":
                    raise CapabilityBenchmarkError(
                        f"{capability_id} is not implemented"
                    )
                if benchmark.replay_required and not capability.replay_support:
                    raise CapabilityBenchmarkError("Replay support mismatch")
                if benchmark.human_required and not capability.human_support:
                    raise CapabilityBenchmarkError("Human support mismatch")
                if benchmark.agent_required and not capability.agent_support:
                    raise CapabilityBenchmarkError("Agent support mismatch")
            if not set(benchmark.validation.engine_checks):
                raise CapabilityBenchmarkError("engine validation cannot be empty")
            if benchmark.composite and set(
                capability_id for capability_id, _checks
                in benchmark.validation.capability_checks
            ) != set(benchmark.capability_ids):
                raise CapabilityBenchmarkError(
                    "composite benchmark requires checks for every capability"
                )


def load_default_benchmark_registry() -> CapabilityBenchmarkRegistry:
    root = Path(__file__).resolve().parents[2]
    return CapabilityBenchmarkRegistry.load(
        root / "config/benchmarks/native_capability_benchmark_v1.json",
        repository_root=root,
    )


def _descriptor(value: object) -> CapabilityBenchmarkDescriptor:
    if not isinstance(value, Mapping):
        raise CapabilityBenchmarkError("benchmark descriptor must be an object")
    features = _sorted_strings(value.get("required_features"), "required_features")
    events = _unique_strings(value.get("expected_events"), "expected_events")
    validation = value.get("validation")
    if not isinstance(validation, Mapping):
        raise CapabilityBenchmarkError("validation must be an object")
    interval = validation.get("tick_interval_ms")
    if type(interval) is not int or interval <= 0:
        raise CapabilityBenchmarkError("tick_interval_ms must be positive")
    capability_ids = _capability_ids(value)
    raw_capability_checks = validation.get("capability_checks", {})
    if not isinstance(raw_capability_checks, Mapping):
        raise CapabilityBenchmarkError("capability_checks must be an object")
    capability_checks = tuple(
        (
            _text(capability_id, "capability_checks key"),
            _unique_strings(checks, f"capability_checks.{capability_id}"),
        )
        for capability_id, checks in sorted(raw_capability_checks.items())
    )
    return CapabilityBenchmarkDescriptor(
        benchmark_id=_text(value.get("id"), "id"),
        capability_ids=capability_ids,
        scenario=_text(value.get("scenario"), "scenario"),
        required_features=features,
        validation=BenchmarkValidation(
            engine_checks=_unique_strings(
                validation.get("engine_checks"), "engine_checks"
            ),
            human_checks=_unique_strings(
                validation.get("human_checks"), "human_checks"
            ),
            agent_checks=_unique_strings(
                validation.get("agent_checks"), "agent_checks"
            ),
            replay_check=_text(validation.get("replay_check"), "replay_check"),
            tick_interval_ms=interval,
            capability_checks=capability_checks,
        ),
        expected_events=events,
        replay_required=_bool(value.get("replay_required"), "replay_required"),
        human_required=_bool(value.get("human_required"), "human_required"),
        agent_required=_bool(value.get("agent_required"), "agent_required"),
    )


def _capability_ids(value: Mapping[str, object]) -> tuple[str, ...]:
    singular = value.get("capability")
    plural = value.get("capability_ids")
    if singular is not None and plural is not None:
        raise CapabilityBenchmarkError(
            "benchmark cannot define capability and capability_ids"
        )
    if plural is not None:
        if not isinstance(plural, list):
            raise CapabilityBenchmarkError("capability_ids must be an array")
        result = tuple(_text(item, "capability_id") for item in plural)
    else:
        result = (_text(singular, "capability"),)
    if len(result) != len(set(result)):
        raise CapabilityBenchmarkError("capability_ids must be unique")
    if result != tuple(sorted(result)):
        raise CapabilityBenchmarkError(
            "capability_ids must be deterministically sorted"
        )
    return result


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityBenchmarkError(f"{field} must be non-empty text")
    return value.strip()


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise CapabilityBenchmarkError(f"{field} must be boolean")
    return value


def _unique_strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise CapabilityBenchmarkError(f"{field} must contain strings")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise CapabilityBenchmarkError(f"{field} must be unique")
    return result


def _sorted_strings(value: object, field: str) -> tuple[str, ...]:
    result = _unique_strings(value, field)
    if result != tuple(sorted(result)):
        raise CapabilityBenchmarkError(f"{field} must be sorted")
    return result


__all__ = [
    "BENCHMARK_REGISTRY_SCHEMA",
    "BenchmarkValidation",
    "CapabilityBenchmarkDescriptor",
    "CapabilityBenchmarkError",
    "CapabilityBenchmarkRegistry",
    "load_default_benchmark_registry",
]
