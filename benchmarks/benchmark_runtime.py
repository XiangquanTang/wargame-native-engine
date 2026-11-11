"""Reproducible local game runtime benchmark; no model training or network."""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import platform
import statistics
import tempfile
import tracemalloc
from pathlib import Path
from time import perf_counter_ns

from wargame_native_engine.agents.realtime import (
    ManualRealtimeClock,
    RealtimeAgentSession,
)
from wargame_native_engine.agents.seat import SeatDecision
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.operations import ScenarioMatchFactory
from wargame_native_engine.version import (
    ADJUDICATION_SEMANTICS_VERSION,
    ENGINE_VERSION,
)
from wargame_native_engine.viewer.realtime_control_server import (
    RealtimeDualControlHTTPServer,
)


def source_identity():
    """Identify the imported implementation, including uncommitted source edits."""
    import wargame_native_engine

    root = Path(wargame_native_engine.__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return {
        "engine_version": ENGINE_VERSION,
        "adjudication_semantics_version": ADJUDICATION_SEMANTICS_VERSION,
        "source_tree_sha256": digest.hexdigest(),
        "benchmark_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_tree_sha256": tree_identity(root.parents[1] / "config"),
        "rules_docs_tree_sha256": tree_identity(root.parents[1] / "docs" / "rules"),
        "assets_tree_sha256": tree_identity(root.parents[1] / "assets"),
    }


def tree_identity(root):
    """Hash file paths and bytes so separately extracted data can be compared."""
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def samples(call, count):
    values = []
    for _ in range(count):
        start = perf_counter_ns()
        call()
        values.append((perf_counter_ns() - start) / 1e6)
    ordered = sorted(values)
    return {
        "count": count,
        "p50_ms": statistics.median(values),
        "p95_ms": ordered[min(count - 1, int(count * 0.95))],
        "samples_ms": values,
    }


def session_for(bundle, storage, namespace):
    return RealtimeAgentSession(
        ScenarioMatchFactory(
            bundle=bundle,
            storage_root=Path(storage),
            red_controller_id="bench-red",
            blue_controller_id="bench-blue",
            decision_interval_ms=1000,
            frame_interval_ms=1000,
        ),
        run_namespace=namespace,
        tick_interval_ms=1000,
        clock=ManualRealtimeClock(),
    )


def run(
    scenario,
    ticks,
    repeats,
    profile,
    *,
    map_runtime_data=None,
    warmup=1,
    history_sizes=(0, 100, 1000, 10000),
):
    bundle = ScenarioBundle.from_path(scenario)
    if map_runtime_data is not None:
        bundle = bundle.with_map_runtime_data(map_runtime_data)
    runtime_manifest = (
        None if map_runtime_data is None
        else Path(map_runtime_data) / "map_runtime_manifest.native.json"
    )
    result = {
        **source_identity(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "scenario": str(scenario),
        "scenario_sha256": hashlib.sha256(Path(scenario).read_bytes()).hexdigest(),
        "map_runtime_data": None if map_runtime_data is None else str(map_runtime_data),
        "map_runtime_manifest_sha256": (
            None if runtime_manifest is None
            else hashlib.sha256(runtime_manifest.read_bytes()).hexdigest()
        ),
        "seed": 42,
        "tick_interval_ms": 1000,
        "warmup_ticks": warmup,
        "workload": "empty decisions / no-op ticks",
    }
    with tempfile.TemporaryDirectory(prefix="game-runtime-bench-") as storage:
        session = session_for(bundle, storage, "runtime-benchmark")
        reset = session.reset(seed=42)
        server = object.__new__(RealtimeDualControlHTTPServer)
        server.realtime_session = session
        server.bindings = {"red": reset.red_binding, "blue": reset.blue_binding}
        try:
            for _ in range(warmup):
                session.advance_tick()  # warm up actual tick path
            profiler = cProfile.Profile() if profile else None
            if profiler:
                profiler.enable()
            result["tick"] = samples(session.advance_tick, ticks)
            if profiler:
                profiler.disable()
                profiler.dump_stats(profile)
            # Stable canonical facts, excluding wall timings, permit A/B equality.
            result["tick_facts_hash"] = canonical_hash(
                [record.authoritative_record() for record in session.tick_records]
            )
            result["red_view_hash"] = canonical_hash(
                session.latest_view(reset.red_binding).to_record()
            )
            result["blue_view_hash"] = canonical_hash(
                session.latest_view(reset.blue_binding).to_record()
            )
            history = []
            previous_count = 0
            version = session.latest_view(reset.red_binding).observation_version
            for count in history_sizes:
                # Real accepted submissions; all but the last are superseded.
                # No game tactical policy is involved in empty decisions.
                for _ in range(count - previous_count):
                    session.submit(
                        reset.red_binding,
                        observation_version=version,
                        decision=SeatDecision(),
                    )
                previous_count = count
                entry = {
                    "history_events": count,
                    **samples(lambda: server.status("red", reset.red_binding), repeats),
                }
                tracemalloc.start()
                server.status("red", reset.red_binding)
                entry["peak_allocated_bytes"] = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
                history.append(entry)
            result["status"] = history
        finally:
            session.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        type=Path,
        default=Path("examples/public_river_valley_scenario.json"),
    )
    parser.add_argument("--ticks", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--profile", type=str)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--map-runtime-data", type=Path)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument(
        "--history-sizes", type=int, nargs="+", default=[0, 100, 1000, 10000]
    )
    args = parser.parse_args()
    if args.ticks < 1 or args.repeats < 1:
        parser.error("ticks and repeats must be positive")
    if (
        args.warmup < 0
        or args.history_sizes != sorted(set(args.history_sizes))
        or min(args.history_sizes) < 0
    ):
        parser.error(
            "warmup must be nonnegative and history sizes ascending and unique"
        )
    result = run(
        args.scenario,
        args.ticks,
        args.repeats,
        args.profile,
        map_runtime_data=args.map_runtime_data,
        warmup=args.warmup,
        history_sizes=args.history_sizes,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
