#!/usr/bin/env python3
"""Prepare Map123 release data and run the existing realtime controller."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

from scripts import run_realtime_dual_control as realtime_launcher
from wargame_native_engine.data import (
    DataLoadError,
    MapRuntimeDataError,
    ScenarioBundle,
    prepare_map_runtime_data,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSETS_DIR = REPOSITORY_ROOT / "examples" / "map123"
DEFAULT_CACHE_ROOT = REPOSITORY_ROOT / "local_data" / "map123" / "runtime-cache"
SCENARIO_FILES = {
    "original": "scenario.json",
    "artillery-ammo2": "scenario-artillery-ammo2.json",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare the published Map123 lookup cache and run the loopback-only "
            "Native simplified realtime controller"
        ),
    )
    parser.add_argument(
        "--scenario",
        choices=tuple(SCENARIO_FILES),
        default="original",
        help="original scenario or the explicitly labelled 12-artillery ammo=2 demo",
    )
    parser.add_argument("--assets-dir", type=Path, default=DEFAULT_ASSETS_DIR)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--json", action="store_true", help="print full preparation and launcher metadata as JSON")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--run-namespace")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--tick-interval-ms", type=int, default=1_000)
    parser.add_argument("--max-catch-up-ticks-per-batch", type=int, default=4)
    parser.add_argument("--shutdown-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--red-token", default="local-red-seat")
    parser.add_argument("--blue-token", default="local-blue-seat")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scenario_path = args.assets_dir / SCENARIO_FILES[args.scenario]
    try:
        bundle = ScenarioBundle.from_path(scenario_path)
        prepared = prepare_map_runtime_data(
            bundle.scenario.map,
            release_dir=args.assets_dir,
            cache_root=args.cache_root,
        )
    except (DataLoadError, MapRuntimeDataError, OSError) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        print(f"MAP123_PREPARE_ERROR: {code}: {exc}", file=sys.stderr)
        return 2

    report = {
        "cache": "hit" if prepared.cache_hit else "prepared",
        "cache_dir": str(prepared.cache_dir),
        "manifest_sha256": prepared.manifest_sha256,
        "map_runtime_identity": prepared.identity,
        "scenario": args.scenario,
        "scenario_id": bundle.scenario_identity.scenario_id,
        "scenario_sha256": bundle.scenario_identity.sha256,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    else:
        print(f"PREPARED: map=123 scenario={bundle.scenario_identity.scenario_id} cache={report['cache']}", flush=True)
        print(f"SCENARIO_INPUT: {scenario_path.resolve()}", flush=True)
        print("RUNTIME: 机动/视线缓存已校验（详细路径使用 --json）", flush=True)
    if args.prepare_only:
        return 0

    run_namespace = args.run_namespace or _unique_run_namespace(args.scenario)
    output_dir = args.output_dir or (
        REPOSITORY_ROOT / "local_data" / "map123" / "runs" / run_namespace
    )
    forwarded = [
        "--scenario-bundle", str(scenario_path),
        "--map-runtime-data", str(prepared.cache_dir),
        "--output-dir", str(output_dir),
        "--run-namespace", run_namespace,
        "--seed", str(args.seed),
        "--tick-interval-ms", str(args.tick_interval_ms),
        "--max-catch-up-ticks-per-batch", str(args.max_catch_up_ticks_per_batch),
        "--shutdown-timeout-seconds", str(args.shutdown_timeout_seconds),
        "--host", args.host,
        "--port", str(args.port),
        "--red-token", args.red_token,
        "--blue-token", args.blue_token,
    ]
    if args.json:
        forwarded.append("--json")
    if args.overwrite:
        forwarded.append("--overwrite")
    return realtime_launcher.main(forwarded)


def _unique_run_namespace(scenario: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"map123-{scenario}-{timestamp}-{uuid4().hex[:8]}"


if __name__ == "__main__":
    raise SystemExit(main())
