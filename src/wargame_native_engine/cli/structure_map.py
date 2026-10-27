"""Export one explicit static map without overwriting existing artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from wargame_native_engine.io.serialization import canonical_json
from wargame_native_engine.io.structured_map import load_structured_map


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--input-format", required=True,
                        choices=("native-scenario", "native-map", "basic"))
    parser.add_argument("--output", type=Path, help="New JSON file; omit to write stdout")
    parser.add_argument("--coordinate-system", choices=("odd-r",),
                        help="Explicit basic-map override when source metadata is absent")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = load_structured_map(
            args.input, input_format=args.input_format,
            coordinate_system_override=args.coordinate_system,
        )
        payload = canonical_json(result) + "\n"
        if args.output is None:
            sys.stdout.write(payload)
        else:
            with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"map export failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
