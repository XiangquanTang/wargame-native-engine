from __future__ import annotations

import argparse
import json
from pathlib import Path

from wargame_native_engine.cli.run_game import _validate_output
from wargame_native_engine.io.importers import LegacyScenarioImporter
from wargame_native_engine.rules.config import default_ruleset_path


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Import a legacy JSON fixture into a NativeScenario")
    value.add_argument("--source", required=True, type=Path)
    value.add_argument("--source-identifier")
    value.add_argument("--ruleset", type=Path, default=default_ruleset_path())
    value.add_argument("--output", required=True, type=Path)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        _validate_output(args.output)
        result = LegacyScenarioImporter(args.ruleset).import_file(
            args.source, source_identifier=args.source_identifier,
            output_dir=args.output)
        print(json.dumps({
            "status": "complete", "migration_id": result.migration_id,
            "output": str(args.output),
            "scenario_hash": result.scenario.scenario_hash,
            "ruleset_hash": result.manifest["ruleset_hash"],
        }, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": str(exc)}, ensure_ascii=False,
                         sort_keys=True, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
