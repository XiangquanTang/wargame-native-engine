"""Command-line smoke evaluator. Custom model adapters use the Python API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from wargame_native_engine.evaluation.manifest import load_manifest
from wargame_native_engine.evaluation.policy import FirstLegalPolicy, NoOpPolicy
from wargame_native_engine.evaluation.runner import run_benchmark


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, required=True, help="new output directory"
    )
    parser.add_argument("--policy", choices=("noop", "first-legal"), default="noop")
    args = parser.parse_args(argv)
    policy_type = NoOpPolicy if args.policy == "noop" else FirstLegalPolicy
    try:
        result = run_benchmark(
            load_manifest(args.manifest),
            args.output,
            lambda _: policy_type(),
            policy_id=f"{args.policy}/1.0",
        )
    except (TypeError, ValueError, OSError) as error:
        parser.exit(2, f"benchmark configuration/output error: {error}\n")
    print(
        json.dumps(
            {
                "status": result["status"],
                "summary": result["summary"],
                "semantic_fingerprint": result["semantic_fingerprint"],
                "report": str(args.output / "result.json"),
            },
            indent=2,
        )
    )
    return 0 if result["status"] == "completed" else 1
