"""Alternating in-process copy microbenchmark of an actual detached seat snapshot."""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import tracemalloc
from copy import deepcopy
from pathlib import Path
from time import perf_counter_ns

from benchmark_runtime import session_for, source_identity

from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.detached import detached_copy
from wargame_native_engine.io.serialization import canonical_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario", type=Path, default=Path("examples/public_river_valley_scenario.json")
    )
    parser.add_argument("--repeats", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    methods = {"stdlib_deepcopy": deepcopy, "detached_copy": detached_copy}
    values = {name: [] for name in methods}
    result = {
        **source_identity(),
        "scenario": str(args.scenario),
        "seed": 42,
        "workload": "red realtime/controller snapshot copy, in process",
        "order": "alternating AB/BA pairs after one warmup copy per method",
    }
    with tempfile.TemporaryDirectory(prefix="snapshot-copy-bench-") as storage:
        session = session_for(ScenarioBundle.from_path(args.scenario), storage, "copy-bench")
        try:
            reset = session.reset(seed=42)
            snapshot = session.controller_snapshot(reset.red_binding)
            expected = canonical_hash(snapshot)
            for method in methods.values():
                if canonical_hash(method(snapshot)) != expected:
                    raise AssertionError("copy changed the snapshot")
            for index in range(args.repeats):
                order = list(methods) if index % 2 == 0 else list(reversed(methods))
                for name in order:
                    start = perf_counter_ns()
                    copied = methods[name](snapshot)
                    values[name].append((perf_counter_ns() - start) / 1e6)
                    del copied
            for name, method in methods.items():
                tracemalloc.start()
                copied = method(snapshot)
                peak = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
                result[name] = {
                    "count": args.repeats,
                    "p50_ms": statistics.median(values[name]),
                    "samples_ms": values[name],
                    "peak_allocated_bytes": peak,
                    "snapshot_hash": canonical_hash(copied),
                }
        finally:
            session.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
