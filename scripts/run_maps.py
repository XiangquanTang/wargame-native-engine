#!/usr/bin/env python3
"""List, verify and start maps included in the source release."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "examples/maps/catalog.json"


def _catalog():
    value = json.loads(CATALOG.read_text(encoding="utf-8"))
    if value.get("schema") != "native-release-map-catalog/1.0":
        raise ValueError("unsupported map catalog")
    return value


def _path(relative: str):
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError(f"map asset unavailable: {relative}")
    return path


def _sha256(path: Path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def verify(catalog, entries):
    from wargame_native_engine.data import ScenarioBundle

    files = {f["path"]: f for f in catalog["files"]}
    checked = set()
    for entry in entries:
        for relative in entry["resources"]:
            record = files[relative]
            path = _path(relative)
            if path.stat().st_size != record["bytes"] or _sha256(path) != record["sha256"]:
                raise ValueError(f"map asset checksum mismatch: {relative}")
            checked.add(relative)
        for variant in entry["scenarios"]:
            bundle = ScenarioBundle.from_path(_path(variant["path"]))
            bundle.validate()
            if (bundle.map_identity.map_id != entry["native_map_id"]
                or bundle.map_identity.sha256 != entry["native_map_sha256"]
                or bundle.scenario_identity.sha256 != variant["scenario_sha256"]):
                raise ValueError(f"map/scenario identity mismatch: {variant['path']}")
            bundle.render_map_record()
    return len(checked)


def _parse_visibility_modes(text):
    try:
        values = tuple(int(part) for part in text.split(","))
        from wargame_native_engine.data.map_workspace import _visibility_modes
        return _visibility_modes(values, label="--visibility-modes")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    initialize = sub.add_parser("init", help="create the local multi-map workspace")
    initialize.add_argument("--workspace", type=Path, default=ROOT / "local_data/maps")
    intake = sub.add_parser("check", help="check uploaded maps independently and write a report")
    intake.add_argument("--workspace", type=Path, default=ROOT / "local_data/maps")
    intake.add_argument("--input", type=Path, help="map file, ZIP, or folder; defaults to workspace/inbox")
    intake.add_argument("--coordinate-system", choices=("odd-r",))
    intake.add_argument("--terrain-only", action="store_true", help="skip runtime table expansion")
    intake.add_argument("--require-runtime", action="store_true")
    intake.add_argument("--visibility-modes", type=_parse_visibility_modes,
                        help="canonical channel IDs in physical array order, e.g. 0,1,2 or 0,3,4")
    listing = sub.add_parser("list", help="list maps and configured hex counts")
    listing.add_argument("--json", action="store_true")
    check = sub.add_parser("verify", help="verify file hashes, scenario identities and renderer projections")
    check.add_argument("--map", dest="map_id")
    run = sub.add_parser("run", help="start one map; extra arguments are passed to its existing Web launcher")
    run.add_argument("map_id")
    run.add_argument("--variant")
    start = sub.add_parser("start", help="convert an uploaded Source scenario and map, then start a fresh game")
    start.add_argument("--input", type=Path, required=True, help="one raw map ZIP, basic.json, or folder")
    start.add_argument("--scenario-input", type=Path, required=True, help="Source scenario JSON")
    start.add_argument("--workspace", type=Path, default=ROOT / "local_data/maps")
    start.add_argument("--coordinate-system", choices=("odd-r",))
    start.add_argument("--visibility-modes", type=_parse_visibility_modes)
    start.add_argument("--json", action="store_true", help="print full preparation and launcher details as JSON")
    start.add_argument("--prepare-only", action="store_true", help="validate and write the game without starting the Web server")
    args, extra = parser.parse_known_args(argv)
    if extra and args.command not in ("run", "start"):
        parser.error("unrecognized arguments: " + " ".join(extra))
    if args.command == "start":
        protected = ("--scenario-bundle", "--map-runtime-data", "--output-dir", "--run-namespace", "--overwrite")
        if any(token.split("=", 1)[0] in protected for token in extra):
            parser.error("start manages the scenario, runtime, namespace and fresh output directory")
        if args.prepare_only and extra:
            parser.error("--prepare-only does not accept Web launcher arguments")
        # Validate the complete downstream grammar before conversion and forbid
        # argparse's abbreviated aliases for generated/protected bindings.
        from scripts import run_realtime_dual_control
        launcher_parser = run_realtime_dual_control.build_parser()
        launcher_parser.allow_abbrev = False
        launch_options = launcher_parser.parse_args([
            "--output-dir", "managed-output", "--run-namespace", "managed-run", *extra,
        ])
        if not 1 <= launch_options.port <= 65535:
            parser.error("--port must be in 1..65535")
        if launch_options.tick_interval_ms <= 0 or launch_options.max_catch_up_ticks_per_batch <= 0:
            parser.error("tick interval and catch-up batch size must be positive")
        if not math.isfinite(launch_options.shutdown_timeout_seconds) or launch_options.shutdown_timeout_seconds <= 0:
            parser.error("shutdown timeout must be positive")
        from wargame_native_engine.data.scenario_workspace import prepare_source_game
        report = prepare_source_game(args.input, args.scenario_input, args.workspace,
                                     coordinate_system=args.coordinate_system,
                                     visibility_modes=args.visibility_modes)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        else:
            print(f"PREPARED: map={report['map_id']} scenario={report['scenario_id']}", flush=True)
            print(f"MAP_INPUT: {args.input.resolve()}", flush=True)
            print(f"SCENARIO_INPUT: {args.scenario_input.resolve()}", flush=True)
            print(f"PREPARATION: {report['preparation_path']}", flush=True)
            warnings = report.get("conversion", {}).get("warnings", [])
            for warning in warnings:
                print(f"WARNING: {warning}", flush=True)
        if args.prepare_only:
            return 0
        return run_realtime_dual_control.main([
            "--scenario-bundle", report["scenario_path"],
            "--map-runtime-data", report["runtime"]["path"],
            "--output-dir", report["output_dir"],
            "--run-namespace", report["run_namespace"], *(["--json"] if args.json else []), *extra,
        ])
    if args.command in ("init", "check"):
        from wargame_native_engine.data.map_workspace import initialize_workspace, check_workspace
        if args.command == "init":
            print(json.dumps(initialize_workspace(args.workspace), ensure_ascii=False, indent=2))
            return 0
        report = check_workspace(args.workspace, source=args.input, coordinate_system=args.coordinate_system,
                                 terrain_only=args.terrain_only, require_runtime=args.require_runtime,
                                 visibility_modes=args.visibility_modes)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["total"] and not report["failed"] else 2
    catalog = _catalog()
    entries = catalog["maps"]
    if args.command == "list":
        if args.json:
            print(json.dumps(catalog, ensure_ascii=False, indent=2))
        else:
            print(f"{len(entries)} maps / {catalog['scenario_count']} scenario variants")
            for entry in entries:
                variants = ",".join(v["variant"] for v in entry["scenarios"])
                print(f"{entry['id']:<23} {entry['grid']['configured_hexes']:>5} hexes  {entry['name']}  [{variants}]")
        return 0
    if args.map_id:
        entries = [entry for entry in entries if entry["id"] == args.map_id]
        if not entries:
            raise ValueError(f"unknown map {args.map_id!r}; use 'python -m scripts.run_maps list'")
    count = verify(catalog, entries)
    if args.command == "verify":
        print(json.dumps({"verified": True, "maps": len(entries), "data_files": count,
                          "map123_cache_prepared": False}, ensure_ascii=False))
        return 0
    entry = entries[0]
    variant_name = args.variant or entry["default_variant"]
    variants = [v for v in entry["scenarios"] if v["variant"] == variant_name]
    if not variants:
        raise ValueError(f"unknown variant {variant_name!r} for {entry['id']}")
    if entry["id"] == "map123":
        from scripts import run_map123
        return run_map123.main(["--scenario", variant_name, *extra])
    from scripts import run_realtime_dual_control
    namespace = f"map-{entry['id']}-{uuid4().hex[:12]}"
    return run_realtime_dual_control.main([
        "--scenario-bundle", str(_path(variants[0]["path"])),
        "--output-dir", str(ROOT / "local_data/maps/runs" / namespace),
        "--run-namespace", namespace, *extra,
    ])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(f"MAP_DATA_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
