#!/usr/bin/env python3
"""Convert one legacy serialized replay into frame-playback artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from wargame_native_engine.analysis import EightFactorAnalyzer
from wargame_native_engine.replay import convert_legacy_replay


_DEFAULT_ANALYZER = EightFactorAnalyzer()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert a legacy serialized replay into a numbered playback ZIP "
            "and a separate eight-factor JSONL file."
        ),
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
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
        result = convert_legacy_replay(
            args.input,
            args.output,
            analyzer=analyzer,
            overwrite=args.overwrite,
        )
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({
        "input": str(result.input_path),
        "source_archive_sha256": result.source_archive_sha256,
        "raw_record_count": result.raw_record_count,
        "selected_frame_count": result.selected_frame_count,
        "collapsed_record_count": result.collapsed_record_count,
        "output_dir": str(result.output_dir),
        "replay_zip": str(result.replay_zip_path),
        "analysis_jsonl": str(result.analysis_jsonl_path),
        "replay_zip_sha256": result.replay_zip_sha256,
        "conversion_version": result.conversion_version,
        "playback_compatibility": result.playback_compatibility,
        "strict_replay_supported": result.strict_replay_supported,
        "model_id": analyzer.model_id,
        "model_version": analyzer.model_version,
        "model_hash": analyzer.model_hash,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
       allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
