#!/usr/bin/env python3
"""Convert one explicit legacy map cost/visibility pair for Native runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from wargame_native_engine.data import publish_map_runtime_data
from wargame_native_engine.io.native_map_converter import (
    convert_benchmark_basic_map,
)
from wargame_native_engine.io.scenario import native_map_identity_hash


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate legacy cost.pickle and see.npz data, then emit a "
            "provenance-bound Native simplified-rules runtime lookup directory."
        )
    )
    parser.add_argument("--map-config", type=Path, required=True)
    parser.add_argument("--movement-cost", type=Path, required=True)
    parser.add_argument("--visibility-npz", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--coordinate-system", choices=("odd-r",))
    parser.add_argument("--hex-size-m", type=int)
    parser.add_argument("--elevation-unit-m", type=float)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    map_conversion = convert_benchmark_basic_map(
        args.map_config,
        coordinate_system_override=args.coordinate_system,
        hex_size_m_override=args.hex_size_m,
        elevation_unit_m_override=args.elevation_unit_m,
    )
    published = publish_map_runtime_data(
        map_conversion.native_map,
        source_map_path=args.map_config,
        movement_cost_path=args.movement_cost,
        visibility_npz_path=args.visibility_npz,
        output_dir=args.output_dir,
    )
    print(json.dumps({
        "map_id": map_conversion.native_map.map_id,
        "map_sha256": native_map_identity_hash(map_conversion.native_map),
        "runtime_dir": str(published.output_dir),
        "runtime_manifest": str(published.manifest_path),
        "runtime_manifest_sha256": published.manifest_sha256,
        "movement_cost": str(published.movement_cost_path),
        "visibility_npy": str(published.visibility_path),
        "visibility_storage": "read_only_memory_mapped_npy",
        "native_rules_equivalence": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
