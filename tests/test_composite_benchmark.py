from __future__ import annotations

from pathlib import Path

from scripts.run_benchmark import run_capability_benchmark
from wargame_native_engine.capability_benchmark import (
    load_default_benchmark_registry,
)


CAPABILITIES = (
    "boarding",
    "direct_fire",
    "objective_control",
    "visibility",
)


def test_composite_benchmark_descriptor_exposes_capability_set() -> None:
    descriptor = load_default_benchmark_registry().resolve(
        "mechanized_assault_basic"
    )

    assert descriptor.composite is True
    assert descriptor.capability_ids == CAPABILITIES
    assert tuple(
        capability_id
        for capability_id, _checks
        in descriptor.validation.capability_checks
    ) == CAPABILITIES


def test_composite_benchmark_reports_each_capability_and_reexecution(
    tmp_path: Path,
) -> None:
    output = tmp_path / "composite-benchmark"
    report = run_capability_benchmark(
        "mechanized_assault_basic",
        output_dir=output,
    )

    assert report["passed"] is True
    assert report["capability"] == "composite"
    assert report["capability_ids"] == list(CAPABILITIES)
    assert set(report["capability_results"]) == set(CAPABILITIES)
    assert all(
        value["passed"] is True
        for value in report["capability_results"].values()
    )
    assert report["engine_result"]["passed"] is True
    assert report["human_result"]["result"] == "PASS"
    assert report["agent_result"]["result"] == "PASS"
    assert report["replay_result"] == {
        "strict": True,
        "matches": True,
        "world_hash_matches": True,
        "result": "PASS",
    }
    assert report["expected_events_match"] is True
    assert isinstance(report["world_hash"], str)
    assert isinstance(report["rng_hash"], str)
    assert (output / "benchmark_report.json").is_file()
    assert (output / "event_trace.json").is_file()
