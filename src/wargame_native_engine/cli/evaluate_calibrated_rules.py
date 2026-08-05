from __future__ import annotations

import argparse
import json
from pathlib import Path

from wargame_native_engine.calibration.calibrator import evaluate_partition
from wargame_native_engine.cli.run_game import _validate_output


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Evaluate a fixed Native ruleset on one replay partition")
    value.add_argument("--split", required=True, type=Path)
    value.add_argument("--ruleset", required=True, type=Path)
    value.add_argument("--partition", required=True,
                       choices=("smoke", "calibration", "validation"))
    value.add_argument("--weights", type=Path,
                       default=Path("config/calibration/metric_weights_v1.json"))
    value.add_argument("--output", required=True, type=Path)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        _validate_output(args.output)
        report = evaluate_partition(
            split_path=args.split, ruleset_path=args.ruleset,
            partition=args.partition, weights_path=args.weights,
            output_dir=args.output)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": str(exc)}, ensure_ascii=False,
                         sort_keys=True, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
