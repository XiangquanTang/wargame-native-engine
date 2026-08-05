from __future__ import annotations

import argparse
import json
from pathlib import Path

from wargame_native_engine.alignment.aligner import ReplayAligner, load_weights
from wargame_native_engine.cli.run_game import _validate_output
from wargame_native_engine.io.importers.historical_replay import (
    load_historical_replay,
)
from wargame_native_engine.rules.config import default_ruleset_path, load_ruleset


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Align historical evidence with Native simplified rules")
    value.add_argument("--historical-replay", required=True, type=Path)
    value.add_argument("--ruleset", type=Path, default=default_ruleset_path())
    value.add_argument("--weights", type=Path,
                       default=Path("config/calibration/metric_weights_v1.json"))
    value.add_argument("--unsupported-policy", default="skip_tick",
                       choices=("skip_tick", "stop"))
    value.add_argument("--output", required=True, type=Path)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        _validate_output(args.output)
        replay = load_historical_replay(args.historical_replay)
        ruleset = load_ruleset(args.ruleset)
        result = ReplayAligner().align(
            replay, ruleset, weights=load_weights(args.weights),
            unsupported_policy=args.unsupported_policy)
        result.write(args.output)
        print(json.dumps(result.summary, ensure_ascii=False,
                         sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": str(exc)}, ensure_ascii=False,
                         sort_keys=True, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
