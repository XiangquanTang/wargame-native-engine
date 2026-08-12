#!/usr/bin/env python3
"""Serial batch entry point for deterministic replay analysis."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping, Sequence

from wargame_native_engine.analysis import (
    EightFactorAnalyzer,
    FRAME_ANALYSIS_JSONL_NAME,
    analyze_replay_to_jsonl,
)


_DEFAULT_ANALYZER = EightFactorAnalyzer()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serially analyze replay ZIP files under one root.",
    )
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument(
        "--format",
        dest="source_format",
        choices=("auto", "native", "historical"),
        default="auto",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--errors", required=True)
    parser.add_argument("--model-id", default=_DEFAULT_ANALYZER.model_id)
    parser.add_argument(
        "--model-version",
        default=_DEFAULT_ANALYZER.model_version,
    )
    parser.add_argument("--model-hash", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_root = Path(args.input_root)
    output_root = Path(args.output_root)
    errors_path = Path(args.errors)
    if not input_root.is_dir():
        print(f"NotADirectoryError: input root is not a directory: {input_root}",
              file=sys.stderr)
        return 2

    inputs = tuple(sorted(
        (
            path for path in input_root.rglob("*")
            if path.is_file() and path.suffix.lower() == ".zip"
        ),
        key=lambda path: path.relative_to(input_root).as_posix(),
    ))
    analyzer = EightFactorAnalyzer(
        model_id=args.model_id,
        model_version=args.model_version,
        model_hash=args.model_hash,
    )

    success = 0
    skipped = 0
    failures: list[Mapping[str, Any]] = []
    for input_path in inputs:
        relative = input_path.relative_to(input_root)
        output_path = (
            output_root
            / relative.parent
            / input_path.stem
            / FRAME_ANALYSIS_JSONL_NAME
        )
        if args.resume and output_path.is_file() and output_path.stat().st_size:
            skipped += 1
            continue
        try:
            analyze_replay_to_jsonl(
                input_path,
                output_path,
                source_format=args.source_format,
                analyzer=analyzer,
                overwrite=bool(args.resume and output_path.exists()),
            )
            success += 1
        except Exception as exc:
            failures.append({
                "input_path": str(input_path),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            })

    error_payload = "".join(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
        for record in failures
    ).encode("utf-8")
    _atomic_write(errors_path, error_payload)

    print(json.dumps({
        "discovered": len(inputs),
        "success": success,
        "skipped": skipped,
        "failed": len(failures),
        "errors": str(errors_path),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
       allow_nan=False))
    return 1 if failures else 0


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
