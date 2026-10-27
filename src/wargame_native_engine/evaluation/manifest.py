"""Strict, portable experiment manifests for the model-independent evaluator."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wargame_native_engine.io.serialization import canonical_hash, file_sha256

MANIFEST_SCHEMA = "native-agent-benchmark/1.0"


@dataclass(frozen=True, slots=True)
class ScenarioCase:
    case_id: str
    path: Path
    sha256: str

    def __post_init__(self) -> None:
        _identifier(self.case_id, "scenario id")
        if not isinstance(self.path, Path):
            raise TypeError("scenario path must be a pathlib.Path")
        if not isinstance(self.sha256, str) or not re.fullmatch(
            r"[a-f0-9]{64}", self.sha256
        ):
            raise ValueError("scenario sha256 must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class BenchmarkManifest:
    """A validated experiment specification; paths are resolved at load time."""

    benchmark_id: str
    cases: tuple[ScenarioCase, ...]
    seeds: tuple[int, ...]
    sides: tuple[str, ...]
    max_decisions: int
    decision_interval_ms: int
    training_interface: str
    decision_budget_ms: int | None
    max_trace_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        _identifier(self.benchmark_id, "benchmark_id")
        for name in ("cases", "seeds", "sides"):
            if not isinstance(getattr(self, name), tuple) or not getattr(self, name):
                raise TypeError(f"{name} must be a nonempty tuple")
        if any(not isinstance(case, ScenarioCase) for case in self.cases):
            raise TypeError("cases must contain ScenarioCase values")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("scenario ids must be unique")
        if any(type(seed) is not int or not 0 <= seed < 2**63 for seed in self.seeds):
            raise ValueError("seeds must be exact integers in [0, 2**63)")
        if len(set(self.seeds)) != len(self.seeds):
            raise ValueError("seeds must be unique")
        if any(side not in ("red", "blue") for side in self.sides) or len(
            set(self.sides)
        ) != len(self.sides):
            raise ValueError("sides must be unique red/blue strings")
        if self.training_interface not in ("decision-v1", "decision-v2"):
            raise ValueError("unsupported training interface")
        for name in ("max_decisions", "decision_interval_ms", "max_trace_bytes"):
            _positive_int(getattr(self, name), name)
        if self.decision_budget_ms is not None:
            _positive_int(self.decision_budget_ms, "decision_budget_ms")

    def to_record(self) -> dict[str, Any]:
        # Source locations are deliberately excluded from experiment identity.
        return {
            "schema": MANIFEST_SCHEMA,
            "benchmark_id": self.benchmark_id,
            "scenarios": [
                {"id": case.case_id, "sha256": case.sha256} for case in self.cases
            ],
            "seeds": list(self.seeds),
            "sides": list(self.sides),
            "max_decisions": self.max_decisions,
            "decision_interval_ms": self.decision_interval_ms,
            "training_interface": self.training_interface,
            "decision_budget_ms": self.decision_budget_ms,
            "max_trace_bytes": self.max_trace_bytes,
            "opponent": "noop/1.0",
        }

    @property
    def semantic_hash(self) -> str:
        return canonical_hash(self.to_record())


def load_manifest(path: str | Path) -> BenchmarkManifest:
    """Reject misspelled options, changed input bytes and ambiguous duplicates."""
    source = Path(path).resolve()
    value = json.loads(
        source.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
    )
    _keys(
        value,
        {
            "schema",
            "benchmark_id",
            "scenarios",
            "seeds",
            "sides",
            "max_decisions",
            "decision_interval_ms",
            "training_interface",
            "decision_budget_ms",
            "max_trace_bytes",
        },
        required={
            "schema",
            "benchmark_id",
            "scenarios",
            "seeds",
            "sides",
            "max_decisions",
            "decision_interval_ms",
            "training_interface",
        },
    )
    if value["schema"] != MANIFEST_SCHEMA:
        raise ValueError("unsupported benchmark manifest schema")
    benchmark_id = _identifier(value["benchmark_id"], "benchmark_id")
    cases = []
    for row in _nonempty_list(value["scenarios"], "scenarios"):
        _keys(row, {"id", "path", "sha256"}, required={"id", "path", "sha256"})
        case_id = _identifier(row["id"], "scenario id")
        if not isinstance(row["path"], str) or not row["path"]:
            raise ValueError("scenario path must be nonempty text")
        scenario_path = (source.parent / row["path"]).resolve()
        sha256 = row["sha256"]
        if not isinstance(sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise ValueError("scenario sha256 must be lowercase SHA-256")
        if file_sha256(scenario_path) != sha256:
            raise ValueError(f"scenario input digest mismatch: {case_id}")
        cases.append(ScenarioCase(case_id, scenario_path, sha256))
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("scenario ids must be unique")
    seeds = tuple(_nonempty_list(value["seeds"], "seeds"))
    if any(type(seed) is not int or not 0 <= seed < 2**63 for seed in seeds):
        raise ValueError("seeds must be exact integers in [0, 2**63)")
    if len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be unique")
    sides = tuple(_nonempty_list(value["sides"], "sides"))
    if any(side not in ("red", "blue") for side in sides) or len(set(sides)) != len(
        sides
    ):
        raise ValueError("sides must be unique red/blue strings")
    interface = value["training_interface"]
    if interface not in ("decision-v1", "decision-v2"):
        raise ValueError("training_interface must be decision-v1 or decision-v2")
    budget = value.get("decision_budget_ms")
    if budget is not None:
        budget = _positive_int(budget, "decision_budget_ms")
    return BenchmarkManifest(
        benchmark_id,
        tuple(cases),
        seeds,
        sides,
        _positive_int(value["max_decisions"], "max_decisions"),
        _positive_int(value["decision_interval_ms"], "decision_interval_ms"),
        interface,
        budget,
        _positive_int(
            value.get("max_trace_bytes", 16 * 1024 * 1024), "max_trace_bytes"
        ),
    )


def _keys(value: Any, allowed: set[str], *, required: set[str]) -> None:
    if not isinstance(value, dict):
        raise TypeError("manifest objects must be JSON objects")
    if set(value) - allowed or required - set(value):
        raise ValueError("manifest fields are missing or unknown")


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value
    ):
        raise ValueError(f"{name} must be a short filesystem-safe identifier")
    return value


def _positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive exact integer")
    return value


def _nonempty_list(value: Any, name: str) -> list:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a nonempty JSON list")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value
