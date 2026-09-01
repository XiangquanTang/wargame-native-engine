from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_benchmark import main, run_capability_benchmark
from wargame_native_engine.capability_benchmark import (
    BENCHMARK_REGISTRY_SCHEMA,
    load_default_benchmark_registry,
)
from wargame_native_engine.capability_registry import (
    load_default_capability_registry,
)
from wargame_native_engine.demo_registry import load_default_demo_registry


BENCHMARK_IDS = (
    "boarding_basic",
    "direct_fire_basic",
    "drone_basic",
    "guided_fire_basic",
    "indirect_fire_full_cycle",
    "loitering_munition_basic",
    "mechanized_assault_basic",
    "objective_control",
    "visibility_blocking",
)


@pytest.fixture(scope="module")
def benchmark_reports(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, dict[str, object]]:
    root = tmp_path_factory.mktemp("capability-benchmarks")
    return {
        benchmark_id: run_capability_benchmark(
            benchmark_id,
            output_dir=root / benchmark_id,
        )
        for benchmark_id in BENCHMARK_IDS
    }


def test_registry_schema_and_nine_benchmarks_exist() -> None:
    payload = json.loads(
        Path("config/benchmarks/native_capability_benchmark_v1.json").read_text(
            encoding="utf-8"
        )
    )
    registry = load_default_benchmark_registry()

    assert payload["schema"] == BENCHMARK_REGISTRY_SCHEMA
    assert tuple(item.benchmark_id for item in registry.descriptors) == BENCHMARK_IDS
    assert {item.capability for item in registry.descriptors} == {
        "boarding",
        "direct_fire",
        "drone",
        "guided_fire",
        "indirect_fire",
        "loitering_munition",
        "objective_control",
        "visibility",
    }


def test_demo_mapping_replay_and_event_contracts_are_traceable() -> None:
    registry = load_default_benchmark_registry()
    registry.validate_traceability(
        load_default_capability_registry(),
        load_default_demo_registry(),
    )

    for descriptor in registry.descriptors:
        assert descriptor.replay_required is True
        assert descriptor.human_required is True
        assert descriptor.agent_required is True
        assert descriptor.validation.replay_check == "strict_replay"
    assert registry.resolve("direct_fire_basic").expected_events == (
        "DIRECT_FIRE_REQUESTED",
        "DIRECT_FIRE_VALIDATED",
        "LOSSES_APPLIED",
    )
    assert registry.resolve("guided_fire_basic").expected_events == (
        "DIRECT_FIRE_VALIDATED",
        "GUIDED_FIRE_RESOLVED",
    )
    assert registry.resolve("boarding_basic").expected_events == (
        "BOARD_STARTED",
        "BOARD_COMPLETED",
        "UNLOAD_STARTED",
        "UNLOAD_COMPLETED",
    )
    assert registry.resolve("indirect_fire_full_cycle").expected_events == (
        "INDIRECT_FIRE_MISSION_CREATED",
        "INDIRECT_FIRE_ACTIVE_STARTED",
        "INDIRECT_FIRE_AREA_TRIGGERED",
        "INDIRECT_FIRE_RESOLVED",
    )


def test_all_nine_benchmarks_validate_engine_human_agent_and_replay(
    benchmark_reports: dict[str, dict[str, object]],
) -> None:
    for benchmark_id in BENCHMARK_IDS:
        report = benchmark_reports[benchmark_id]
        assert report["passed"] is True
        assert report["engine_result"]["passed"] is True
        assert report["human_result"]["result"] == "PASS"
        assert report["human_result"]["executed"] is True
        assert report["human_validation"] == report["human_result"]
        assert report["agent_result"]["result"] == "PASS"
        assert report["agent_result"]["valid_action_visible"] is True
        assert report["agent_validation"] == report["agent_result"]
        assert report["replay_result"]["matches"] is True
        assert report["replay_result"]["world_hash_matches"] is True
        assert isinstance(report["world_hash"], str)
        assert isinstance(report["rng_hash"], str)


def test_indirect_event_trace_is_complete_and_ordered(
    benchmark_reports: dict[str, dict[str, object]],
) -> None:
    report = benchmark_reports["indirect_fire_full_cycle"]

    assert report["expected_events_match"] is True
    assert report["event_trace"] == [
        "INDIRECT_FIRE_MISSION_CREATED",
        "INDIRECT_FIRE_ACTIVE_STARTED",
        "INDIRECT_FIRE_AREA_TRIGGERED",
        "INDIRECT_FIRE_RESOLVED",
    ]


def test_world_and_rng_hashes_are_deterministic(
    benchmark_reports: dict[str, dict[str, object]],
    tmp_path: Path,
) -> None:
    first = benchmark_reports["indirect_fire_full_cycle"]
    second = run_capability_benchmark(
        "indirect_fire_full_cycle",
        output_dir=tmp_path / "repeat",
    )

    assert second["passed"] is True
    assert second["world_hash"] == first["world_hash"]
    assert second["rng_hash"] == first["rng_hash"]
    assert second["event_trace"] == first["event_trace"]


def test_runner_writes_explicit_failure_report(tmp_path: Path) -> None:
    def fail_runner(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError("injected benchmark failure")

    output = tmp_path / "failure"
    report = run_capability_benchmark(
        "direct_fire_basic",
        output_dir=output,
        demo_runner=fail_runner,
    )
    persisted = json.loads(
        (output / "benchmark_report.json").read_text(encoding="utf-8")
    )

    assert report["passed"] is False
    assert report["error"] == {
        "type": "RuntimeError",
        "message": "injected benchmark failure",
    }
    assert persisted == report
    assert json.loads(
        (output / "event_trace.json").read_text(encoding="utf-8")
    )["events"] == []


def test_cli_list_and_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list"]) == 0
    listed = capsys.readouterr().out
    assert "Native Engine Capability Benchmark" in listed
    assert "indirect_fire_full_cycle" in listed

    assert main(["check"]) == 0
    checked = capsys.readouterr().out
    assert "Benchmark traceability: PASS" in checked
