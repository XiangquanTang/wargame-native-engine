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
    parser.add_argument(
        "--output-root",
        required=True,
        help=(
            "Mirror replay parents and write under each full ZIP file name, "
            "for example day1/game.zip/frame_analysis.native.jsonl"
        ),
    )
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
    plans = tuple(
        (
            input_path,
            output_root
            / input_path.relative_to(input_root).parent
            / input_path.name
            / FRAME_ANALYSIS_JSONL_NAME,
        )
        for input_path in inputs
    )
    try:
        _validate_batch_paths(inputs, plans, errors_path)
    except ValueError as exc:
        print(f"ValueError: {exc}", file=sys.stderr)
        return 2

    analyzer = EightFactorAnalyzer(
        model_id=args.model_id,
        model_version=args.model_version,
        model_hash=args.model_hash,
    )

    success = 0
    skipped = 0
    failures: list[Mapping[str, Any]] = []
    for input_path, output_path in plans:
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


def _validate_batch_paths(
    inputs: Sequence[Path],
    plans: Sequence[tuple[Path, Path]],
    errors_path: Path,
) -> None:
    """Reject aliases and collisions before any replay is processed."""

    input_by_resolved = {_resolved_key(path): path for path in inputs}
    input_by_platform_key = {
        _platform_key(path): path for path in inputs
    }
    input_by_identity = {
        identity: path
        for path in inputs
        if (identity := _existing_identity(path)) is not None
    }
    output_by_resolved: dict[str, tuple[Path, Path]] = {}
    output_by_identity: dict[tuple[int, int], tuple[Path, Path]] = {}
    output_by_platform_key: dict[str, tuple[Path, Path]] = {}

    for input_path, output_path in plans:
        resolved = _resolved_key(output_path)
        conflicting_input = input_by_resolved.get(resolved)
        if conflicting_input is None:
            conflicting_input = input_by_platform_key.get(
                os.path.normcase(resolved)
            )
        if conflicting_input is not None:
            raise ValueError(
                f"planned analysis output {output_path} conflicts with "
                f"input replay {conflicting_input}"
            )

        identity = _existing_identity(output_path)
        if identity is not None and identity in input_by_identity:
            raise ValueError(
                f"planned analysis output {output_path} conflicts with "
                f"input replay {input_by_identity[identity]}"
            )

        prior = output_by_resolved.get(resolved)
        if prior is not None:
            raise ValueError(
                f"planned analysis output collision between {prior[0]} "
                f"and {input_path}: {output_path}"
            )
        output_by_resolved[resolved] = (input_path, output_path)

        if identity is not None:
            prior_identity = output_by_identity.get(identity)
            if prior_identity is not None:
                raise ValueError(
                    f"planned analysis output alias between {prior_identity[0]} "
                    f"and {input_path}: {output_path}"
                )
            output_by_identity[identity] = (input_path, output_path)

        platform_key = os.path.normcase(resolved)
        prior_platform = output_by_platform_key.get(platform_key)
        if prior_platform is not None:
            raise ValueError(
                f"planned analysis output case-fold collision between "
                f"{prior_platform[0]} and {input_path}: {output_path}"
            )
        output_by_platform_key[platform_key] = (input_path, output_path)

    for input_path in inputs:
        if _paths_alias(errors_path, input_path):
            raise ValueError(
                f"errors path {errors_path} conflicts with "
                f"input replay {input_path}"
            )
    for input_path, output_path in plans:
        if _paths_alias(errors_path, output_path):
            raise ValueError(
                f"errors path {errors_path} conflicts with planned analysis "
                f"output for {input_path}: {output_path}"
            )


def _resolved_key(path: Path) -> str:
    return str(path.resolve(strict=False))


def _platform_key(path: Path) -> str:
    return os.path.normcase(_resolved_key(path))


def _existing_identity(path: Path) -> tuple[int, int] | None:
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    return value.st_dev, value.st_ino


def _paths_alias(first: Path, second: Path) -> bool:
    if _platform_key(first) == _platform_key(second):
        return True
    try:
        return os.path.samefile(first, second)
    except FileNotFoundError:
        return False


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
