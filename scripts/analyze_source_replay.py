#!/usr/bin/env python3
"""Analyze one Native or historical replay into derived JSONL."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from wargame_native_engine.analysis import (
    EightFactorAnalyzer,
    analyze_replay_to_jsonl,
)


_DEFAULT_ANALYZER = EightFactorAnalyzer()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write deterministic eight-factor analysis for one replay.",
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--format",
        dest="source_format",
        choices=("auto", "native", "historical"),
        default="auto",
    )
    parser.add_argument("--model-id", default=_DEFAULT_ANALYZER.model_id)
    parser.add_argument(
        "--model-version",
        default=_DEFAULT_ANALYZER.model_version,
    )
    parser.add_argument("--model-hash", default=None)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    analyzer = EightFactorAnalyzer(
        model_id=args.model_id,
        model_version=args.model_version,
        model_hash=args.model_hash,
    )
    try:
        result = analyze_replay_to_jsonl(
            args.input,
            args.output,
            source_format=args.source_format,
            analyzer=analyzer,
            overwrite=args.overwrite,
        )
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({
        "input": str(result.input_path),
        "source_format": result.source_format,
        "frame_count": result.frame_count,
        "output": str(result.output_path),
        "model_id": analyzer.model_id,
        "model_version": analyzer.model_version,
        "model_hash": analyzer.model_hash,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
       allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
