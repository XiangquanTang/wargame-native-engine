from __future__ import annotations

import argparse
import json
from pathlib import Path

from wargame_native_engine.cli.run_game import _validate_output
from wargame_native_engine.io.importers.historical_replay import (
    HistoricalReplayImporter,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Import a parsed historical replay as read-only evidence")
    value.add_argument("--input", required=True, type=Path)
    value.add_argument("--output", required=True, type=Path)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        _validate_output(args.output)
        result = HistoricalReplayImporter().import_path(
            args.input, output_dir=args.output)
        print(json.dumps(result.import_manifest, ensure_ascii=False,
                         sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": str(exc)}, ensure_ascii=False,
                         sort_keys=True, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
