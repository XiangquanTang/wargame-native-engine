"""Convert one explicit Source Scenario into a canonical Native bundle file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from wargame_native_engine.data import (
    EXPLICIT_NATIVE_SUPPLEMENT,
    STRICT_SOURCE,
    convert_source_scenario,
    publish_source_scenario_conversion,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-scenario", type=Path, required=True)
    parser.add_argument("--source-map", type=Path, required=True)
    parser.add_argument("--source-scenario-identity", required=True)
    parser.add_argument("--source-map-identity", required=True)
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--map-id", required=True)
    parser.add_argument(
        "--mode",
        choices=(STRICT_SOURCE, EXPLICIT_NATIVE_SUPPLEMENT),
        required=True,
    )
    parser.add_argument("--supplement", type=Path)
    parser.add_argument("--coordinate-system", choices=("odd-r",))
    parser.add_argument("--hex-size-m", type=int)
    parser.add_argument("--elevation-unit-m", type=float)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conversion = convert_source_scenario(
        args.source_scenario,
        args.source_map,
        conversion_mode=args.mode,
        source_scenario_identity=args.source_scenario_identity,
        source_map_identity=args.source_map_identity,
        expected_scenario_id=args.scenario_id,
        expected_map_id=args.map_id,
        supplement_path=args.supplement,
        coordinate_system_override=args.coordinate_system,
        hex_size_m_override=args.hex_size_m,
        elevation_unit_m_override=args.elevation_unit_m,
    )
    published = publish_source_scenario_conversion(conversion, args.output_dir)
    scenario = conversion.scenario
    print(json.dumps({
        "conversion_mode": args.mode,
        "scenario_id": scenario.scenario_id,
        "scenario_hash": scenario.scenario_hash,
        "scenario_payload_sha256": published.scenario_sha256,
        "map_id": scenario.map.map_id,
        "map_hash": conversion.manifest["native_map"]["sha256"],
        "operator_count": len(scenario.operators),
        "scenario_path": str(published.scenario_path),
        "manifest_path": str(published.manifest_path),
        "manifest_sha256": published.manifest_sha256,
        "native_rules_equivalence": False,
        "warnings": conversion.manifest["warnings"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
