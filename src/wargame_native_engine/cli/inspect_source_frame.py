from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from wargame_native_engine.cli.run_game import _validate_output
from wargame_native_engine.compat.source_frame import SourceFrameExporter, SourceFrameImporter
from wargame_native_engine.io.serialization import canonical_hash


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Inspect a documented source standard frame")
    value.add_argument("--input", required=True, type=Path)
    value.add_argument("--output", required=True, type=Path)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        _validate_output(args.output)
        if args.output.exists() and any(args.output.iterdir()):
            raise FileExistsError(f"refusing to overwrite non-empty output: {args.output}")
        args.output.mkdir(parents=True, exist_ok=True)
        importer = SourceFrameImporter()
        frame = importer.load(args.input)
        world, mapping = importer.to_world(frame)
        relationships = _relationships(frame)
        compatible = SourceFrameExporter().export(world, template=frame)
        summary = {
            "status": "loaded", "schema_version": frame.schema_version,
            "source_hash": frame.source_hash,
            "scenario_id": frame.scenario_id, "terrain_id": frame.terrain_id,
            "operators": len(frame.operators), "passengers": len(frame.passengers),
            "world_hash": canonical_hash(world.to_dict()),
            "compatible_frame_hash": canonical_hash(compatible),
            "relationship_issues": len(relationships["unresolved"]),
            "warnings": list(frame.warnings),
            "valid_actions_used_as_rule_truth": False,
        }
        _write(args.output / "source_frame.json", frame.to_dict())
        _write(args.output / "mapping_report.json", mapping)
        _write(args.output / "relationships.json", relationships)
        _write(args.output / "native_world.json", world.to_dict())
        _write(args.output / "compatible_frame.json", compatible)
        _write(args.output / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": str(exc)}, ensure_ascii=False,
                         sort_keys=True, indent=2))
        return 2


def _relationships(frame: Any) -> dict[str, Any]:
    ids = {item.obj_id for item in frame.all_units}
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for unit in frame.all_units:
        values = [("car", unit.car_id), ("launcher", unit.launcher_id)]
        values.extend(("passenger_ids", value) for value in unit.passenger_ids)
        values.extend(("launch_ids", value) for value in unit.launch_ids)
        for field, target in values:
            if target is None:
                continue
            record = {"source_id": unit.obj_id, "field": field, "target_id": target}
            (resolved if target in ids else unresolved).append(record)
    return {"resolved": sorted(resolved, key=_relation_key),
            "unresolved": sorted(unresolved, key=_relation_key)}


def _relation_key(value: dict[str, Any]) -> tuple[Any, ...]:
    return value["source_id"], value["field"], value["target_id"]


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
