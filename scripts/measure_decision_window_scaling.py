from __future__ import annotations

import argparse
import cProfile
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import pstats
import tempfile
import time
import tracemalloc
from typing import Callable, Sequence, TypeVar

from scripts.run_minimal_source_replay import _template
from wargame_native_engine.analysis import (
    EightFactorAnalyzer,
    GreenFrameAnalysisInput,
)
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.serialization import (
    canonical_green_frame_payload,
)
from wargame_native_engine.gateway import CommandGateway, DecisionWindow, SessionManager
from wargame_native_engine.io.native_map_converter import (
    convert_benchmark_basic_map,
)
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)


_T = TypeVar("_T")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure a 100-operator Native DecisionWindow without tuning.",
    )
    parser.add_argument("--map-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--operator-count", type=int, default=100)
    parser.add_argument("--coordinate-system", choices=("odd-r",))
    parser.add_argument("--hex-size-m", type=int)
    parser.add_argument("--elevation-unit-m", type=float)
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if type(args.operator_count) is not int or args.operator_count < 2:
        raise ValueError("operator-count must be an exact int of at least two")
    if args.output.exists():
        raise FileExistsError(f"measurement output already exists: {args.output}")
    source_before = hashlib.sha256(args.map_config.read_bytes()).hexdigest()
    conversion = convert_benchmark_basic_map(
        args.map_config,
        coordinate_system_override=args.coordinate_system,
        hex_size_m_override=args.hex_size_m,
        elevation_unit_m_override=args.elevation_unit_m,
    )
    scenario = _scaling_scenario(conversion.native_map.to_dict(), args.operator_count)
    environment = _environment(scenario)

    setup_ms, bundle = _measure(lambda: environment.setup_views(args.seed))
    red_valid_ms, red_valid = _measure(
        lambda: environment.session.valid_actions("red")
    )
    blue_valid_ms, blue_valid = _measure(
        lambda: environment.session.valid_actions("blue")
    )

    tracemalloc.start()
    freeze_start = time.perf_counter_ns()
    window = DecisionWindow.open(
        session_id="scaling-measurement",
        decision_window_index=0,
        opened_at_sim_time_ms=0,
        red_valid_actions=red_valid,
        blue_valid_actions=blue_valid,
    )
    freeze_ms = _elapsed_ms(freeze_start)
    _, freeze_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    action_ids = {
        side: tuple(window.valid_action_snapshots[side])
        for side in ("red", "blue")
    }
    lookup_ms, lookup_count = _measure(lambda: sum(
        window.valid_action_snapshots[side][action_id].action.actor_id is not None
        for side in ("red", "blue")
        for action_id in action_ids[side]
    ))
    selected = {
        side: _one_wait_per_actor(window, side)
        for side in ("red", "blue")
    }
    validation_start = time.perf_counter_ns()
    window.submit(
        side="red",
        request_id="measure-red",
        ordered_action_ids=selected["red"],
    )
    window.submit(
        side="blue",
        request_id="measure-blue",
        ordered_action_ids=selected["blue"],
    )
    window.close()
    batch_validation_ms = _elapsed_ms(validation_start)

    projection_ms, _ = _measure(lambda: environment._project(bundle.green))
    raw_step_ms, stepped = _measure(lambda: environment.step_views(
        window.merged_action_batch,
        delta_time_seconds=1,
    ))
    analyzer = EightFactorAnalyzer()
    frame_payload = canonical_green_frame_payload(stepped[0].green)
    analyzer_ms, _ = _measure(lambda: analyzer.analyze_green_frame(
        GreenFrameAnalysisInput(
            frame_index=1,
            frame=stepped[0].green,
            frame_hash=hashlib.sha256(frame_payload).hexdigest(),
            scenario_hash=scenario.scenario_hash,
        )
    ))

    profile = cProfile.Profile()
    profile.enable()
    profile_start = time.perf_counter_ns()
    environment.step_views((), delta_time_seconds=1)
    profiled_step_ms = _elapsed_ms(profile_start)
    profile.disable()
    profile_stats = pstats.Stats(profile).stats
    total_internal_seconds = sum(item[2] for item in profile_stats.values())
    visibility_seconds = sum(
        item[2]
        for key, item in profile_stats.items()
        if "/visibility/" in key[0] or "visibility" in key[2]
    )

    with tempfile.TemporaryDirectory(
        prefix="wargame-100-operator-measurement."
    ) as storage:
        manager = SessionManager(
            lambda: _environment(scenario),
            storage_root=storage,
        )
        gateway = CommandGateway(
            manager,
            decision_interval_ms=1_000,
            frame_interval_ms=1_000,
        )
        managed_setup_ms, descriptor = _measure(lambda: gateway.create_session(
            red_controller_id="measure-red",
            blue_controller_id="measure-blue",
            seed=args.seed,
            record=True,
        ))
        managed_window = gateway.window(
            descriptor.session_id, "measure-red"
        )
        red_batch = _wait_ids_from_records(managed_window.valid_actions)
        blue_window = gateway.window(
            descriptor.session_id, "measure-blue"
        )
        blue_batch = _wait_ids_from_records(blue_window.valid_actions)
        gateway.submit_batch(
            descriptor.session_id,
            "measure-red",
            request_id="managed-red",
            expected_window=0,
            ordered_action_ids=red_batch,
        )
        managed_advance_ms, _ = _measure(lambda: gateway.submit_batch(
            descriptor.session_id,
            "measure-blue",
            request_id="managed-blue",
            expected_window=0,
            ordered_action_ids=blue_batch,
        ))
        gateway.close_session(descriptor.session_id)

    source_after = hashlib.sha256(args.map_config.read_bytes()).hexdigest()
    report = {
        "schema": "decision-window-scaling-measurement/1.0",
        "operator_count": args.operator_count,
        "side_operator_counts": {
            "red": len(selected["red"]),
            "blue": len(selected["blue"]),
        },
        "seed": args.seed,
        "scenario_hash": scenario.scenario_hash,
        "map": {
            "map_id": conversion.native_map.map_id,
            "source_sha256": source_before,
            "native_map_sha256": conversion.native_map_sha256,
            "source_unchanged": source_before == source_after,
        },
        "valid_action_counts": {
            "red": len(red_valid),
            "blue": len(blue_valid),
        },
        "merged_action_count": len(window.merged_action_batch),
        "measurements_ms": {
            "source_environment_setup": setup_ms,
            "red_valid_actions": red_valid_ms,
            "blue_valid_actions": blue_valid_ms,
            "freeze_action_snapshots": freeze_ms,
            "all_action_id_lookups": lookup_ms,
            "ordered_batch_validation_and_merge": batch_validation_ms,
            "red_blue_source_projection": projection_ms,
            "raw_frame_step_with_100_actions": raw_step_ms,
            "eight_factor_frame_analysis": analyzer_ms,
            "profiled_empty_frame_step": profiled_step_ms,
            "managed_setup_with_recorder_and_analyzer": managed_setup_ms,
            "managed_frame_advance_with_recorder_and_analyzer": managed_advance_ms,
        },
        "memory_bytes": {
            "freeze_action_snapshots_peak_tracemalloc": freeze_peak,
        },
        "lookup_count": lookup_count,
        "visibility_profile": {
            "internal_seconds": visibility_seconds,
            "profile_internal_seconds": total_internal_seconds,
            "internal_time_percent": (
                0.0
                if total_internal_seconds <= 0
                else visibility_seconds / total_internal_seconds * 100.0
            ),
            "note": "cProfile internal Python time; not wall-time attribution",
        },
        "optimization_performed": False,
        "limitations": [
            "one synthetic 50-vs-50 placement over one explicitly converted map",
            "timings are single-run smoke measurements, not a statistical benchmark",
            "no result is extrapolated to the 370k historical archive",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def _scaling_scenario(map_record: dict, operator_count: int):
    source = json.loads(
        Path("examples/source_movement_scenario.json").read_text(
            encoding="utf-8"
        )
    )
    map_hexes = map_record["hexes"]
    if len(map_hexes) < operator_count:
        raise ValueError("converted map does not have enough distinct cells")
    templates = {
        item["side"]: item for item in source["operators"]
    }
    red_count = operator_count // 2
    operators = []
    positions = {"red": {}, "blue": {}}
    for index in range(operator_count):
        side = "red" if index < red_count else "blue"
        source_index = (
            index * 2
            if side == "red"
            else len(map_hexes) - 1 - (index - red_count) * 2
        )
        cell = map_hexes[source_index]
        value = deepcopy(templates[side])
        operator_id = index + 1
        current_hex = int(cell["pos"])
        row, column = divmod(current_hex, 100)
        value.update({
            "operator_id": operator_id,
            "x": column,
            "y": row,
            "current_hex": current_hex,
            "weapon_ids": [],
            "attack_range": 0,
            "attack_damage": 0,
        })
        operators.append(value)
        positions[side][str(operator_id)] = [column, row]
    source.update({
        "scenario_id": f"decision-window-scaling-{operator_count}",
        "map": map_record,
        "operators": operators,
        "deployment": {"positions": positions},
        "initial_stage": "combat",
    })
    return scenario_from_dict(source)


def _environment(scenario) -> SourceCompatibleEnvironment:
    return SourceCompatibleEnvironment(
        GameSession(NativeEngineBackend(scenario)),
        template=_template(scenario),
    )


def _one_wait_per_actor(window: DecisionWindow, side: str) -> tuple[str, ...]:
    result = []
    actors = set()
    for action_id, valid in window.valid_action_snapshots[side].items():
        action = valid.action
        if action.action_type.value != "WAIT" or action.actor_id in actors:
            continue
        actors.add(action.actor_id)
        result.append(action_id)
    return tuple(result)


def _wait_ids_from_records(values) -> tuple[str, ...]:
    result = []
    actors = set()
    for value in values:
        action = value["action"]
        if action["action_type"] != "WAIT" or action["actor_id"] in actors:
            continue
        actors.add(action["actor_id"])
        result.append(value["action_id"])
    return tuple(result)


def _measure(call: Callable[[], _T]) -> tuple[float, _T]:
    started = time.perf_counter_ns()
    result = call()
    return _elapsed_ms(started), result


def _elapsed_ms(started_ns: int) -> float:
    return (time.perf_counter_ns() - started_ns) / 1_000_000


if __name__ == "__main__":
    raise SystemExit(main())
