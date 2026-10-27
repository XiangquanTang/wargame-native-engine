#!/usr/bin/env python3
"""Opt-in trusted external Miaosuan agent runner; never extracts or vendors ZIPs."""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys

from wargame_native_engine.agents import FirstValidActionAgent, NoOpAgent, RandomLegalAgent
from wargame_native_engine.data import ScenarioBundle, prepare_map_runtime_data
from wargame_native_engine.evaluation.miaosuan_runner import run_miaosuan_match
from wargame_native_engine.io.serialization import canonical_hash, file_sha256


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True,
                        help="canonical native scenario, including examples/map123/scenario.json")
    parser.add_argument("--map-runtime-data", type=Path)
    parser.add_argument("--prepare-runtime-from", type=Path, help="Map123 release directory")
    parser.add_argument("--cache-root", type=Path, default=Path("local_data/miaosuan/runtime-cache"))
    parser.add_argument("--agent-root", type=Path, required=True, help="directory containing ai/agent.py")
    parser.add_argument("--agent-module", default="ai.agent")
    parser.add_argument("--agent-class", default="Agent")
    parser.add_argument("--allow-external-code", action="store_true",
                        help="explicitly execute trusted external Python with this process's privileges")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--side", choices=("red", "blue"), default="red")
    parser.add_argument("--opponent", choices=("first-valid", "random-legal", "noop", "self-play"), default="self-play",
                        help="self-play uses equal full-side batch permissions; others are unequal-budget integration checks")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-decisions", type=int, default=3)
    parser.add_argument("--decision-interval-ms", type=int, default=1000)
    parser.add_argument("--decision-budget-ms", type=int)
    parser.add_argument("--max-trace-bytes", type=int, default=16 * 1024 * 1024)
    args = parser.parse_args(argv)
    if not args.allow_external_code:
        parser.error("external code is disabled; review/trust the source before --allow-external-code")
    if args.map_runtime_data and args.prepare_runtime_from:
        parser.error("choose existing runtime data or preparation, not both")
    if args.output.exists():
        parser.error("output directory must not exist")
    try:
        bundle = ScenarioBundle.from_path(args.scenario)
        if args.prepare_runtime_from:
            prepared = prepare_map_runtime_data(bundle.scenario.map,
                                                release_dir=args.prepare_runtime_from,
                                                cache_root=args.cache_root)
            bundle = bundle.with_map_runtime_data(prepared.cache_dir)
        elif args.map_runtime_data:
            bundle = bundle.with_map_runtime_data(args.map_runtime_data)
        root = args.agent_root.resolve(strict=True)
        sources = {path.relative_to(root).as_posix(): file_sha256(path)
                   for path in sorted(root.rglob("*.py"))}
        if not sources:
            raise ValueError("agent root contains no Python sources")
        sys.path.insert(0, str(root))
        module = importlib.import_module(args.agent_module)
        loaded = Path(module.__file__).resolve()
        if not loaded.is_relative_to(root):
            raise ValueError("agent module did not resolve inside the requested root")
        constructor = getattr(module, args.agent_class)
        opponents = {"first-valid": lambda side, seed: FirstValidActionAgent(),
                     "random-legal": lambda side, seed: RandomLegalAgent(seed),
                     "noop": lambda side, seed: NoOpAgent()}
        report = run_miaosuan_match(
            bundle, args.output, lambda side, seed: constructor(),
            policy_id="miaosuan_flexible_native_v1",
            policy_provenance={"python_sources_sha256": canonical_hash(sources),
                               "module": args.agent_module, "class": args.agent_class},
            controlled_side=args.side, seed=args.seed,
            opponent_factory=opponents.get(args.opponent), opponent_id=args.opponent,
            self_play=args.opponent == "self-play", max_decisions=args.max_decisions,
            decision_interval_ms=args.decision_interval_ms,
            decision_budget_ms=args.decision_budget_ms, max_trace_bytes=args.max_trace_bytes,
        )
        print(json.dumps({key: report[key] for key in
                          ("status", "strict_replay_matches", "applied_decisions", "errors")}, indent=2))
        return 0 if report["status"] == "completed" else 1
    except (ValueError, TypeError, OSError, ImportError, AttributeError) as error:
        print(f"miaosuan configuration error: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
