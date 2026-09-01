#!/usr/bin/env python3
"""Run deterministic Native capability benchmarks over public demos."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import json
from pathlib import Path
from typing import Any

if __package__:
    from scripts.run_demo import run_registered_demo
else:  # direct ``python scripts/run_benchmark.py`` entry point
    from run_demo import run_registered_demo
from wargame_native_engine.capability_benchmark import (
    CapabilityBenchmarkDescriptor,
    CapabilityBenchmarkError,
    CapabilityBenchmarkRegistry,
    load_default_benchmark_registry,
)
from wargame_native_engine.capability_registry import (
    CapabilityRegistry,
    load_default_capability_registry,
)
from wargame_native_engine.core.action import EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.demo_registry import (
    DemoRegistry,
    load_default_demo_registry,
)
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.serialization import canonical_hash


DemoRunner = Callable[..., Mapping[str, object]]


def run_capability_benchmark(
    benchmark_id: str,
    *,
    registry: CapabilityBenchmarkRegistry | None = None,
    capabilities: CapabilityRegistry | None = None,
    demos: DemoRegistry | None = None,
    output_dir: Path | None = None,
    seed: int | None = None,
    overwrite: bool = False,
    demo_runner: DemoRunner = run_registered_demo,
) -> dict[str, object]:
    benchmarks = registry or load_default_benchmark_registry()
    capability_registry = capabilities or load_default_capability_registry()
    demo_registry = demos or load_default_demo_registry()
    benchmarks.validate_traceability(capability_registry, demo_registry)
    descriptor = benchmarks.resolve(benchmark_id)
    target = output_dir or Path("/tmp") / (
        f"wargame-native-benchmark-{descriptor.benchmark_id}"
    )
    if target.exists() and any(target.iterdir()) and not overwrite:
        raise FileExistsError(f"benchmark output exists: {target}")
    target.mkdir(parents=True, exist_ok=True)
    demo_output = target / "demo_output"
    try:
        demo_report = demo_runner(
            descriptor.scenario,
            registry=demo_registry,
            output_dir=demo_output,
            seed=seed,
            tick_interval_ms=descriptor.validation.tick_interval_ms,
            overwrite=overwrite,
        )
        validation = _json_object(demo_output / "validation_report.json")
        checks = _check_map(validation)
        replay_path = Path(str(demo_report["replay"]))
        bundle = ScenarioBundle.from_path(
            demo_registry.scenario_path(descriptor.scenario)
        )
        strict = replay_source_archive(
            bundle.scenario,
            replay_path,
            strict=True,
            map_runtime_data=bundle.map_runtime_data,
        )
        execution = _reexecute_action_stream(bundle, replay_path)
        event_trace = tuple(execution["event_trace"])
        expected_events_ok = _ordered_subset(
            descriptor.expected_events,
            event_trace,
        )
        engine = _validation_result(
            descriptor.validation.engine_checks,
            checks,
        )
        human = _validation_result(
            descriptor.validation.human_checks,
            checks,
        )
        agent = _validation_result(
            descriptor.validation.agent_checks,
            checks,
        )
        replay_check = checks.get(descriptor.validation.replay_check) is True
        replay_passed = (
            strict.matches
            and replay_check
            and execution["world_hash"] == strict.expected_world_hash
        )
        capability_results = {}
        for capability_id in descriptor.capability_ids:
            names = descriptor.validation.checks_for(capability_id)
            if not names:
                names = tuple(dict.fromkeys((
                    *descriptor.validation.engine_checks,
                    *descriptor.validation.human_checks,
                    *descriptor.validation.agent_checks,
                )))
            capability_results[capability_id] = _validation_result(names, checks)
        passed = all((
            engine["passed"],
            not descriptor.human_required or human["passed"],
            not descriptor.agent_required or agent["passed"],
            not descriptor.replay_required or replay_passed,
            expected_events_ok,
            all(value["passed"] for value in capability_results.values()),
        ))
        operation_types = _operation_types_for(descriptor.capability_ids)
        human_validation = {
            "available": human["passed"],
            "operation_types": list(operation_types),
            "executed": human["passed"],
            "result": "PASS" if human["passed"] else "FAIL",
            "checks": human["checks"],
            "path": [
                "CAPABILITY_PROJECTION",
                "HUMAN_OPERATION_CONTRACT",
                "REALTIME_GATEWAY",
                "ENGINE",
            ],
        }
        agent_validation = {
            "available": agent["passed"],
            "valid_action_visible": agent["passed"],
            "execute": agent["passed"],
            "result": "PASS" if agent["passed"] else "FAIL",
            "checks": agent["checks"],
            "path": [
                "SIDE_OBSERVATION",
                "ENGINE_CAPABILITY",
                "SEAT_OPERATION",
                "REALTIME_GATEWAY",
                "ENGINE",
            ],
        }
        report: dict[str, object] = {
            "schema": "native-capability-benchmark-report/v1",
            "benchmark_id": descriptor.benchmark_id,
            "capability": (
                descriptor.capability if not descriptor.composite else "composite"
            ),
            "capability_ids": list(descriptor.capability_ids),
            "capability_results": capability_results,
            "scenario": descriptor.scenario,
            "required_features": list(descriptor.required_features),
            "engine_result": engine,
            "human_result": human_validation,
            "human_validation": human_validation,
            "agent_result": agent_validation,
            "agent_validation": agent_validation,
            "replay_result": {
                "strict": True,
                "matches": strict.matches,
                "world_hash_matches": (
                    execution["world_hash"] == strict.expected_world_hash
                ),
                "result": "PASS" if replay_passed else "FAIL",
            },
            "world_hash": execution["world_hash"],
            "rng_hash": execution["rng_hash"],
            "event_trace": list(event_trace),
            "expected_events": list(descriptor.expected_events),
            "expected_events_match": expected_events_ok,
            "passed": passed,
            "demo_output": str(demo_output),
            "replay": str(replay_path),
        }
    except Exception as exc:  # produce auditable failure output, never hide it
        report = _failure_report(descriptor, target, exc)
    _write_json(target / "event_trace.json", {
        "benchmark": descriptor.benchmark_id,
        "events": report.get("event_trace", []),
    })
    _write_json(target / "benchmark_report.json", report)
    return report


def render_benchmarks(
    descriptors: Sequence[CapabilityBenchmarkDescriptor],
) -> str:
    headers = ("BENCHMARK", "CAPABILITY", "DEMO", "REPLAY", "HUMAN", "AGENT")
    rows = [(
        item.benchmark_id,
        ",".join(item.capability_ids),
        item.scenario,
        _yes_no(item.replay_required),
        _yes_no(item.human_required),
        _yes_no(item.agent_required),
    ) for item in descriptors]
    widths = [max(len(headers[index]), *(len(row[index]) for row in rows))
              for index in range(len(headers))]
    return "\n".join((
        "Native Engine Capability Benchmark",
        _format_row(headers, widths),
        "  ".join("-" * width for width in widths),
        *(_format_row(row, widths) for row in rows),
    ))


def _reexecute_action_stream(
    bundle: ScenarioBundle,
    replay_path: Path,
) -> dict[str, object]:
    """Re-execute recorded inputs; never restore a recorded final snapshot."""

    archive = SourceReplayArchiveImporter().import_path(replay_path)
    seed = archive.manifest.get("episode_seed")
    if type(seed) is not int:
        raise ValueError("Replay lacks exact episode_seed")
    engine = NativeEngine(
        bundle.scenario,
        max_ticks=int(archive.manifest.get(
            "max_ticks", bundle.scenario.terminal_rules.max_ticks
        )),
        map_runtime_data=bundle.map_runtime_data,
    )
    events = []
    try:
        engine.reset(seed)
        for record in archive.steps:
            ordered = tuple(
                EngineAction.from_dict(value)
                for value in record.get("ordered_actions", ())
            )
            delta = record.get("delta_time_ms")
            if type(delta) is not int or delta <= 0:
                raise ValueError("Replay step lacks positive delta_time_ms")
            result = engine.step(ordered, delta_time_ms=delta)
            events.extend(result.events)
        snapshot = engine.snapshot()
        world_hash = canonical_hash(snapshot["world"])
        rng_hash = canonical_hash(snapshot["rng"])
    finally:
        engine.close()
    return {
        "world_hash": world_hash,
        "rng_hash": rng_hash,
        "event_trace": [event.event_type.value.upper() for event in events],
    }


def _validation_result(
    names: Sequence[str],
    checks: Mapping[str, bool],
) -> dict[str, object]:
    values = {name: checks.get(name) is True for name in names}
    return {"passed": all(values.values()), "checks": values}


def _check_map(value: Mapping[str, object]) -> dict[str, bool]:
    raw = value.get("checks")
    if not isinstance(raw, list):
        raise ValueError("validation report lacks checks")
    result: dict[str, bool] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("validation check must be an object")
        name, outcome = item.get("name"), item.get("result")
        if not isinstance(name, str) or type(outcome) is not bool:
            raise ValueError("validation check is malformed")
        if name in result:
            raise ValueError(f"duplicate validation check: {name}")
        result[name] = outcome
    return result


def _ordered_subset(expected: Sequence[str], actual: Sequence[str]) -> bool:
    offset = 0
    for value in actual:
        if offset < len(expected) and value == expected[offset]:
            offset += 1
    return offset == len(expected)


def _operation_types_for(capability_ids: Sequence[str]) -> tuple[str, ...]:
    mappings = {
        "boarding": ("BOARD", "UNLOAD"),
        "direct_fire": ("DIRECT_FIRE",),
        "drone": ("MOVE", "WAIT"),
        "guided_fire": ("GUIDED_FIRE",),
        "indirect_fire": ("INDIRECT_FIRE",),
        "loitering_munition": ("MOVE", "DIRECT_FIRE"),
        "objective_control": ("MOVE",),
        "visibility": ("OBSERVATION",),
    }
    return tuple(dict.fromkeys(
        operation
        for capability_id in capability_ids
        for operation in mappings[capability_id]
    ))


def _failure_report(
    descriptor: CapabilityBenchmarkDescriptor,
    target: Path,
    error: Exception,
) -> dict[str, object]:
    return {
        "schema": "native-capability-benchmark-report/v1",
        "benchmark_id": descriptor.benchmark_id,
        "capability": (
            descriptor.capability if not descriptor.composite else "composite"
        ),
        "capability_ids": list(descriptor.capability_ids),
        "capability_results": {
            capability_id: {"passed": False, "checks": {}}
            for capability_id in descriptor.capability_ids
        },
        "scenario": descriptor.scenario,
        "engine_result": {"passed": False, "checks": {}},
        "human_result": {"available": False, "operation_types": [],
                         "executed": False, "result": "FAIL", "checks": {}},
        "human_validation": {"available": False, "operation_types": [],
                             "executed": False, "result": "FAIL",
                             "checks": {}, "path": []},
        "agent_result": {"available": False, "valid_action_visible": False,
                         "execute": False, "result": "FAIL", "checks": {}},
        "agent_validation": {"available": False,
                             "valid_action_visible": False, "execute": False,
                             "result": "FAIL", "checks": {}, "path": []},
        "replay_result": {"strict": True, "matches": False,
                          "world_hash_matches": False, "result": "FAIL"},
        "world_hash": None,
        "rng_hash": None,
        "event_trace": [],
        "expected_events": list(descriptor.expected_events),
        "expected_events_match": False,
        "passed": False,
        "error": {"type": type(error).__name__, "message": str(error)},
        "demo_output": str(target / "demo_output"),
    }


def _json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _yes_no(value: bool) -> str:
    return "YES" if value else "NO"


def _format_row(values: Sequence[str], widths: Sequence[int]) -> str:
    return "  ".join(value.ljust(widths[index])
                     for index, value in enumerate(values)).rstrip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Native capability benchmarks")
    parser.add_argument("command", help="list, check, run, or benchmark ID")
    parser.add_argument("benchmark", nargs="?")
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        registry = (
            CapabilityBenchmarkRegistry.load(args.registry)
            if args.registry is not None
            else load_default_benchmark_registry()
        )
        if args.command == "list":
            print(render_benchmarks(registry.descriptors))
            return 0
        if args.command == "check":
            registry.validate_traceability(
                load_default_capability_registry(),
                load_default_demo_registry(),
            )
            print(render_benchmarks(registry.descriptors))
            print("\nBenchmark traceability: PASS")
            return 0
        benchmark_id = args.benchmark if args.command == "run" else args.command
        if not benchmark_id:
            raise CapabilityBenchmarkError("run requires a benchmark ID")
        report = run_capability_benchmark(
            benchmark_id,
            registry=registry,
            output_dir=args.output_dir,
            seed=args.seed,
            overwrite=args.overwrite,
        )
    except (CapabilityBenchmarkError, ValueError, OSError) as exc:
        parser.exit(1, f"Benchmark: FAIL: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
