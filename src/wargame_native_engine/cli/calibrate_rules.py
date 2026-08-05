from __future__ import annotations

import argparse
import json
from pathlib import Path

from wargame_native_engine.calibration.calibrator import calibrate
from wargame_native_engine.cli.run_game import _validate_output


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Search bounded Native simplified rule candidates")
    value.add_argument("--split", required=True, type=Path)
    value.add_argument("--baseline-ruleset", required=True, type=Path)
    value.add_argument("--parameter-space", required=True, type=Path)
    value.add_argument("--weights", type=Path,
                       default=Path("config/calibration/metric_weights_v1.json"))
    value.add_argument("--output", required=True, type=Path)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        _validate_output(args.output)
        outcome = calibrate(
            split_path=args.split, baseline_ruleset_path=args.baseline_ruleset,
            parameter_space_path=args.parameter_space,
            weights_path=args.weights, output_dir=args.output)
        print(json.dumps({"calibration_id": outcome.calibration_id,
                          "candidate_ruleset_hash": outcome.candidate_ruleset_hash,
                          "recommendation": outcome.recommendation,
                          "comparison": outcome.comparison}, ensure_ascii=False,
                         sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": str(exc)}, ensure_ascii=False,
                         sort_keys=True, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
