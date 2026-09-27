#!/usr/bin/env python3
"""Measure product-tick cost without UI, transport, or model inference."""

from __future__ import annotations

import argparse
import cProfile
import json
import math
from pathlib import Path
import statistics
import time

from scripts.run_realtime_agent_match import _MatchFactory
from wargame_native_engine.agents import (
    RealtimeAgentSession,
    RealtimeExecutionMode,
)
from wargame_native_engine.data import ScenarioBundle


def _distribution(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)
    return {
        "n": len(ordered),
        "min_ms": ordered[0],
        "p50_ms": statistics.median(ordered),
        "p95_ms_nearest_rank": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "mean_ms": statistics.fmean(ordered),
        "max_ms": ordered[-1],
    }


def _measure(call):
    started = time.perf_counter_ns()
    value = call()
    return (time.perf_counter_ns() - started) / 1_000_000.0, value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, type=Path)
    parser.add_argument("--map-runtime", required=True, type=Path)
    parser.add_argument("--storage-root", required=True, type=Path)
    parser.add_argument("--steps", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--tick-ms", type=int, default=1_000)
    parser.add_argument(
        "--profile-output",
        type=Path,
        help="optional cProfile output; profiling covers advance_tick only",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.steps < 1:
        raise ValueError("steps must be at least 1")
    if args.tick_ms <= 0:
        raise ValueError("tick-ms must be positive")
    if args.storage_root.exists():
        raise FileExistsError(
            f"storage-root must not already exist: {args.storage_root}"
        )

    load_ms, bundle = _measure(
        lambda: ScenarioBundle.from_path(args.scenario).with_map_runtime_data(
            args.map_runtime
        )
    )
    session = RealtimeAgentSession(
        _MatchFactory(
            bundle,
            storage_root=args.storage_root,
            tick_interval_ms=args.tick_ms,
        ),
        run_namespace="runtime-identity-benchmark",
        tick_interval_ms=args.tick_ms,
        execution_mode=RealtimeExecutionMode.MANUAL,
    )
    cache_stats = None
    cache_entries_released_on_close = None
    try:
        reset_ms, _ = _measure(lambda: session.reset(seed=args.seed))
        samples: list[float] = []
        ticks: list[int] = []
        profile = cProfile.Profile() if args.profile_output is not None else None
        if profile is not None:
            profile.enable()
        try:
            for _ in range(args.steps):
                elapsed_ms, tick = _measure(session.advance_tick)
                samples.append(elapsed_ms)
                ticks.append(tick.tick_index)
        finally:
            if profile is not None:
                profile.disable()
                args.profile_output.parent.mkdir(parents=True, exist_ok=True)
                profile.dump_stats(args.profile_output)
        match = session._match  # noqa: SLF001 - benchmark-only instrumentation
        if match is not None:
            manager = match.gateway.manager
            session_id = match.session_id
            cache_stats = manager.controller_view_cache_stats(session_id)
    finally:
        session.close(timeout_seconds=30.0)
        if cache_stats is not None:
            cache_entries_released_on_close = (  # noqa: SLF001
                session_id not in manager._sessions
            )

    report = {
        "schema": "native-runtime-identity-benchmark/1.0",
        "scenario_id": bundle.scenario_identity.scenario_id,
        "scenario_hash": bundle.scenario_identity.sha256,
        "map_id": bundle.map_identity.map_id,
        "map_hash": bundle.map_identity.sha256,
        "seed": args.seed,
        "steps": args.steps,
        "tick_interval_ms": args.tick_ms,
        "action_sequence": "consecutive empty RED/BLUE decisions",
        "timing_scope": (
            "advance_tick includes authoritative step, GREEN/side projections, "
            "controller projection clone/serialization, analysis, and replay "
            "recording; excludes load, reset, UI, HTTP encoding/transport, final "
            "benchmark JSON encoding, and model inference"
        ),
        "load_ms": load_ms,
        "reset_ms": reset_ms,
        "advance_tick_ms": samples,
        "advance_tick": _distribution(samples),
        "steady_state_after_first": (
            None if len(samples) == 1 else _distribution(samples[1:])
        ),
        "profile_output": (
            None if args.profile_output is None else str(args.profile_output)
        ),
        "profile_timing_warning": (
            None
            if args.profile_output is None
            else "advance_tick samples include cProfile instrumentation overhead"
        ),
        "tick_indices": ticks,
        "controller_view_cache": cache_stats,
        "controller_view_cache_released_on_close": (
            cache_entries_released_on_close
        ),
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
