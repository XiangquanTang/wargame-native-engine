"""Generate independent replay analysis with an explicit Source/Native gate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from wargame_native_engine.analysis.generation import (
    generate_native_analysis,
    generate_source_analysis,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--input", required=True, type=Path)
    value.add_argument("--output-directory", required=True, type=Path,
                       help="new directory outside the input replay directory")
    value.add_argument("--analysis-model", required=True, choices=("source", "native"),
                       help="explicit formula input contract; never guessed")
    value.add_argument("--source-format", default="auto",
                       choices=("auto", "native", "historical"),
                       help="replay container format only, not the analysis model")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    generator = {"source": generate_source_analysis, "native": generate_native_analysis}[
        args.analysis_model
    ]
    try:
        result = generator(args.input, args.output_directory, source_format=args.source_format)
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({
        "status": "ok", "analysis_model": result.analysis_model,
        "source_format": result.analysis.source_format,
        "frame_count": result.analysis.frame_count,
        "output_directory": str(result.output_directory),
        "analysis": str(result.analysis.output_path),
        "replay": str(result.replay_path) if result.replay_path else None,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
