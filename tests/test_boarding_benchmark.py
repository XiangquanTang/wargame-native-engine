from __future__ import annotations

import json
from pathlib import Path

from scripts.run_benchmark import run_capability_benchmark
from wargame_native_engine.capability_benchmark import (
    load_default_benchmark_registry,
)
from wargame_native_engine.capability_registry import (
    load_default_capability_registry,
)
from wargame_native_engine.demo_registry import load_default_demo_registry


def test_boarding_registry_and_benchmark_traceability() -> None:
    capabilities = load_default_capability_registry()
    benchmarks = load_default_benchmark_registry()
    demos = load_default_demo_registry()

    boarding = capabilities.resolve("boarding")
    benchmark = benchmarks.resolve("boarding_basic")
    assert boarding.engine_status == "IMPLEMENTED"
    assert boarding.demo_status == "PASS"
    assert boarding.human_support is True
    assert boarding.agent_support is True
    assert boarding.replay_support is True
    assert benchmark.scenario == "boarding_basic"
    benchmarks.validate_traceability(capabilities, demos)


def test_boarding_benchmark_validates_full_capability_path(
    tmp_path: Path,
) -> None:
    output = tmp_path / "boarding-benchmark"
    report = run_capability_benchmark(
        "boarding_basic",
        output_dir=output,
    )

    assert report["passed"] is True
    assert report["engine_result"]["passed"] is True
    assert report["human_result"]["result"] == "PASS"
    assert report["human_result"]["operation_types"] == ["BOARD", "UNLOAD"]
    assert report["agent_result"]["result"] == "PASS"
    assert report["agent_result"]["valid_action_visible"] is True
    assert report["replay_result"]["matches"] is True
    assert report["replay_result"]["world_hash_matches"] is True
    assert report["event_trace"] == [
        "BOARD_STARTED",
        "BOARD_COMPLETED",
        "UNLOAD_STARTED",
        "UNLOAD_COMPLETED",
    ]
    assert isinstance(report["world_hash"], str)
    assert isinstance(report["rng_hash"], str)
    assert (output / "benchmark_report.json").is_file()
    assert (output / "event_trace.json").is_file()
    assert (output / "demo_output/replay.zip").is_file()
    assert json.loads(
        (output / "benchmark_report.json").read_text(encoding="utf-8")
    )["passed"] is True


def test_existing_fire_benchmarks_remain_registered() -> None:
    registry = load_default_benchmark_registry()
    assert registry.resolve("direct_fire_basic").capability == "direct_fire"
    assert registry.resolve("guided_fire_basic").capability == "guided_fire"
    assert (
        registry.resolve("indirect_fire_full_cycle").capability
        == "indirect_fire"
    )
