#!/usr/bin/env python3
"""List, verify and start maps included in the source release."""
from __future__ import annotations

import argparse
import hashlib
import json
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="list maps and configured hex counts")
    listing.add_argument("--json", action="store_true")
    check = sub.add_parser("verify", help="verify file hashes, scenario identities and renderer projections")
    check.add_argument("--map", dest="map_id")
    run = sub.add_parser("run", help="start one map; extra arguments are passed to its existing Web launcher")
    run.add_argument("map_id")
    run.add_argument("--variant")
    args, extra = parser.parse_known_args(argv)
    if extra and args.command != "run":
        parser.error("unrecognized arguments: " + " ".join(extra))
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
