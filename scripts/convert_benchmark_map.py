from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from wargame_native_engine.io.native_map_converter import (
    convert_benchmark_basic_map,
    publish_native_map_conversion,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert one explicit benchmark basic.json into a provenance-backed "
            "Native simplified-rules map."
        ),
    )
    parser.add_argument("--map-config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--coordinate-system",
        choices=("odd-r",),
        help="Required only when the source omits coordinate_system.",
    )
    parser.add_argument(
        "--hex-size-m",
        type=int,
        help="Required only when the source omits scale.",
    )
    parser.add_argument(
        "--elevation-unit-m",
        type=float,
        help="Required only when the source omits ele_grade.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conversion = convert_benchmark_basic_map(
        args.map_config,
        coordinate_system_override=args.coordinate_system,
        hex_size_m_override=args.hex_size_m,
        elevation_unit_m_override=args.elevation_unit_m,
    )
    published = publish_native_map_conversion(conversion, args.output_dir)
    print(json.dumps({
        "map_id": conversion.native_map.map_id,
        "cell_count": len(conversion.native_map.hexes),
        "native_map_path": str(published.native_map_path),
        "native_map_sha256": published.native_map_sha256,
        "provenance_path": str(published.provenance_path),
        "provenance_sha256": published.provenance_sha256,
        "warnings": conversion.provenance["warnings"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
