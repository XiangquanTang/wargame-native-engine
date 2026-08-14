"""Inspect one explicit Source Scenario/map pair without converting rules."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from wargame_native_engine.data import inspect_source_scenario


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-scenario", type=Path, required=True)
    parser.add_argument("--source-map", type=Path, required=True)
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--map-id", required=True)
    parser.add_argument("--coordinate-system", choices=("odd-r",))
    parser.add_argument("--hex-size-m", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = inspect_source_scenario(
        args.source_scenario,
        args.source_map,
        expected_scenario_id=args.scenario_id,
        expected_map_id=args.map_id,
        coordinate_system_override=args.coordinate_system,
        hex_size_m_override=args.hex_size_m,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
