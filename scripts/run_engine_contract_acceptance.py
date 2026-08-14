"""Long-run exact-time/determinism acceptance for the managed Native chain."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import time
from typing import Any, Sequence

from scripts.measure_decision_window_scaling import (
    _environment,
    _scaling_scenario,
    _wait_ids_from_records,
)
from wargame_native_engine.analysis import analyze_replay_to_jsonl
from wargame_native_engine.gateway import CommandGateway, SessionManager
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.native_map_converter import (
    convert_benchmark_basic_map,
)
from wargame_native_engine.io.replay import replay_source_archive


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run two deterministic managed episodes and compare evidence.",
    )
    parser.add_argument("--map-config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--operator-count", type=int, default=100)
    parser.add_argument("--decision-windows", type=int, default=60)
    parser.add_argument("--frame-interval-ms", type=int, default=1_000)
    parser.add_argument("--coordinate-system", choices=("odd-r",), required=True)
    parser.add_argument("--seed", type=int, default=42)
    return parser


def run_acceptance(
    scenario,
    output_dir: Path,
    *,
    decision_windows: int,
    frame_interval_ms: int,
    seed: int,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"acceptance output already exists: {output_dir}")
    if type(decision_windows) is not int or decision_windows <= 0:
        raise ValueError("decision_windows must be a positive exact int")
    if type(frame_interval_ms) is not int or frame_interval_ms <= 0:
        raise ValueError("frame_interval_ms must be a positive exact int")
    output_dir.mkdir(parents=True)

    first = _run_episode(
        scenario,
        output_dir / "red-first",
        decision_windows=decision_windows,
        frame_interval_ms=frame_interval_ms,
        seed=seed,
        first_side="red",
    )
    second = _run_episode(
        scenario,
        output_dir / "blue-first",
        decision_windows=decision_windows,
        frame_interval_ms=frame_interval_ms,
        seed=seed,
        first_side="blue",
    )
    replay_equal = first["replay_bytes"] == second["replay_bytes"]
    analysis_equal = first["analysis_bytes"] == second["analysis_bytes"]
    report = {
        "schema": "native-engine-contract-acceptance/1.0",
        "status": "passed" if replay_equal and analysis_equal else "failed",
        "scenario_hash": scenario.scenario_hash,
        "seed": seed,
        "operator_count": len(scenario.operators),
        "decision_windows": decision_windows,
        "frame_interval_ms": frame_interval_ms,
        "expected_sim_time_ms": decision_windows * frame_interval_ms,
        "arrival_order_reversed": True,
        "replay_bytes_equal": replay_equal,
        "analysis_bytes_equal": analysis_equal,
        "runs": {
            "red_first": _public_run_record(first),
            "blue_first": _public_run_record(second),
        },
        "limitations": [
            "synthetic balanced placement over one explicitly converted map",
            "WAIT-only long run validates scheduling/evidence scale, not combat quality",
            "single-host timings are evidence, not a 1Hz service-level claim",
        ],
    }
    report_path = output_dir / "engine_contract_acceptance.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _run_episode(
    scenario,
    target: Path,
    *,
    decision_windows: int,
    frame_interval_ms: int,
    seed: int,
    first_side: str,
) -> dict[str, Any]:
    target.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="engine-contract-managed.") as storage:
        manager = SessionManager(
            lambda: _environment(scenario),
            storage_root=storage,
        )
        gateway = CommandGateway(
            manager,
            decision_interval_ms=frame_interval_ms,
            frame_interval_ms=frame_interval_ms,
        )
        descriptor = gateway.create_session(
            red_controller_id="contract-red",
            blue_controller_id="contract-blue",
            seed=seed,
            record=True,
        )
        controllers = {"red": "contract-red", "blue": "contract-blue"}
        other_side = "blue" if first_side == "red" else "red"
        frame_times_ms: list[float] = []
        started = time.perf_counter_ns()
        for window_index in range(decision_windows):
            action_ids = {
                side: _wait_ids_from_records(
                    gateway.window(descriptor.session_id, controllers[side]).valid_actions
                )
                for side in ("red", "blue")
            }
            gateway.submit_batch(
                descriptor.session_id,
                controllers[first_side],
                request_id=f"{first_side}-{window_index}",
                expected_window=window_index,
                ordered_action_ids=action_ids[first_side],
            )
            frame_started = time.perf_counter_ns()
            completed = gateway.submit_batch(
                descriptor.session_id,
                controllers[other_side],
                request_id=f"{other_side}-{window_index}",
                expected_window=window_index,
                ordered_action_ids=action_ids[other_side],
            )
            frame_times_ms.append((time.perf_counter_ns() - frame_started) / 1_000_000)
            if not completed.advanced:
                raise RuntimeError("completed side batch did not advance the window")
        runtime_ms = (time.perf_counter_ns() - started) / 1_000_000
        final = manager.descriptor(descriptor.session_id)
        publication = gateway.publish_session(
            descriptor.session_id,
            controllers[first_side],
            str(target / "published"),
            truncated=not final.terminal,
        )
        gateway.close_session(descriptor.session_id)

    offline_path = target / "frame_analysis.offline.jsonl"
    analyze_replay_to_jsonl(publication.replay_path, offline_path)
    replay_result = replay_source_archive(
        scenario, publication.replay_path, strict=True
    )
    archive = SourceReplayArchiveImporter().import_path(publication.replay_path)
    replay_bytes = publication.replay_path.read_bytes()
    analysis_bytes = publication.analysis_path.read_bytes()
    offline_bytes = offline_path.read_bytes()
    return {
        "first_side": first_side,
        "runtime_ms": runtime_ms,
        "frame_times_ms": frame_times_ms,
        "final_frame_index": final.frame_index,
        "final_sim_time_ms": final.sim_time_ms,
        "frame_count": len(archive.frames),
        "step_count": len(archive.steps),
        "strict_replay_match": replay_result.matches,
        "online_offline_analysis_equal": analysis_bytes == offline_bytes,
        "replay_sha256": hashlib.sha256(replay_bytes).hexdigest(),
        "analysis_sha256": hashlib.sha256(analysis_bytes).hexdigest(),
        "replay_bytes": replay_bytes,
        "analysis_bytes": analysis_bytes,
    }


def _public_run_record(value: dict[str, Any]) -> dict[str, Any]:
    frame_times = value["frame_times_ms"]
    return {
        key: item
        for key, item in value.items()
        if key not in {"replay_bytes", "analysis_bytes", "frame_times_ms"}
    } | {
        "frame_time_ms": {
            "minimum": min(frame_times),
            "maximum": max(frame_times),
            "mean": sum(frame_times) / len(frame_times),
        }
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if type(args.operator_count) is not int or args.operator_count < 2:
        raise ValueError("operator-count must be an exact int of at least two")
    source_before = hashlib.sha256(args.map_config.read_bytes()).hexdigest()
    conversion = convert_benchmark_basic_map(
        args.map_config,
        coordinate_system_override=args.coordinate_system,
    )
    scenario = _scaling_scenario(
        conversion.native_map.to_dict(), args.operator_count
    )
    report = run_acceptance(
        scenario,
        args.output_dir,
        decision_windows=args.decision_windows,
        frame_interval_ms=args.frame_interval_ms,
        seed=args.seed,
    )
    source_after = hashlib.sha256(args.map_config.read_bytes()).hexdigest()
    report["map"] = {
        "source_sha256": source_before,
        "native_map_sha256": conversion.native_map_sha256,
        "source_unchanged": source_before == source_after,
    }
    report_path = args.output_dir / "engine_contract_acceptance.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
