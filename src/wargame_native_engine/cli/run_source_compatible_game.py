from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from wargame_native_engine.adjudication import AdjudicationTableSet
from wargame_native_engine.cli.run_game import _validate_output
from wargame_native_engine.compat.source_frame import SourceFrameExporter, SourceFrameImporter
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.rules.config import default_ruleset_path, load_ruleset


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Load a source frame into the safe Native compatibility boundary")
    value.add_argument("--frame", required=True, type=Path)
    value.add_argument("--ruleset", type=Path, default=default_ruleset_path())
    value.add_argument("--table-set", required=True, type=Path)
    value.add_argument("--max-events", type=int, default=20)
    value.add_argument("--output", required=True, type=Path)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.max_events < 0:
            raise ValueError("max-events must be non-negative")
        _validate_output(args.output)
        if args.output.exists() and any(args.output.iterdir()):
            raise FileExistsError(f"refusing to overwrite non-empty output: {args.output}")
        args.output.mkdir(parents=True, exist_ok=True)
        ruleset = load_ruleset(args.ruleset)
        tables = AdjudicationTableSet(args.table_set)
        importer = SourceFrameImporter()
        frame = importer.load(args.frame)
        world, mapping = importer.to_world(frame)
        warnings = list(frame.warnings) + [
            "Source frame contains no terrain elevation grid; direct-fire LOS was not adjudicated",
            "Source armor/type/ammunition-slot mappings remain incomplete under U01",
            "Source frame contains no submitted action; no action was inferred",
            "Source valid_actions and action echo were not used as rule truth",
        ]
        compatible = SourceFrameExporter().export(world, template=frame)
        manifest = {
            "schema_version": "native-source-compatible-run/1.0",
            "status": "COMPATIBILITY_LOAD_ONLY",
            "source_frame_hash": frame.source_hash,
            "scenario_id": frame.scenario_id, "terrain_id": frame.terrain_id,
            "ruleset_id": ruleset.ruleset_id,
            "ruleset_version": ruleset.ruleset_version,
            "ruleset_hash": ruleset.content_hash,
            "table_set_id": tables.table_set_id,
            "table_set_version": tables.table_set_version,
            "table_set_hash": tables.content_hash,
            "events_processed": 0, "max_events": args.max_events,
            "world_hash": canonical_hash(world.to_dict()), "warnings": warnings,
        }
        _write(args.output / "manifest.json", manifest)
        _write(args.output / "mapping_report.json", mapping)
        _write(args.output / "initial_world.json", world.to_dict())
        _write(args.output / "compatible_frame.json", compatible)
        (args.output / "events.jsonl").write_text("", encoding="utf-8")
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": str(exc)}, ensure_ascii=False,
                         sort_keys=True, indent=2))
        return 2


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
