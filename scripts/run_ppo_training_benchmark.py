"""Plan, preflight, execute, or aggregate a frozen Phase-7 PPO benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from wargame_native_engine.training.ppo_benchmark import PPOTrainingBenchmarkRunner
from wargame_native_engine.training.ppo_benchmark_config import load_ppo_training_benchmark_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "preflight", "run", "aggregate"))
    parser.add_argument(
        "--config",
        default="config/benchmarks/native_ppo_training_benchmark_pilot_v1.json",
    )
    parser.add_argument("--output", default="local_data/ppo_benchmark_phase7_pilot_v1")
    parser.add_argument("--reference-transitions-per-second", type=float, default=20.59)
    parser.add_argument("--no-skip-completed", action="store_true")
    args = parser.parse_args()
    protocol = load_ppo_training_benchmark_config(Path(args.config))
    runner = PPOTrainingBenchmarkRunner(
        protocol, output_root=Path(args.output),
        reference_transitions_per_second=args.reference_transitions_per_second,
    )
    if args.command == "plan":
        result = runner.dry_run_payload()
    elif args.command == "preflight":
        result = runner.preflight(run_transition_smoke=True)
    elif args.command == "aggregate":
        result = runner.aggregate_existing()
    else:
        result = runner.execute(skip_completed=not args.no_skip_completed)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
